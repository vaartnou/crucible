"""A deterministic fluid network: demand, queues, retries and finite connection pools.

Services are in callee-first topological order. A bounded damped fixed-point iteration couples
caller retries to downstream demand and downstream delay to caller timeouts. Entry demand does
not include retries. The per-minute samples are the only source of telemetry.
"""

import math
from dataclasses import dataclass, field, replace

from crucible.envs.oncall.world import Edge, World

SOLVER_FIXED_POINT_ITERATIONS = 2048
SOLVER_NEWTON_ITERATIONS = 64
SOLVER_LINE_SEARCH_ITERATIONS = 24


def _linear_solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve a small dense system with partial pivoting, without an array dependency."""
    rows = [[*row, value] for row, value in zip(matrix, rhs, strict=True)]
    size = len(rows)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(column + 1, size):
            factor = rows[row][column] / rows[column][column]
            for index in range(column + 1, size + 1):
                rows[row][index] -= factor * rows[column][index]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        solution[row] = (
            rows[row][size] - sum(rows[row][col] * solution[col] for col in range(row + 1, size))
        ) / rows[row][row]
    return solution


@dataclass(frozen=True)
class PodSample:
    minute: int
    service: str
    pod: str
    version: str
    state: str
    request_rate: float
    error_ratio: float
    p99_latency: float
    memory: float
    pool_in_use: float
    queue_depth: float
    message: str


@dataclass(frozen=True)
class EdgeSample:
    minute: int
    caller: str
    callee: str
    request_rate: float
    attempt_rate: float
    error_ratio: float
    latency: float
    concurrency: float
    pool_in_use: float
    timeout_ms: float
    pool_size: float
    retries: float
    offered_attempt_rate: float = 0
    pool_rejection_ratio: float = 0
    solver_residual: float = 0
    solver_iterations: int = 0


@dataclass
class State:
    minute: int = 5
    calls: int = 0
    repaired: bool = False
    deployment_rolled_back: bool = False
    pending_action: tuple[str, str, str] | None = None
    restart_until: int = -1
    stampede_until: int = -1
    penalty: float = 0
    report: dict[str, str] | None = None
    incident_reports: list[dict[str, str]] | None = None
    history: list[tuple[PodSample, ...]] = field(
        default_factory=lambda: list[tuple[PodSample, ...]]()
    )
    edge_history: list[tuple[EdgeSample, ...]] = field(
        default_factory=lambda: list[tuple[EdgeSample, ...]]()
    )
    reverted_changes: set[str] = field(default_factory=lambda: set[str]())
    restart_at: dict[str, int] = field(default_factory=lambda: dict[str, int]())


def effective_parameters(world: World, state: State, minute: int) -> dict[str, dict[str, float]]:
    result = {
        s.name: {
            "capacity": s.capacity,
            "base_latency": s.base_latency,
            "version": float(s.version),
            "bad_pods": 0.0,
            "pod_error": 0.0,
            "leak_mib_min": 0.0,
            "consumer_running": 1.0,
        }
        for s in world.services
    }
    for change in world.changes:
        if (
            change.minute <= minute
            and change.id not in state.reverted_changes
            and change.callee is None
        ):
            result[change.service].update(change.after)
    for build in world.builds:
        if result[build.service]["version"] == build.version:
            result[build.service].update(
                {
                    "bad_pods": float(build.bad_pods),
                    "pod_error": build.pod_error,
                    "leak_mib_min": build.leak_mib_min,
                    "consumer_running": build.consumer_running,
                }
            )
    return result


def effective_edges(world: World, state: State, minute: int) -> tuple[Edge, ...]:
    edges = world.edges or tuple(
        Edge(s.name, dep) for s in world.services for dep in s.dependencies
    )
    result = {(e.caller, e.callee): e for e in edges}
    for change in world.changes:
        if (
            change.minute <= minute
            and change.id not in state.reverted_changes
            and change.callee is not None
        ):
            key = (change.service, change.callee)
            edge = result[key]
            result[key] = replace(
                edge,
                timeout_ms=change.after.get("timeout_ms", edge.timeout_ms),
                pool_size=change.after.get("pool_size", edge.pool_size),
                retries=change.after.get("retries", edge.retries),
                rate_multiplier=change.after.get("rate_multiplier", edge.rate_multiplier),
            )
    return tuple(result.values())


def solve(
    world: World, state: State, minute: int
) -> tuple[tuple[PodSample, ...], tuple[EdgeSample, ...]]:
    params = effective_parameters(world, state, minute)
    edges = effective_edges(world, state, minute)
    outgoing: dict[str, list[Edge]] = {s.name: [] for s in world.services}
    for edge in edges:
        outgoing[edge.caller].append(edge)
    if minute < 0:
        demand_scale = 0.55 + 0.45 * math.exp(-((minute / 11.0) ** 2))
    else:
        demand_scale = 1.0 + 0.03 * math.sin(minute / 18.0)
    entry = world.entry_rate * demand_scale
    rates = {s.name: 0.0 for s in world.services}
    rates["edge-gateway"] = entry
    for service in reversed(world.services):
        for edge in outgoing[service.name]:
            rates[edge.callee] += rates[service.name] * edge.rate_multiplier
    health: dict[str, tuple[float, float]] = {s.name: (0.0, 0.05) for s in world.services}
    edge_rows: dict[tuple[str, str], EdgeSample] = {}
    own: dict[str, tuple[float, float, float, float, float, str, str]] = {}
    previous = next(
        (frame for frame in reversed(state.history) if frame and frame[0].minute <= minute), ()
    )
    previous_queue = {
        s.name: sum(p.queue_depth for p in previous if p.service == s.name) for s in world.services
    }
    elapsed = max(0, minute - previous[0].minute) if previous else 1

    # Correlated failures persist across retries (timeouts and capacity saturation are not
    # independent Bernoulli failures). Retry traffic nevertheless consumes callee capacity.
    def evaluate(
        candidate: dict[str, float],
    ) -> tuple[
        dict[str, float],
        dict[str, tuple[float, float]],
        dict[str, tuple[float, float, float, float, float, str, str]],
        dict[tuple[str, str], EdgeSample],
    ]:
        rates = candidate
        own: dict[str, tuple[float, float, float, float, float, str, str]] = {}
        edge_rows: dict[tuple[str, str], EdgeSample] = {}
        next_health: dict[str, tuple[float, float]] = {}
        for service in world.services:
            p = params[service.name]
            capacity = max(1.0, p["capacity"] * service.replicas)
            rate = rates[service.name]
            utilisation = rate / capacity
            latency = min(10.0, p["base_latency"] / max(0.01, 1 - utilisation))
            error = max(0.0, 1 - capacity / max(rate, 1.0))
            status, message = "Running", "request completed"
            base_memory = 150.0 + float(
                (int(service.capacity * 10) + int(service.base_latency * 1000)) % 180
            )
            memory, backlog = base_memory, max(0.0, rate - capacity) * 60
            canary_memory = base_memory
            leaking_pods = 0
            if p["bad_pods"]:
                error = 1 - (1 - error) * (1 - p["pod_error"] * p["bad_pods"] / service.replicas)
                message = "rpc handler returned status 500: internal execution failure"
            leak = p["leak_mib_min"]
            if leak > 0:
                leaking_pods = int(p["bad_pods"]) if p["bad_pods"] > 0 else service.replicas
                deployed = max(
                    (
                        c.minute
                        for c in world.changes
                        if c.service == service.name
                        and c.after.get("version") == p["version"]
                        and c.id not in state.reverted_changes
                        and c.minute <= minute
                    ),
                    default=minute,
                )
                age = minute - state.restart_at.get(service.name, deployed)
                canary_memory = min(1024.0, base_memory + max(0, age) * leak)
                memory = canary_memory
                if canary_memory >= 1024.0:
                    oom_service_error = 0.5 * leaking_pods / service.replicas
                    error = 1 - (1 - error) * (1 - oom_service_error)
                    message = "container terminated: resident memory limit exceeded"
                    if leaking_pods >= service.replicas:
                        status = "OOMKilled"
            throttled = p["consumer_running"] < 1.0 and service.name not in state.restart_at
            processing_cap = capacity
            if throttled:
                if p["consumer_running"] <= 0.0:
                    processing_cap = 0.0
                    error, status, message = (
                        max(error, 0.4),
                        "CrashLoopBackOff",
                        "worker poll loop exited unexpectedly",
                    )
                else:
                    processing_cap = max(1.0, capacity * p["consumer_running"])
                    utilisation = rate / processing_cap
                    latency = min(10.0, p["base_latency"] / max(0.01, 1 - utilisation))
                    error = max(error, max(0.0, 1 - processing_cap / max(rate, 1.0)))
                    status = "Running"
                    if error > 0.01:
                        message = "worker dispatch latency elevated; batch commit slow"
            if service.kind == "worker":
                processing = 0.0 if status == "CrashLoopBackOff" else processing_cap
                backlog = max(
                    0.0, previous_queue[service.name] + (rate - processing) * elapsed * 60
                )
                if previous_queue[service.name] > 0 and processing > 0:
                    # Requests this minute encounter the queue present at the start of
                    # the tick. The newly integrated backlog is next minute's queue.
                    latency = min(10.0, latency + previous_queue[service.name] / processing)
            if (
                service.kind == "cache"
                and state.stampede_until >= 0
                and minute < state.stampede_until
            ):
                error, latency, message = (
                    max(error, 0.8),
                    4.0,
                    "cold cache; backing store overloaded",
                )
            own[service.name] = (
                error,
                latency,
                memory,
                backlog,
                processing_cap,
                status,
                message,
            )
            for edge in outgoing[service.name]:
                callee_error, callee_latency = next_health[edge.callee]
                timeout = max(0.001, edge.timeout_ms / 1000)
                timeout_error = max(0.0, 1 - timeout / max(callee_latency, 0.001))
                attempt_error = 1 - (1 - callee_error) * (1 - timeout_error)
                requested = rate * edge.rate_multiplier
                attempted = requested * (1 + edge.retries * attempt_error)
                # Timed-out requests retain their connection until the timeout expires.
                residence = min(callee_latency, timeout)
                concurrency = attempted * residence
                accepted = min(1.0, edge.pool_size / max(concurrency, 0.001))
                edge_error = 1 - (1 - attempt_error) * accepted
                edge_rows[(edge.caller, edge.callee)] = EdgeSample(
                    minute,
                    edge.caller,
                    edge.callee,
                    requested,
                    attempted * accepted,
                    edge_error,
                    callee_latency,
                    concurrency,
                    min(concurrency, edge.pool_size),
                    edge.timeout_ms,
                    edge.pool_size,
                    edge.retries,
                    offered_attempt_rate=attempted,
                    pool_rejection_ratio=1 - accepted,
                )
                error = 1 - (1 - error) * (1 - edge_error)
                latency = max(latency, p["base_latency"] + min(callee_latency, timeout))
            next_health[service.name] = (min(1.0, error), latency)
        next_rates = {s.name: 0.0 for s in world.services}
        next_rates["edge-gateway"] = entry
        for row in edge_rows.values():
            next_rates[row.callee] += row.attempt_rate
        return next_rates, next_health, own, edge_rows

    delta = float("inf")
    _iteration = 0
    for _iteration in range(SOLVER_FIXED_POINT_ITERATIONS):
        next_rates, health, own, edge_rows = evaluate(rates)
        delta = max(abs(next_rates[name] - rates[name]) for name in rates)
        if delta < 1e-7:
            break
        damping = 0.65 if _iteration < 500 else (0.25 if _iteration < 1200 else 0.05)
        rates = {name: (1 - damping) * rates[name] + damping * next_rates[name] for name in rates}
    if delta >= 1e-7:
        # Picard iteration can oscillate around a steep timeout/pool transition.
        # Solve the same traffic-balance equations with a damped Newton fallback.
        names = list(rates)
        for _ in range(SOLVER_NEWTON_ITERATIONS):
            next_rates, health, own, edge_rows = evaluate(rates)
            _iteration += 1
            residual = [next_rates[name] - rates[name] for name in names]
            delta = max(abs(value) for value in residual)
            if delta < 1e-7:
                break
            jacobian = [[0.0] * len(names) for _ in names]
            for column, name in enumerate(names):
                shifted = dict(rates)
                step = 1e-5 * max(1.0, abs(rates[name]))
                shifted[name] += step
                shifted_next, _, _, _ = evaluate(shifted)
                _iteration += 1
                for row, other in enumerate(names):
                    jacobian[row][column] = (
                        shifted_next[other] - shifted[other] - residual[row]
                    ) / step
            direction = _linear_solve(jacobian, [-value for value in residual])
            if direction is None:
                break
            fraction = 1.0
            for _ in range(SOLVER_LINE_SEARCH_ITERATIONS):
                candidate = {
                    name: max(0.0, rates[name] + fraction * direction[i])
                    for i, name in enumerate(names)
                }
                candidate_next, _, _, _ = evaluate(candidate)
                _iteration += 1
                candidate_delta = max(abs(candidate_next[name] - candidate[name]) for name in names)
                if candidate_delta < delta:
                    rates = candidate
                    break
                fraction *= 0.5
            else:
                break
        next_rates, health, own, edge_rows = evaluate(rates)
        _iteration += 1
        delta = max(abs(next_rates[name] - rates[name]) for name in rates)
    if delta >= 1e-5:
        raise ValueError(f"Fluid network did not converge: residual={delta:g}")
    output: list[PodSample] = []
    for service in world.services:
        p = params[service.name]
        base_memory = 150.0 + float(
            (int(service.capacity * 10) + int(service.base_latency * 1000)) % 180
        )
        (
            own_error,
            _,
            canary_memory,
            backlog,
            effective_cap,
            status,
            own_message,
        ) = own[service.name]
        error, latency = health[service.name]
        service_edges = [edge_rows[(e.caller, e.callee)] for e in outgoing[service.name]]
        pool = max((e.pool_in_use for e in service_edges), default=0.0)
        worst = max(service_edges, key=lambda e: e.error_ratio, default=None)
        for index in range(service.replicas):
            is_canary = int(p["bad_pods"]) > 0 and index >= service.replicas - int(p["bad_pods"])
            is_leaking_pod = (p["leak_mib_min"] > 0) and (
                is_canary if int(p["bad_pods"]) > 0 else True
            )
            pod_mem = canary_memory if is_leaking_pod else base_memory
            pod_status = status
            local_fault = 0.0
            message = "request completed"
            if status == "CrashLoopBackOff":
                local_fault = 0.4
                message = own_message
            if int(p["bad_pods"]) > 0 and p["pod_error"] > 0 and is_canary:
                local_fault = max(local_fault, p["pod_error"])
                message = "rpc handler returned status 500: internal execution failure"
            if is_leaking_pod and canary_memory >= 1024.0:
                local_fault = max(local_fault, 0.5)
                pod_status = "OOMKilled"
                message = "container terminated: resident memory limit exceeded"
            if (
                service.kind == "cache"
                and state.stampede_until >= 0
                and minute < state.stampede_until
            ):
                local_fault = max(local_fault, 0.8)
                message = "cold cache; backing store overloaded"
            downstream = 1 - (1 - error) / max(1e-9, 1 - own_error)
            saturation = max(0.0, 1 - effective_cap / max(rates[service.name], 1.0))
            pod_error = 1 - ((1 - saturation) * (1 - local_fault) * (1 - downstream))
            if message == "request completed":
                if worst is not None and worst.error_ratio > 0.01:
                    message = (
                        f"rpc call to {worst.callee} failed: upstream deadline or transport error"
                    )
                elif saturation > 0.01:
                    message = (
                        own_message
                        if own_message != "request completed"
                        else "request handling degraded; local worker contention"
                    )
            version = p["version"]
            if int(p["bad_pods"]) > 0 and p["pod_error"] > 0:
                if not is_canary:
                    version -= 1
            output.append(
                PodSample(
                    minute,
                    service.name,
                    f"{service.name}-7f9c4-p{index}",
                    f"v{version:g}",
                    pod_status,
                    rates[service.name] / service.replicas,
                    min(1.0, max(0.0, pod_error)),
                    latency,
                    pod_mem,
                    pool / service.replicas,
                    backlog / service.replicas,
                    message,
                )
            )
    return tuple(output), tuple(
        replace(row, solver_residual=delta, solver_iterations=_iteration + 1)
        for row in edge_rows.values()
    )


def snapshot(world: World, state: State, minute: int) -> tuple[PodSample, ...]:
    return solve(world, state, minute)[0]


def advance(world: World, state: State, minutes: int) -> None:
    for _ in range(minutes):
        state.minute += 1
        pods, edges = solve(world, state, state.minute)
        state.history.append(pods)
        state.edge_history.append(edges)

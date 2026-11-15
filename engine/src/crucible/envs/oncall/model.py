"""A deterministic fluid network: demand, queues, retries and finite connection pools.

Services are in callee-first topological order. A bounded damped fixed-point iteration couples
caller retries to downstream demand and downstream delay to caller timeouts. Entry demand does
not include retries. The per-minute samples are the only source of telemetry.
"""

import math
from dataclasses import dataclass, field, replace

from crucible.envs.oncall.world import Edge, World


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
    entry = world.entry_rate * (1 + 0.04 * math.sin(minute / 20))
    rates = {s.name: entry for s in world.services}
    rates["edge-gateway"] = entry
    health: dict[str, tuple[float, float]] = {s.name: (0.0, 0.05) for s in world.services}
    edge_rows: dict[tuple[str, str], EdgeSample] = {}
    own: dict[str, tuple[float, float, float, float, str, str]] = {}
    previous = next(
        (frame for frame in reversed(state.history) if frame and frame[0].minute <= minute), ()
    )
    previous_queue = {
        s.name: sum(p.queue_depth for p in previous if p.service == s.name) for s in world.services
    }
    elapsed = max(0, minute - previous[0].minute) if previous else 1

    # Correlated failures persist across retries (timeouts and capacity saturation are not
    # independent Bernoulli failures). Retry traffic nevertheless consumes callee capacity.
    delta = float("inf")
    _iteration = 0
    for _iteration in range(2048):
        next_health: dict[str, tuple[float, float]] = {}
        for service in world.services:
            p = params[service.name]
            capacity = max(1.0, p["capacity"] * service.replicas)
            rate = rates[service.name]
            utilisation = rate / capacity
            latency = min(10.0, p["base_latency"] / max(0.01, 1 - utilisation))
            error = max(0.0, 1 - capacity / max(rate, 1.0))
            status, message = "Running", "request completed"
            memory, backlog = 150.0, max(0.0, rate - capacity) * 60
            if p["bad_pods"]:
                error = 1 - (1 - error) * (1 - p["pod_error"] * p["bad_pods"] / service.replicas)
                message = "handler exception in deployed build"
            leak = p["leak_mib_min"]
            if leak:
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
                memory = min(1024.0, 150 + max(0, age) * leak)
                if memory >= 1024:
                    error, status, message = (
                        max(error, 0.5),
                        "OOMKilled",
                        "memory limit exceeded; restarting container",
                    )
            if not p["consumer_running"] and service.name not in state.restart_at:
                error, status, message = (
                    max(error, 0.4),
                    "CrashLoopBackOff",
                    "consumer process exited with code 1",
                )
            if service.kind == "worker":
                processing = 0.0 if status == "CrashLoopBackOff" else capacity
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
            own[service.name] = (error, latency, memory, backlog, status, message)
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
        delta = max(abs(next_rates[name] - rates[name]) for name in rates)
        damping = 0.35 if _iteration < 80 else 0.05
        rates = {name: (1 - damping) * rates[name] + damping * next_rates[name] for name in rates}
        health = next_health
        if delta < 1e-7:
            break
    if delta >= 1e-5:
        raise ValueError(f"Fluid network did not converge: residual={delta:g}")
    output: list[PodSample] = []
    for service in world.services:
        p = params[service.name]
        own_error, _, memory, backlog, status, own_message = own[service.name]
        error, latency = health[service.name]
        service_edges = [edge_rows[(e.caller, e.callee)] for e in outgoing[service.name]]
        pool = max((e.pool_in_use for e in service_edges), default=0.0)
        worst = max(service_edges, key=lambda e: e.error_ratio, default=None)
        for index in range(service.replicas):
            message, pod_error = own_message, error
            if p["bad_pods"]:
                bad = index >= service.replicas - int(p["bad_pods"])
                downstream = 1 - (1 - error) / max(1e-9, 1 - own_error)
                saturation = max(
                    0.0, 1 - p["capacity"] * service.replicas / max(rates[service.name], 1.0)
                )
                pod_error = 1 - (1 - saturation) * (1 - (p["pod_error"] if bad else 0.0)) * (
                    1 - downstream
                )
                message = "handler exception in deployed build" if bad else "request completed"
            if message == "request completed" and worst is not None and worst.error_ratio > 0.01:
                if worst.concurrency > worst.pool_size + 0.01:
                    message = (
                        f"pool exhausted calling {worst.callee}: "
                        f"{worst.pool_in_use:.1f}/{worst.pool_size:g} active"
                    )
                elif worst.latency * 1000 > worst.timeout_ms:
                    message = (
                        f"upstream timeout after {worst.timeout_ms:g}ms "
                        f"calling {worst.callee}; retries={worst.retries:g}"
                    )
                else:
                    message = f"upstream request failed calling {worst.callee}"
            elif message == "request completed" and own_error > 0.01:
                message = "service capacity saturated; request queue growing"
            version = p["version"]
            if p["bad_pods"] and index < service.replicas - int(p["bad_pods"]):
                version -= 1
            output.append(
                PodSample(
                    minute,
                    service.name,
                    f"{service.name}-7f9c4-p{index}",
                    f"v{version:g}",
                    status,
                    rates[service.name] / service.replicas,
                    min(1.0, max(0.0, pod_error)),
                    latency,
                    memory,
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

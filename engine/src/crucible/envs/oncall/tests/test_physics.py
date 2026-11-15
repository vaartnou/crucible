"""Causal invariants: interventions alter network parameters, not fault-label switches."""

from dataclasses import replace

import pytest

from crucible.envs.oncall.faults import apply_action
from crucible.envs.oncall.model import State, advance, effective_edges, effective_parameters, solve
from crucible.envs.oncall.world import Edge, Service, World, make_world


def network(
    *,
    timeout: float = 2000,
    pool: float = 4096,
    retries: float = 0,
    demand: float = 100,
    capacity: float = 100,
) -> World:
    return World(
        services=(
            Service("db", "db", (), capacity=capacity, base_latency=0.05),
            Service("edge-gateway", "http", ("db",), capacity=1000, base_latency=0.01),
        ),
        changes=(),
        culprit="",
        fault="",
        change_id="",
        fingerprint="physics",
        edges=(Edge("edge-gateway", "db", timeout_ms=timeout, pool_size=pool, retries=retries),),
        entry_rate=demand,
    )


def error(world: World, name: str) -> float:
    pods, _ = solve(world, State(), 0)
    rows = [p.error_ratio for p in pods if p.service == name]
    return sum(rows) / len(rows)


def latency(world: World, name: str) -> float:
    pods, _ = solve(world, State(), 0)
    return max(p.p99_latency for p in pods if p.service == name)


def test_healthy_network_conserves_demand() -> None:
    pods, edges = solve(network(), State(), 0)
    assert all(p.error_ratio == 0 for p in pods)
    assert sum(p.request_rate for p in pods if p.service == "db") == pytest.approx(100)
    assert edges[0].attempt_rate == pytest.approx(100)
    assert edges[0].concurrency == pytest.approx(100 * edges[0].latency)
    assert edges[0].solver_residual < 1e-5


def test_timeout_fails_caller_with_healthy_callee() -> None:
    world = network(timeout=10)
    assert error(world, "edge-gateway") > 0.8
    assert error(world, "db") == 0
    assert error(network(timeout=2000), "edge-gateway") == 0


def test_pool_limit_rejects_at_caller_not_database() -> None:
    pods, edges = solve(network(pool=2), State(), 0)
    edge = edges[0]
    assert edge.pool_rejection_ratio > 0.5
    assert edge.pool_in_use == 2
    assert edge.attempt_rate < edge.offered_attempt_rate
    assert all(p.error_ratio == 0 for p in pods if p.service == "db")
    assert error(network(pool=2), "edge-gateway") > 0.5


def test_retries_feed_back_into_callee_load() -> None:
    ordinary = network(timeout=50)
    retried = network(timeout=50, retries=5)
    base_pods, base_edges = solve(ordinary, State(), 0)
    storm_pods, storm_edges = solve(retried, State(), 0)
    assert storm_edges[0].attempt_rate > base_edges[0].attempt_rate * 2
    assert error(retried, "db") > error(ordinary, "db")
    assert latency(retried, "db") > latency(ordinary, "db")
    assert sum(p.request_rate for p in storm_pods if p.service == "edge-gateway") == pytest.approx(
        sum(p.request_rate for p in base_pods if p.service == "edge-gateway")
    )


def test_load_and_capacity_monotonicity() -> None:
    assert latency(network(demand=200), "db") > latency(network(demand=100), "db")
    assert error(network(demand=400), "db") > error(network(demand=200), "db")
    assert latency(network(demand=200, capacity=200), "db") < latency(
        network(demand=200, capacity=100), "db"
    )


@pytest.mark.parametrize("seed", range(40))
def test_generated_world_repair_is_parameter_intervention(seed: int) -> None:
    world = make_world(seed, "physics-holdout")
    state = State()
    before_pods, before_edges = solve(world, state, 5)
    operation = (
        "config" if world.fault in ("config_push", "pool_exhaustion", "retry_storm") else "undo"
    )
    apply_action(world, state, operation, world.culprit, world.change_id)
    state.repaired = False  # This legacy bookkeeping flag must have no effect on dynamics.
    after_pods, after_edges = solve(world, state, 5)
    assert sum(p.error_ratio for p in after_pods) < sum(p.error_ratio for p in before_pods)
    assert all(p.error_ratio == 0 for p in after_pods)
    assert all(e.solver_residual < 1e-5 for e in (*before_edges, *after_edges))
    if operation == "config":
        assert effective_edges(world, state, 5) != effective_edges(world, State(), 5)
    if world.fault == "pool_exhaustion":
        edge = next(e for e in before_edges if e.caller == world.culprit)
        assert edge.pool_rejection_ratio > 0
        assert all(p.error_ratio == 0 for p in before_pods if p.service == edge.callee)
    # Renaming the private diagnostic label cannot alter model output.
    assert solve(replace(world, fault="unrelated", culprit="unknown"), state, 5) == (
        after_pods,
        after_edges,
    )


@pytest.mark.parametrize("seed", range(20))
def test_change_before_values_match_real_effective_history(seed: int) -> None:
    world = make_world(seed, "history-audit")
    applied: list[str] = []
    for change in world.changes:
        state = State(reverted_changes={c.id for c in world.changes if c.id not in applied})
        if change.callee is None:
            previous = effective_parameters(world, state, change.minute)[change.service]
        else:
            edge = next(
                e
                for e in effective_edges(world, state, change.minute)
                if e.caller == change.service and e.callee == change.callee
            )
            previous = {
                "timeout_ms": edge.timeout_ms,
                "pool_size": edge.pool_size,
                "retries": edge.retries,
                "rate_multiplier": edge.rate_multiplier,
            }
        for key, value in change.before.items():
            assert previous[key] == pytest.approx(value), (change, previous)
        applied.append(change.id)


def test_recovered_consumer_drains_existing_queue_instead_of_erasing_it() -> None:
    world = next(
        make_world(seed, "queue-drain")
        for seed in range(100)
        if make_world(seed, "queue-drain").fault == "queue_backlog"
    )
    state = State(minute=-1)
    state.history.append(solve(world, state, -1)[0])
    advance(world, state, 6)
    queued = sum(p.queue_depth for p in state.history[-1] if p.service == world.culprit)
    assert queued > 0
    apply_action(world, state, "undo", world.culprit, world.change_id)
    immediate = solve(world, state, state.minute)[0]
    assert sum(p.queue_depth for p in immediate if p.service == world.culprit) == queued
    advance(world, state, 1)
    draining = sum(p.queue_depth for p in state.history[-1] if p.service == world.culprit)
    assert 0 < draining < queued
    advance(world, state, 30)
    assert sum(p.queue_depth for p in state.history[-1] if p.service == world.culprit) == 0
    assert state.penalty == 0


def test_rollback_after_restart_is_not_penalized_as_a_wrong_action() -> None:
    world = next(
        make_world(seed, "durability")
        for seed in range(100)
        if make_world(seed, "durability").fault == "memory_leak"
    )
    state = State()
    apply_action(world, state, "restart", world.culprit, "")
    before = state.penalty
    apply_action(world, state, "undo", world.culprit, world.change_id)
    assert state.penalty == before


@pytest.mark.parametrize("seed", range(20))
def test_convergence_across_episode_and_interventions(seed: int) -> None:
    world = make_world(seed, "convergence-audit")
    for minute in (-30, 0, 5, 30, 90, 150, 180):
        for reverted in (set[str](), {world.change_id}, {c.id for c in world.changes[::2]}):
            _, edges = solve(world, State(reverted_changes=reverted), minute)
            assert all(e.solver_residual < 1e-5 for e in edges)
            assert all(e.solver_iterations <= 2048 for e in edges)


def test_repaired_retry_storm_remains_numerically_stable_while_queue_drains() -> None:
    world = make_world(12, "integration-fault-coverage")
    assert world.fault == "retry_storm"
    state = State(minute=-31)
    advance(world, state, 47)  # Repair at minute 16, matching the failing public-policy trace.
    apply_action(world, state, "config", world.culprit, world.change_id)
    advance(world, state, 80)
    assert all(edge.solver_residual < 1e-5 for frame in state.edge_history for edge in frame)
    assert all(p.error_ratio == 0 for p in state.history[-1])
    assert all(p.queue_depth == 0 for p in state.history[-1])


@pytest.mark.parametrize("seed", range(12))
def test_safe_adverse_direction_changes_are_causally_healthy(seed: int) -> None:
    world = make_world(seed, "safe-config-audit")
    state = State(reverted_changes={world.change_id})
    pods, _ = solve(world, state, 5)
    assert all(p.error_ratio == 0 for p in pods)
    safe = [c for c in world.changes if c.id != world.change_id and c.callee is not None]
    assert any(c.after.get("timeout_ms", 2000) < c.before.get("timeout_ms", 0) for c in safe)
    assert any(c.after.get("pool_size", 2000) < c.before.get("pool_size", 0) for c in safe)
    assert any(c.after.get("retries", 0) > c.before.get("retries", 100) for c in safe)
    if next(s for s in world.services if s.name == world.culprit).dependencies:
        assert len([c for c in safe if c.service == world.culprit]) >= 3

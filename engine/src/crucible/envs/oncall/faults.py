"""Interventions update effective configuration; repairs are not simulation shortcuts."""

from dataclasses import replace

from crucible.envs.oncall.model import State, advance, effective_edges, effective_parameters
from crucible.envs.oncall.world import World


def _badness(world: World, state: State) -> float:
    # The integrator needs only the preceding immutable frame. Avoid cloning the
    # entire episode for each counterfactual; advance never mutates intervention maps.
    probe = replace(state, history=list(state.history[-1:]), edge_history=[])
    advance(world, probe, 30)
    return sum(p.error_ratio + max(0.0, p.p99_latency - 1.0) for p in probe.history[-1])


def _same_dynamics(world: World, before: State, after: State) -> bool:
    """Prove an action cannot change the thirty-minute physical projection.

    Version-only changes are inert when the versions have identical healthy behavior.
    Restart timestamps matter only to active leaks and stopped consumers. Future scheduled
    changes could make a currently inert restart relevant, so those retain the full check.
    """
    if any(before.minute < c.minute <= before.minute + 30 for c in world.changes):
        return False
    if before.stampede_until != after.stampede_until:
        return False
    old = effective_parameters(world, before, before.minute)
    new = effective_parameters(world, after, after.minute)
    for parameters in (old, new):
        for values in parameters.values():
            if not values["leak_mib_min"]:
                del values["version"]
    if old != new:
        return False
    if effective_edges(world, before, before.minute) != effective_edges(world, after, after.minute):
        return False
    for name, parameters in old.items():
        if parameters["leak_mib_min"] or not parameters["consumer_running"]:
            if before.restart_at.get(name) != after.restart_at.get(name):
                return False
    return True


def apply_action(world: World, state: State, operation: str, target: str, revision: str) -> None:
    before_state = replace(
        state, reverted_changes=set(state.reverted_changes), restart_at=dict(state.restart_at)
    )
    if operation in ("config", "undo"):
        kind = "config" if operation == "config" else "deploy"
        candidates = [
            c
            for c in world.changes
            if c.service == target
            and c.kind == kind
            and c.minute <= state.minute
            and c.id not in state.reverted_changes
        ]
        change = next((c for c in candidates if c.id == revision), None)
        if change is None and operation == "undo" and revision.startswith("deploy/"):
            change = max(candidates, key=lambda c: (c.minute, c.id), default=None)
        if change is not None:
            state.reverted_changes.add(change.id)
            state.repaired = world.change_id in state.reverted_changes
            state.deployment_rolled_back = state.repaired and operation == "undo"
    elif operation == "restart":
        parameters = effective_parameters(world, state, state.minute)[target]
        state.restart_at[target] = state.minute
        if parameters["leak_mib_min"]:
            state.restart_until = state.minute + 25
        if target == "session-cache":
            state.stampede_until = state.minute + 20
            state.penalty += 0.15
    if _same_dynamics(world, before_state, state) or (
        _badness(world, state) >= _badness(world, before_state) - 1e-8
    ):
        state.penalty += 0.07

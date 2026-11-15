"""The action projection fast path must preserve the complete slow-path episode state."""

from copy import deepcopy

import pytest

from crucible.envs.oncall import faults
from crucible.envs.oncall.model import State, advance
from crucible.envs.oncall.world import FAULTS, make_world

# These tests deliberately force the private slow evaluator to establish equivalence.
# pyright: reportPrivateUsage=false


def force_slow(*_: object) -> bool:
    return False


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("salt", ("projection-a", "projection-b", "projection-c"))
def test_fast_path_matches_paired_projections(
    fault: str, salt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = next(
        make_world(seed, salt) for seed in range(100) if make_world(seed, salt).fault == fault
    )
    initial = State(minute=-31)
    advance(world, initial, 36)
    healthy = next(s.name for s in world.services if s.name != world.culprit and s.kind != "cache")
    benign = next(c for c in world.changes if c.service == healthy and c.kind == "deploy")
    configuration = next(c for c in world.changes if c.kind == "config" and c.id != world.change_id)
    operation = "config" if fault in ("config_push", "pool_exhaustion", "retry_storm") else "undo"
    sequences = (
        ((operation, world.culprit, world.change_id),),
        (("undo", healthy, benign.id),),
        (("config", configuration.service, configuration.id),),
        (("restart", healthy, ""),),
        (("restart", "session-cache", ""),),
        (("restart", world.culprit, ""), (operation, world.culprit, world.change_id)),
        (("undo", healthy, benign.id), ("undo", healthy, benign.id)),
    )
    for actions in sequences:
        fast, slow = deepcopy(initial), deepcopy(initial)
        for action, target, revision in actions:
            faults.apply_action(world, fast, action, target, revision)
            with monkeypatch.context() as patch:
                patch.setattr(faults, "_same_dynamics", force_slow)
                faults.apply_action(world, slow, action, target, revision)
            assert fast == slow
        advance(world, fast, 1)
        advance(world, slow, 1)
        assert fast == slow


def test_inert_actions_skip_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world(0, "inert-cost")
    healthy = next(s.name for s in world.services if s.name != world.culprit and s.kind != "cache")
    change = next(c for c in world.changes if c.service == healthy and c.kind == "deploy")
    state = State()

    def must_not_project(*_: object) -> float:
        raise AssertionError("An inert action must not simulate two additional thirty-minute paths")

    monkeypatch.setattr(faults, "_badness", must_not_project)
    faults.apply_action(world, state, "restart", healthy, "")
    faults.apply_action(world, state, "undo", healthy, change.id)
    assert state.penalty == pytest.approx(0.14)

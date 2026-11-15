"""Cache performance must not change chronological counterfactual scores."""

from dataclasses import replace

import pytest

# Cache internals are inspected only to test invalidation and exact equivalence.
# pyright: reportPrivateUsage=false
from crucible.envs.oncall import grader
from crucible.envs.oncall.model import State, advance
from crucible.envs.oncall.world import HORIZON, World, make_world


def direct_baseline(world: World, horizon: int) -> float:
    state = State(minute=-31)
    advance(world, state, horizon + 31)
    return sum(grader.loss(frame) for frame in state.history if frame[0].minute > 5)


def test_baseline_cache_reuses_complete_world_and_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world(17, "baseline-cache")
    changed = replace(world, entry_rate=world.entry_rate * 0.8)
    assert changed.fingerprint == world.fingerprint
    expected = direct_baseline(world, HORIZON)
    expected_changed = direct_baseline(changed, HORIZON)
    expected_long = direct_baseline(world, HORIZON + 30)
    calls = 0

    def counted(world: World, state: State, minutes: int) -> None:
        nonlocal calls
        calls += 1
        advance(world, state, minutes)

    monkeypatch.setattr(grader, "advance", counted)
    grader._BASELINE_CACHE.clear()
    assert grader._baseline_loss(world, HORIZON) == expected
    assert grader._baseline_loss(replace(world), HORIZON) == expected
    assert calls == 1
    assert grader._baseline_loss(changed, HORIZON) == expected_changed
    assert calls == 2
    assert grader._baseline_loss(world, HORIZON + 30) == expected_long
    assert calls == 3


def test_nested_change_mutation_invalidates_cache() -> None:
    world = make_world(19, "nested-cache")
    grader._BASELINE_CACHE.clear()
    assert grader._baseline_loss(world, HORIZON) == direct_baseline(world, HORIZON)
    before_size = len(grader._BASELINE_CACHE)
    change = next(c for c in world.changes if c.after)
    key = next(iter(change.after))
    change.after[key] += 0.25
    assert grader._baseline_loss(world, HORIZON) == direct_baseline(world, HORIZON)
    assert len(grader._BASELINE_CACHE) == before_size + 1


def test_full_score_exactly_matches_uncached_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world(12, "score-equivalence")
    state = State(minute=-31)
    advance(world, state, 36)
    state.reverted_changes.add(world.change_id)
    advance(world, state, 35)
    state.report = {"service": world.culprit, "fault_kind": world.fault}
    cached = grader.grade(world, state)
    monkeypatch.setattr(grader, "_baseline_loss", direct_baseline)
    assert grader.grade(world, state) == cached


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tiny bound exercises eviction without running hundreds of counterfactuals.
    monkeypatch.setattr(grader, "_BASELINE_CACHE_LIMIT", 2)
    grader._BASELINE_CACHE.clear()
    world = make_world(3, "bounded-cache", difficulty=1)
    for horizon in (10, 11, 12):
        assert grader._baseline_loss(world, horizon) == direct_baseline(world, horizon)
    assert len(grader._BASELINE_CACHE) == 2
    grader._BASELINE_CACHE.clear()

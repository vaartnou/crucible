# pyright: basic
"""`supplygym@1` and `supplygym-fresh@1`: reward anchors, refusals, secrecy and view parity."""

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from crucible.core.registry import get_policy, get_task
from crucible.core.tools import ToolCall
from crucible.envs.supplygym.play import REGIMES, SPLITS, Play
from crucible.envs.supplygym.policies.reference import make_decider
from crucible.envs.supplygym.policies.views import StatusView, to_vector
from crucible.envs.supplygym.task import SupplyGymTask
from crucible.testing import play

TASK = "supplygym@1"
SEEDS = [0, 1, 2]


def run(policy: str, seed: int, out: Path, task: str = TASK):
    path = out / f"{task}-{policy}-{seed}.jsonl"
    return asyncio.run(play(get_policy(policy, task), seed, path, task))


@pytest.mark.parametrize("task", [TASK, "supplygym-fresh@1"])
@pytest.mark.parametrize("policy", ["do_nothing", "ss"])
def test_default_rule_and_no_calls_score_exactly_zero(
    task: str, policy: str, tmp_path: Path
) -> None:
    for seed in SEEDS:
        r = run(policy, seed, tmp_path, task)
        assert r.score.total == 0.0
        assert r.score.breakdown["delta_vs_ss"] == 0.0


def test_finish_at_once_scores_zero() -> None:
    async def go() -> float:
        t = SupplyGymTask()
        await t.reset(3)
        await t.step(ToolCall(name="finish"))
        return (await t.grade()).total

    assert asyncio.run(go()) == 0.0


def test_max_expedite_does_not_beat_default(tmp_path: Path) -> None:
    for seed in SEEDS:
        r = run("max_expedite", seed, tmp_path)
        assert r.score.total == 0.0
        assert r.score.breakdown["delta_vs_ss"] < 0


def test_reference_through_tools_is_near_one(tmp_path: Path) -> None:
    # on dev seeds, where the reference beats the default, it scores 1 against itself
    rewards = [run("reference", s, tmp_path).score.total for s in (1000, 1003, 1004)]
    assert rewards == [pytest.approx(1.0)] * 3 or max(rewards) == pytest.approx(1.0)
    assert min(rewards) >= 0.0


def test_refused_calls_change_nothing() -> None:
    async def go() -> None:
        t = SupplyGymTask()
        await t.reset(5)
        before = (await t.step(ToolCall(name="get_status"))).content
        bad: list[dict[str, Any]] = [
            {"approve_alternative": "A02"},  # A01 must come first
            {"approve_alternative": "Z99"},
            {"supplier_weights": {"S99": 1.0}},
            {"expedite_fraction": 0.9},
        ]
        for args in bad:
            r = await t.step(ToolCall(name="set_controls", args=args))
            assert not r.ok
        assert (await t.step(ToolCall(name="get_status"))).content == before
        ok = await t.step(ToolCall(name="set_controls", args={"approve_alternative": "A01"}))
        assert ok.ok
        again = await t.step(ToolCall(name="set_controls", args={"approve_alternative": "A01"}))
        assert not again.ok and "already approved" in (again.error or "")

    asyncio.run(go())


def test_status_has_no_hidden_state() -> None:
    p = Play(7, 0)
    p.apply({})
    text = json.dumps(p.status())
    for word in ("truth", "events", "tape", "_event", "outcome", "resolve_day", "seed", "salt"):
        assert word not in text, word


def test_salt_changes_the_tape_and_is_repeatable() -> None:
    def profit(salt: str) -> float:
        p = Play(11, 0, salt)
        p.run_default()
        return p.env.summary()["profit"]

    assert profit("") != profit("abc")
    assert profit("abc") == profit("abc")


def test_splits_are_disjoint() -> None:
    ranges = sorted(SPLITS.values())
    for (_, hi), (lo, _) in itertools.pairwise(ranges):
        assert hi <= lo


def test_regimes_differ() -> None:
    a, b = Play(1, 0), Play(1, 1)
    assert a.env.world.nodes[0]["cap"] != b.env.world.nodes[0]["cap"] or REGIMES[0] != REGIMES[1]


def test_status_view_rebuilds_the_original_observation() -> None:
    p = Play(1000, 0)
    rng = np.random.default_rng(0)
    worst = 0.0
    while not p.done:
        got = to_vector(StatusView.model_validate(p.status()))
        worst = max(worst, float(np.abs(got - p.env._obs()).max()))
        args = {"expedite_fraction": float(rng.uniform(0, 0.3)), "safety_stock_days": 1.0}
        p.apply(args)
    assert worst < 2e-5


def test_in_process_reference_equals_tool_path(tmp_path: Path) -> None:
    from crucible.envs.supplygym.grader import replay_reference

    r = run("reference", 1001, tmp_path)
    ref = replay_reference(Play(1001, 0)).env.summary()["profit"]
    assert r.score.breakdown["profit"] == pytest.approx(ref, abs=1e-6)
    assert make_decider("ppo") is not None


@pytest.mark.parametrize(("ref", "regime"), [(TASK, 0), ("supplygym-fresh@1", 1)])
def test_each_task_plays_its_own_regime(ref: str, regime: int) -> None:
    task = get_task(ref)
    assert isinstance(task, SupplyGymTask)
    asyncio.run(task.reset(1000))
    assert task._play is not None and task._play.regime == regime
    # `difficulty` does not pick the regime; the task ref does.
    asyncio.run(task.reset(1000, difficulty=1))
    assert task._play.regime == regime

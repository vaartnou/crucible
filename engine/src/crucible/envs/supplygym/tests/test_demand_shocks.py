# pyright: basic
"""Optional shocks preserve the standard world and expose only realised demand."""

import asyncio

import numpy as np
import pytest

from crucible.core.tools import ToolCall
from crucible.envs.supplygym.grader import grade_play, replay_default, replay_reference
from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.task import SupplyGymFreshTask, SupplyGymTask
from crucible.envs.supplygym.world import make_tape, make_world, tape_digest


# Measured on supplygym-task 1e830d4, before implementing shocks. One week at 0.9x
# orders, then default completion; pin raw anchors too, since clipping can hide drift.
@pytest.mark.parametrize(
    ("regime", "seed", "digest", "profit", "ss", "ref", "reward"),
    [
        (0, 0, "eb8fd95f358a0b1c", 156883.78, 156875.73, 157517.48, 0.00805),
        (0, 1000, "e677bc7170ddb254", 108037.06, 108096.42, 104496.25, 0.0),
        (0, 10000, "695edb81b9bc1273", 128133.23, 128108.79, 132795.04, 0.005215257402),
        (1, 0, "58aff1ea702b1f60", 172329.58, 172310.97, 152293.96, 0.01861),
        (1, 1000, "d88fdf049e0832e6", 146616.59, 146631.75, 115485.26, 0.0),
        (1, 10000, "6766798fa7176f2d", 159486.72, 159494.63, 155621.6, 0.0),
    ],
)
def test_off_preserves_pre_change_world_and_scores(regime, seed, digest, profit, ss, ref, reward):
    implicit = Play(seed, regime)
    explicit = Play(seed, regime, demand_shocks=False)
    assert implicit.status() == explicit.status()
    assert "observed_demand" not in implicit.status()
    for p in (implicit, explicit):
        assert p.env.tape is not None
        assert tape_digest(p.env.tape) == digest
        p.apply({"order_multiplier": 0.9})
        score = grade_play(p)
        assert score.total == pytest.approx(reward, abs=1e-9)
        assert [score.breakdown[k] for k in ("profit", "profit_ss", "profit_ref")] == (
            pytest.approx([profit, ss, ref], abs=1e-6)
        )


@pytest.mark.parametrize("salt", ["", "private-test-salt"])
def test_shocks_are_repeatable_salted_and_change_only_demand(salt):
    world = make_world()
    before = make_tape(world, 1000, salt=salt)
    after = make_tape(world, 1000, salt=salt, demand_shocks=True)
    again = make_tape(world, 1000, salt=salt, demand_shocks=True)
    assert tape_digest(after) == tape_digest(again)
    assert np.any(after.demand > before.demand)
    assert np.any(after.demand < before.demand)
    assert np.all(after.demand >= 0)
    assert np.issubdtype(after.demand.dtype, np.integer)
    for field in ("H", "risk_noise", "risk_stale"):
        np.testing.assert_array_equal(getattr(before, field), getattr(after, field))
    for field in ("events", "rumors", "news"):
        assert getattr(before, field) == getattr(after, field)
    np.testing.assert_array_equal(before.demand[:14], after.demand[:14])
    changed = after.demand != before.demand
    edges = np.diff(np.r_[False, changed, False].astype(int))
    lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    assert all(14 <= n <= 28 for n in lengths)
    other_salt = make_tape(world, 1000, salt=salt + "x", demand_shocks=True)
    assert tape_digest(after) != tape_digest(other_salt)


def test_public_demand_is_completed_demand_even_during_stockouts():
    p = Play(1000, demand_shocks=True)
    assert p.env.tape is not None
    prices = p.env.price.copy()
    assert p.status()["observed_demand"] == []
    saw_stockout = False
    while not p.done:
        week = p.apply({})
        saw_stockout |= week.lost_units > 0
        status = p.status()
        assert status["last_week"]["week"] == p.weeks
        rows = status["observed_demand"]
        days = list(range(max(0, p.env.day - 28), p.env.day))
        assert rows == [{"day": d, "units": int(p.env.tape.demand[d])} for d in days]
        assert all(set(row) == {"day", "units"} for row in rows)
        np.testing.assert_array_equal(p.env.price, prices)
        assert [s["price"] for s in status["suppliers"]] == prices.tolist()
    assert saw_stockout
    # A caller cannot change the simulator's observations through a returned dict.
    p.status()["observed_demand"][0]["units"] = -1
    assert p.status()["observed_demand"][0]["units"] >= 0
    p.env.reset(seed=1000)
    assert p.status()["observed_demand"] == []


@pytest.mark.parametrize("regime", [0, 1])
def test_grader_replays_share_the_shocked_tape(regime):
    p = Play(1000, regime, "private-test-salt", demand_shocks=True)
    assert p.env.tape is not None
    for replay in (replay_default(p), replay_reference(p)):
        assert replay.env.tape is not None
        assert replay.env.demand_shocks
        assert tape_digest(replay.env.tape) == tape_digest(p.env.tape)
    while not p.done:
        p.apply({})
    score = grade_play(p)
    assert score.total == 0
    assert score.breakdown["delta_vs_ss"] == 0


@pytest.mark.parametrize("task_type", [SupplyGymTask, SupplyGymFreshTask])
def test_task_flag_and_finish_preserve_zero_anchor(task_type):
    async def go():
        t = task_type(demand_shocks=True)
        obs = await t.reset(1000, salt="private-test-salt")
        assert "Demand shocks are enabled" in obs.text
        result = await t.step(ToolCall(name="get_status"))
        assert result.content["observed_demand"] == []
        await t.step(ToolCall(name="finish"))
        score = await t.grade()
        assert score.total == 0
        assert score.breakdown["delta_vs_ss"] == 0

    asyncio.run(go())

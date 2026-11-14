# pyright: basic
"""Hard evaluation stays isolated and preserves observable operational warnings."""

import subprocess
import sys

import numpy as np
import pytest

from crucible.envs.supplygym.grader import grade_play, replay_default, replay_reference
from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.sim import SupplyEnv
from crucible.envs.supplygym.world import make_tape, make_world, tape_digest


def test_standard_registration_is_unchanged() -> None:
    code = (
        "from crucible.core.registry import list_tasks; "
        "assert not any('hard' in t for t in list_tasks())"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_hard_task_gate_and_policy_aliases() -> None:
    # A subprocess keeps opt-in task registrations out of the standard golden suite.
    code = """
import asyncio
from crucible.core.registry import get_task, get_policy
from crucible.envs.supplygym.hard import enable_hard_tier
enable_hard_tier(['ss', 'reference'])
enable_hard_tier(['ss', 'reference'])
for ref in ('supplygym-hard@1', 'supplygym-fresh-hard@1'):
    task = get_task(ref)
    asyncio.run(task.reset(30000))
    assert task._play.hard
    assert get_policy('reference', ref).name == 'reference'
    assert 'late orders' in task.instruction()
    try:
        asyncio.run(task.reset(20000))
    except ValueError:
        pass
    else:
        raise AssertionError('training seed accepted')
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_hard_tape_is_reproducible_and_adds_overlaps_and_false_news() -> None:
    world = make_world()
    injected = 0
    for seed in range(30000, 30100):
        standard = make_tape(world, seed)
        hard = make_tape(world, seed, hard=True)
        assert hard.risk_lag in (7, 14)
        assert tape_digest(hard) == tape_digest(make_tape(world, seed, hard=True))
        np.testing.assert_array_equal(standard.demand, hard.demand)
        np.testing.assert_array_equal(standard.risk_noise, hard.risk_noise)
        if len(hard.events) > len(standard.events):
            injected += 1
            assert any(
                a["start"] == b["start"] and a["id"] != b["id"]
                for a in hard.events
                for b in hard.events
            )
        assert sum(n["_event"] is None for n in hard.news) > sum(
            n["_event"] is None for n in standard.news
        )
    assert 10 <= injected <= 30


def test_existing_simulator_already_has_overlaps() -> None:
    events = make_tape(make_world(), 30000).events
    assert any(
        a["start"] < b["end"] and b["start"] < a["end"]
        for a in events
        for b in events
        if a["id"] != b["id"]
    )


@pytest.mark.parametrize("lag", [7, 14])
def test_lagged_risk_and_late_order_warning(lag: int) -> None:
    env = SupplyEnv(hard=True, decision_interval=1)
    env.reset(seed=30000)
    assert env.tape is not None
    env.tape.risk_lag = lag
    env.tape.risk_noise[:] = 0
    env.tape.risk_stale[:] = False
    env.tape.H[:] = 1
    env.tape.H[7:, 0] = 0  # controlled onset at day 7
    env.day = 7
    env._risk_update()
    assert env.risk[0] == pytest.approx(0.15)
    env.day = 7 + lag
    env._risk_update()
    assert env.risk[0] == pytest.approx(0.85)
    # Orders and acknowledgements still use actual health during the risk blind window.
    env.day = 7
    env.onhand = 0
    for _ in range(8):
        env.step(np.zeros(17))
    assert env.fill[0] < 1


def test_hard_replays_keep_the_tier_and_salt() -> None:
    p = Play(30000, 1, "test-salt", hard=True)
    for replay in (replay_default(p), replay_reference(p)):
        assert replay.hard
        assert replay.env.tape is not None and p.env.tape is not None
        assert tape_digest(replay.env.tape) == tape_digest(p.env.tape)
    assert grade_play(p).total == 0


def test_hard_rejects_old_seed_banks() -> None:
    with pytest.raises(ValueError, match="30000"):
        Play(1000, hard=True)


def test_evaluator_rejects_training_split() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "crucible.envs.supplygym.evaluate",
            "--tier",
            "hard",
            "--split",
            "train",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --split fresh" in result.stderr

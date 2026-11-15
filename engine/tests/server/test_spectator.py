"""The spectator reads completed public decisions and the authoritative ledger."""

import json
from pathlib import Path

import pytest

from crucible.core.registry import get_task
from crucible.core.tools import ToolCall
from crucible.server.spectator import Spectator


async def test_live_records_and_final_accounting(tmp_path: Path) -> None:
    task = get_task("supplygym-cala@2")
    await task.reset(42, salt="private-salt")
    path = tmp_path / "events.jsonl"
    feed = Spectator(path, task, 42, "private-salt", "run", "strategy")

    bad = ToolCall(name="set_controls", args={"safety_stock_days": 9})
    before = feed.before_step(bad)
    feed.after_step(bad, await task.step(bad), before)
    assert not path.exists()

    for week in range(1, 3):
        call = ToolCall(name="set_controls", args={})
        before = feed.before_step(call)
        result = await task.step(call)
        feed.after_step(call, result, before)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == week
        assert records[-1]["week"] == week
        assert records[-1]["end_day"] == week * 7
        assert records[-1]["observation"]["day"] == (week - 1) * 7
        assert (
            records[-1]["cumulative_operating_surplus"]
            == (await task.grade()).breakdown["operating_surplus"]
        )

    feed.finalize("policy_stop", await task.grade())
    feed.finalize("policy_stop", await task.grade())
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 3
    assert records[-1]["event"] == "final"
    assert not records[-1]["completed"]
    assert "private-salt" not in path.read_text()


async def test_comparison_identity_and_validation(tmp_path: Path) -> None:
    task = get_task("supplygym-cala@2")
    await task.reset(5, salt="secret")
    a = Spectator(tmp_path / "a", task, 5, "secret", "a", "one")
    b = Spectator(tmp_path / "b", task, 5, "secret", "b", "two")
    c = Spectator(tmp_path / "c", task, 6, "secret")
    assert a.metadata["scenario_comparison_id"] == b.metadata["scenario_comparison_id"]
    assert a.metadata["scenario_comparison_id"] != c.metadata["scenario_comparison_id"]
    with pytest.raises(ValueError, match="salt"):
        Spectator(tmp_path / "d", task, 5, "")


async def test_v3_final_records_actual_scorer_and_bound(tmp_path: Path) -> None:
    task = get_task("supplygym-cala@3")
    await task.reset(5, salt="secret")
    path = tmp_path / "v3.jsonl"
    feed = Spectator(path, task, 5, "secret")
    score = await task.grade()
    feed.finalize("policy_stop", score)
    event = json.loads(path.read_text())
    assert event["task"] == "supplygym-cala@3"
    assert event["scorer_version"] == task.scoring_hash
    assert (
        event["verified_accounting"]["surplus_upper_bound"]
        == score.breakdown["surplus_upper_bound"]
    )
    assert event["simulator_version"]["implementation_sha256"]

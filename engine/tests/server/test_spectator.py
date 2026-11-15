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


async def test_simulator_version_normalizes_crlf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import crucible.server.spectator as spectator_mod

    task = get_task("supplygym-cala@3")
    await task.reset(5, salt="secret")
    lf_root = tmp_path / "lf" / "envs" / "supplygym"
    crlf_root = tmp_path / "crlf" / "envs" / "supplygym"
    lf_root.mkdir(parents=True)
    crlf_root.mkdir(parents=True)
    for name in ("sim.py", "cala_sim.py", "play.py", "state.py"):
        (lf_root / name).write_bytes(f"# {name}\nx = 1\n".encode())
        (crlf_root / name).write_bytes(f"# {name}\r\nx = 1\r\n".encode())
    monkeypatch.setattr(spectator_mod, "__file__", str(tmp_path / "lf" / "server" / "spectator.py"))
    lf_feed = Spectator(tmp_path / "lf.jsonl", task, 5, "secret")
    monkeypatch.setattr(
        spectator_mod, "__file__", str(tmp_path / "crlf" / "server" / "spectator.py")
    )
    crlf_feed = Spectator(tmp_path / "crlf.jsonl", task, 5, "secret")
    assert lf_feed.metadata["simulator_version"] == crlf_feed.metadata["simulator_version"]

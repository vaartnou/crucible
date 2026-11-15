"""Protocol and replay tests, using an in-memory provider and a known small world."""

import json
from pathlib import Path

import pytest

from crucible.core.jobs import Job
from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter
from crucible.feedback.regrade import regrade
from tests.fixtures.guess.task import GuessNumber


async def test_replay_preserves_world_and_detects_divergence(tmp_path: Path) -> None:
    path = tmp_path / "episode.jsonl"
    task = GuessNumber()
    session = EpisodeSession(task, 42, TranscriptWriter(path), salt="test-salt")
    await session.start(policy_name="test", job=Job(task="guess@1", seed=42, policy="test"))
    await session.step(ToolCall(name="guess", args={"n": 50}))
    await session.step(ToolCall(name="give_up"))
    await session.close()
    with pytest.raises(ValueError, match="Salted transcript"):
        await regrade(path)
    result = await regrade(path, salt="test-salt")
    assert result["comparable"] is True
    assert result["reward"] == result["old_reward"]
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["result"]["content"] = "tampered observation"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = await regrade(path, salt="test-salt")
    assert result["comparable"] is False
    assert result["first_divergence"] == 0


def test_comparison_rejects_mismatched_simulator_and_provider_failures(tmp_path: Path) -> None:
    from crucible.feedback.compare import compare

    bots, agent = tmp_path / "bots", tmp_path / "agent"
    bots.mkdir()
    agent.mkdir()
    identity = {"task": "oncall@1", "salt_sha256": "salt", "simulator_sha256": "v1"}
    (bots / "gates.json").write_text(
        json.dumps({**identity, "seeds": list(range(10)), "scores": {"graph_walk": [0.9] * 10}})
    )
    manifest = {**identity, "model": "test", "seeds": list(range(10))}
    (agent / "manifest.json").write_text(json.dumps(manifest))
    rows = [
        {"seed": seed, "reward": 0.5, "error": None, "stop_reason": "task_done"}
        for seed in range(10)
    ]
    (agent / "summary.json").write_text(json.dumps(rows))
    assert compare(bots, agent)["passed"] is True
    manifest["simulator_sha256"] = "v2"
    (agent / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source hash"):
        compare(bots, agent)
    manifest["simulator_sha256"] = "v1"
    (agent / "manifest.json").write_text(json.dumps(manifest))
    rows[0]["error"] = "HTTPStatusError"
    (agent / "summary.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="do not count"):
        compare(bots, agent)

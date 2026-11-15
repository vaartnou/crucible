"""Protocol and replay tests, using an in-memory provider and a known small world."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from crucible.agents.batch import agent_batch
from crucible.agents.compatible import CompatiblePolicy
from crucible.core.jobs import Job
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, ToolCall
from crucible.core.transcript import TranscriptWriter
from crucible.feedback.regrade import regrade
from tests.fixtures.guess.task import GuessNumber


async def test_multi_call_results_keep_provider_ids() -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(i),
                        "type": "function",
                        "function": {"name": "guess", "arguments": json.dumps({"n": i})},
                    }
                    for i in (1, 2)
                ],
            }
        else:
            message = {"role": "assistant", "content": "done"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(
        base_url="https://test/", transport=httpx.MockTransport(respond)
    ) as client:
        policy = CompatiblePolicy(client, "test-model")
        task = GuessNumber()
        await policy.reset(task.spec, task.tools(), task.instruction())
        assert await policy.act(Observation()) == ToolCall(name="guess", args={"n": 1})
        assert await policy.act(Observation(text="higher")) == ToolCall(name="guess", args={"n": 2})
        assert await policy.act(Observation(text="lower")) is None
    replies = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in replies] == [("1", "higher"), ("2", "lower")]


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


async def test_agent_refuses_output_reuse(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        await agent_batch(
            "guess@1",
            "test",
            [0],
            tmp_path,
            base_url="https://test/",
            api_key="secret",
            salt="salt",
        )


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

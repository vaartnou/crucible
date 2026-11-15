"""Trusted sidecar evidence is replayed independently, with errors kept out of scores."""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, ToolCall, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.export.collect_episode import collect_episode
from crucible.export.verify_episode import verify_episode

SALT = "test-private-salt"


async def episode(tmp_path: Path, mode: str = "reference") -> tuple[Path, float]:
    task = get_task("guess@1")
    path = tmp_path / "original.jsonl"
    session = EpisodeSession(task, 4, TranscriptWriter(path), salt=SALT)
    obs = await session.start(policy_name=mode, job=Job(task="guess@1", policy=mode, seed=4))
    if mode == "reference":
        policy = get_policy("reference", "guess@1")
        await policy.reset(task.spec, task.tools(), task.instruction())
        while not task.done():
            call = await policy.act(obs)
            assert call is not None
            result = await session.step(call)
            obs = Observation(text=result_text(result), last_result=result)
    elif mode == "budget":
        # Invalid public calls still consume the session budget and must replay identically.
        for _ in range(task.spec.max_steps):
            await session.step(ToolCall(name="unknown", args={}))
    elif mode == "resolved":
        await session.step(ToolCall(name="give_up", args={}))
    elif mode == "early":
        await session.step(ToolCall(name="guess", args={"n": 50}))
    reason, score = await session.close()
    body = {
        "task": "guess@1",
        "seed": 4,
        "reward": score.total,
        "stop_reason": reason,
        "cleanup_errors": [],
        "transcript": [json.loads(line) for line in path.read_text().splitlines()],
    }
    evidence = tmp_path / "episode.json"
    evidence.write_text(json.dumps(body))
    return evidence, score.total


@pytest.mark.parametrize("mode", ["reference", "empty", "budget", "resolved", "early"])
async def test_replay_reward_and_lifecycle(tmp_path: Path, mode: str) -> None:
    evidence, expected = await episode(tmp_path, mode)
    out = tmp_path / "verifier"
    reward = await verify_episode(evidence, out, task_ref="guess@1", seed=4, salt=SALT)
    assert reward == expected
    assert 0 <= reward <= 1
    assert float((out / "reward.txt").read_text()) == expected
    assert set(p.name for p in out.iterdir()) == {"reward.txt", "grade.json", "transcript.json"}
    assert json.loads((out / "grade.json").read_text())["reward"] == expected


async def test_recomputes_instead_of_trusting_supplied_reward(tmp_path: Path) -> None:
    evidence, expected = await episode(tmp_path)
    body = json.loads(evidence.read_text())
    body["reward"] = 999
    evidence.write_text(json.dumps(body))
    assert (
        await verify_episode(evidence, tmp_path / "out", task_ref="guess@1", seed=4, salt=SALT)
        == expected
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "world",
        "header",
        "hash",
        "missing_footer",
        "count",
        "index",
        "result",
        "score",
        "error",
        "cleanup",
    ],
)
async def test_rejects_invalid_evidence(tmp_path: Path, mutation: str) -> None:
    evidence, _ = await episode(tmp_path)
    body = json.loads(evidence.read_text())
    records = body["transcript"]
    if mutation == "world":
        body["seed"] = 10
    elif mutation == "header":
        records[0]["task"] = "other@1"
    elif mutation == "hash":
        records[0]["scoring_hash"] = "different"
    elif mutation == "missing_footer":
        records.pop()
    elif mutation == "count":
        records[-1]["steps"] += 1
    elif mutation == "index":
        records[1]["step"] = 99
    elif mutation == "result":
        records[1]["result"]["ok"] = not records[1]["result"]["ok"]
    elif mutation == "score":
        records[-1]["score"]["total"] = 0.123456
    elif mutation == "error":
        records[-1]["error"] = "RuntimeError"
    elif mutation == "cleanup":
        body["cleanup_errors"] = ["RuntimeError"]
    evidence.write_text(json.dumps(body))
    out = tmp_path / "out"
    with pytest.raises(ValueError):
        await verify_episode(evidence, out, task_ref="guess@1", seed=4, salt=SALT)
    assert not (out / "reward.txt").exists()


async def test_missing_evidence_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await verify_episode(
            tmp_path / "missing.json", tmp_path / "out", task_ref="guess@1", seed=4, salt=SALT
        )


def test_collection_is_atomic_and_uses_local_auth(tmp_path: Path) -> None:
    path = tmp_path / "verification/episode.json"
    body: dict[str, object] = {"transcript": [], "reward": 0.4}
    with (
        patch.dict("os.environ", {"GRADE_TOKEN": "private-token"}),
        patch(
            "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())
        ) as urlopen,
    ):
        collect_episode(path)
        request = urlopen.call_args.args[0]
        assert request.full_url == "http://127.0.0.1:8000/grade"
        assert request.get_header("X-grade-token") == "private-token"
    assert json.loads(path.read_text()) == body
    assert not path.with_suffix(".tmp").exists()
    with (
        patch.dict("os.environ", {"GRADE_TOKEN": "private-token"}),
        patch("urllib.request.urlopen", side_effect=OSError("sidecar unavailable")),
        pytest.raises(OSError),
    ):
        collect_episode(path)
    assert json.loads(path.read_text()) == body

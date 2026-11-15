"""Live progress stays inspectable, and cancelled runs never become model failures."""

import asyncio
import json
from pathlib import Path

import pytest

from crucible.agents.batch import agent_batch
from crucible.agents.compatible import CompatiblePolicy
from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.core.transcript import TranscriptWriter, read_transcript


def test_tool_progress_is_visible_before_episode_finishes(tmp_path: Path) -> None:
    path = tmp_path / "episode.jsonl"
    writer = TranscriptWriter(path)
    try:
        writer.header(
            task="guess@1", scoring_hash="none", policy="test", seed=0, repeat=0, job_id="test"
        )
        writer.step(0, ToolCall(name="give_up"), ToolResult(ok=True))
        assert not path.exists()
        lines = path.with_suffix(".jsonl.tmp").read_text().splitlines()
        assert [json.loads(line)["type"] for line in lines] == ["header", "step"]
    finally:
        writer.abort()


async def test_cancelled_model_batch_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancel(self: CompatiblePolicy, obs: Observation) -> ToolCall | None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(CompatiblePolicy, "act", cancel)
    out = tmp_path / "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await agent_batch(
            "guess@1",
            "test",
            [0],
            out,
            base_url="https://unused.invalid/",
            api_key="test",
            salt="test",
        )
    transcript = read_transcript(out / "0.jsonl")
    assert transcript.footer.stop_reason == "error"
    assert transcript.footer.error == "CancelledError"
    assert not (out / "summary.json").exists()

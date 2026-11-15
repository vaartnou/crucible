"""Live progress stays inspectable, and cancelled runs never become model failures."""

import json
from pathlib import Path

from crucible.core.tools import ToolCall, ToolResult
from crucible.core.transcript import TranscriptWriter


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

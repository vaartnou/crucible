"""A salt gives a different world for the same seed; no salt keeps the old world."""

import asyncio
from pathlib import Path

import pytest

from crucible.core.jobs import Job
from crucible.core.registry import get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter, read_transcript
from crucible.server.settings import Settings

SEEDS = range(20)


def _guess_answers(seed: int, salt: str | None) -> list[object]:
    """What `guess` answers to 1..100 for this world: a fingerprint of the hidden target."""

    async def run() -> list[object]:
        task = get_task("guess@1")
        await task.reset(seed, salt=salt)
        out: list[object] = []
        for n in range(1, 101):
            result = await task.step(ToolCall(name="guess", args={"n": n}))
            out.append(result.content)
            if result.content == "correct":
                break
        return out

    return asyncio.run(run())


def test_guess_salt_changes_the_world() -> None:
    differ = [s for s in SEEDS if _guess_answers(s, None) != _guess_answers(s, "salt-a")]
    assert len(differ) >= len(SEEDS) - 2, differ
    for seed in SEEDS:
        assert _guess_answers(seed, "salt-a") == _guess_answers(seed, "salt-a")


def test_session_passes_the_salt_and_marks_the_header(tmp_path: Path) -> None:
    async def first_answers(salt: str | None, path: Path) -> object:
        session = EpisodeSession(get_task("guess@1"), 3, TranscriptWriter(path), salt=salt)
        await session.start(policy_name="p", job=Job(task="guess@1", policy="p", seed=3))
        answers: list[object] = []
        low, high = 1, 100
        while low <= high:
            n = (low + high) // 2
            result = await session.step(ToolCall(name="guess", args={"n": n}))
            answers.append(result.content)
            if result.content == "correct":
                break
            if result.content == "higher":
                low = n + 1
            else:
                high = n - 1
        await session.close()
        return answers

    plain = asyncio.run(first_answers(None, tmp_path / "plain.jsonl"))
    salted = asyncio.run(first_answers("secret-salt", tmp_path / "salted.jsonl"))
    assert plain != salted
    assert read_transcript(tmp_path / "plain.jsonl").header.salted is False
    salted_header = read_transcript(tmp_path / "salted.jsonl").header
    assert salted_header.salted is True
    assert "secret-salt" not in (tmp_path / "salted.jsonl").read_text()


def test_sidecar_reads_the_salt_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDECAR_SALT", raising=False)
    assert Settings().sidecar_salt is None
    monkeypatch.setenv("SIDECAR_SALT", "abc123")
    assert Settings().sidecar_salt == "abc123"

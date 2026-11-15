"""Versioned JSONL transcripts: one file per episode, header + steps + footer.

File I/O is plain synchronous: each write is one short line, so it never blocks the event loop
for long, and it keeps the writer usable from sync code and tests.
"""

import os
import subprocess
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from crucible.core.scoring import Score
from crucible.core.tools import ToolCall, ToolResult

SCHEMA_VERSION = 1

StopReason = Literal["policy_stop", "task_done", "budget_exhausted", "error"]


class Header(BaseModel):
    """First record: what ran and where."""

    model_config = ConfigDict(frozen=True)

    type: Literal["header"] = "header"
    schema_version: Literal[1] = SCHEMA_VERSION
    task: str
    scoring_hash: str
    policy: str
    seed: int
    repeat: int
    job_id: str
    git_commit: str
    started_at: str
    salted: bool = False


class StepRecord(BaseModel):
    """One tool call and its result."""

    model_config = ConfigDict(frozen=True)

    type: Literal["step"] = "step"
    schema_version: Literal[1] = SCHEMA_VERSION
    step: int
    call: ToolCall
    result: ToolResult


class Footer(BaseModel):
    """Last record: the outcome. A file without one is not a finished episode."""

    model_config = ConfigDict(frozen=True)

    type: Literal["footer"] = "footer"
    schema_version: Literal[1] = SCHEMA_VERSION
    score: Score
    steps: int
    stop_reason: StopReason
    error: str | None = None
    ended_at: str


class Transcript(BaseModel):
    """A whole transcript, parsed."""

    model_config = ConfigDict(frozen=True)

    header: Header
    steps: list[StepRecord]
    footer: Footer


TIME_FIELDS = ("started_at", "ended_at")

_Record = Annotated[Header | StepRecord | Footer, Field(discriminator="type")]
_RECORD: TypeAdapter[Header | StepRecord | Footer] = TypeAdapter(_Record)


def now() -> str:
    """Current UTC time as an ISO string."""
    return datetime.now(UTC).isoformat()


@cache
def git_commit() -> str:
    """`CRUCIBLE_GIT_COMMIT`, else `git rev-parse HEAD`, else `"unknown"`. Once per process."""
    env = os.environ.get("CRUCIBLE_GIT_COMMIT")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    return out.stdout.strip() or "unknown"


class TranscriptWriter:
    """Writes `<path>.tmp`, renamed to `path` on `close()`, so an existing file is complete."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.step_count = 0
        self._tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO | None = self._tmp.open("w", encoding="utf-8")

    def _write(self, record: Header | StepRecord | Footer) -> None:
        if self._file is None:
            raise ValueError("Transcript writer is closed")
        self._file.write(record.model_dump_json() + "\n")
        # Make live tool progress visible while keeping unfinished episodes in .tmp.
        self._file.flush()

    def header(
        self,
        *,
        task: str,
        scoring_hash: str,
        policy: str,
        seed: int,
        repeat: int,
        job_id: str,
        salted: bool = False,
    ) -> None:
        """Write the header. `salted`: the world used a secret salt (never written)."""
        self._write(
            Header(
                task=task,
                scoring_hash=scoring_hash,
                policy=policy,
                seed=seed,
                repeat=repeat,
                job_id=job_id,
                git_commit=git_commit(),
                started_at=now(),
                salted=salted,
            )
        )

    def step(self, step: int, call: ToolCall, result: ToolResult) -> None:
        self._write(StepRecord(step=step, call=call, result=result))
        self.step_count += 1

    def footer(self, score: Score, stop_reason: StopReason, error: str | None = None) -> None:
        self._write(
            Footer(
                score=score,
                steps=self.step_count,
                stop_reason=stop_reason,
                error=error,
                ended_at=now(),
            )
        )

    def close(self) -> None:
        """Finish the file and move it into place."""
        if self._file is None:
            return
        self._file.close()
        self._file = None
        self._tmp.replace(self.path)

    def abort(self) -> None:
        """Drop a half-written file. Safe to call after `close()`."""
        if self._file is not None:
            self._file.close()
            self._file = None
        self._tmp.unlink(missing_ok=True)


def read_transcript(path: Path) -> Transcript:
    """Parse a finished transcript. Raise `ValueError` if it is not header, steps, footer."""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    records = [_RECORD.validate_json(ln) for ln in lines]
    if (
        len(records) < 2
        or not isinstance(records[0], Header)
        or not isinstance(records[-1], Footer)
    ):
        raise ValueError(f"Transcript {path} must start with a header and end with a footer")
    steps = records[1:-1]
    if not all(isinstance(r, StepRecord) for r in steps):
        raise ValueError(f"Transcript {path} has a header or footer in the middle")
    return Transcript(
        header=records[0],
        steps=[r for r in steps if isinstance(r, StepRecord)],
        footer=records[-1],
    )


__all__ = [
    "SCHEMA_VERSION",
    "TIME_FIELDS",
    "Footer",
    "Header",
    "StepRecord",
    "StopReason",
    "Transcript",
    "TranscriptWriter",
    "git_commit",
    "read_transcript",
]

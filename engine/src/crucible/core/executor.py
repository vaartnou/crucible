"""Executors run a list of jobs and return results in job order."""

import asyncio
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from crucible.core.episode import EpisodeResult, InfraError, run_episode
from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.transcript import TranscriptWriter, git_commit, read_transcript

SUMMARY_COLUMNS = [
    "job_id",
    "task",
    "policy",
    "seed",
    "repeat",
    "total",
    "steps",
    "stop_reason",
    "retried",
    "transcript",
]


class Executor(Protocol):
    """Runs jobs, writing transcripts under `out_dir`."""

    async def run(self, jobs: Sequence[Job], out_dir: Path) -> list[EpisodeResult]: ...


def _load_finished(job: Job, path: Path) -> EpisodeResult | None:
    """Rebuild the result of a finished episode from its transcript, if the file exists."""
    if not path.exists():
        return None
    transcript = read_transcript(path)
    diagnostics = path.with_suffix(".cleanup.json")
    cleanup_errors: tuple[str, ...] = ()
    if diagnostics.exists():
        raw = json.loads(diagnostics.read_text())
        errors = raw.get("cleanup_errors", [])
        cleanup_errors = TypeAdapter(tuple[str, ...]).validate_python(errors)
    return EpisodeResult(
        job=job,
        task=transcript.header.task,
        policy=transcript.header.policy,
        seed=transcript.header.seed,
        score=transcript.footer.score,
        steps=transcript.footer.steps,
        stop_reason=transcript.footer.stop_reason,
        transcript_path=path,
        cleanup_errors=cleanup_errors,
    )


def _write_summary(path: Path, results: Sequence[EpisodeResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(SUMMARY_COLUMNS)
        for r in results:
            writer.writerow(
                [
                    r.job.job_id,
                    r.task,
                    r.policy,
                    r.seed,
                    r.job.repeat,
                    r.score.total,
                    r.steps,
                    r.stop_reason,
                    r.retried,
                    r.transcript_path,
                ]
            )


def _check_names(jobs: Sequence[Job]) -> None:
    """Fail fast with the registry's clear error if a job names an unknown task or policy."""
    for task_ref, policy in {(j.task, j.policy) for j in jobs}:
        get_task(task_ref)
        get_policy(policy, task_ref)


def _prepare(out_dir: Path) -> Path:
    episodes = out_dir / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    return episodes


class LocalExecutor:
    """Runs jobs in this process. A resumed job (transcript exists) reports `retried=0`."""

    # Per-provider rate limiters come with the LLM policies; they will wrap the policy call.

    def __init__(self, concurrency: int = 1, max_retries: int = 2) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.concurrency = concurrency
        self.max_retries = max_retries

    async def run(self, jobs: Sequence[Job], out_dir: Path) -> list[EpisodeResult]:
        ids = [j.job_id for j in jobs]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate jobs: job ids must be unique")
        _check_names(jobs)
        if self.concurrency != 1 and any(get_task(j.task).external_backend for j in jobs):
            raise ValueError("External backend evaluations require concurrency=1")
        await asyncio.to_thread(git_commit)  # cache it now; the subprocess would block the loop
        episodes = _prepare(out_dir)
        semaphore = asyncio.Semaphore(self.concurrency)
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(self._run_job(job, episodes / f"{job.job_id}.jsonl", semaphore))
                for job in jobs
            ]
        results = [t.result() for t in tasks]
        _write_summary(out_dir / "summary.csv", results)
        return results

    async def _run_job(self, job: Job, path: Path, semaphore: asyncio.Semaphore) -> EpisodeResult:
        async with semaphore:
            finished = _load_finished(job, path)
            if finished is not None:
                return finished
            for attempt in range(self.max_retries + 1):
                task = get_task(job.task)
                policy = get_policy(job.policy, job.task)
                try:
                    result = await run_episode(
                        task,
                        policy,
                        job.seed,
                        job=job,
                        writer=TranscriptWriter(path),
                        record_infra_errors=attempt == self.max_retries,
                    )
                except InfraError:
                    continue
                return result.model_copy(update={"retried": attempt})
        raise AssertionError("unreachable: the last attempt records infra errors")


__all__ = ["Executor", "LocalExecutor"]

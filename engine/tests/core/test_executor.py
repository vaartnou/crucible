import csv
from collections.abc import Generator
from pathlib import Path
from typing import ClassVar

import pytest

from crucible.core import registry
from crucible.core.episode import EpisodeResult, InfraError
from crucible.core.executor import Executor, LocalExecutor
from crucible.core.jobs import Job, RunSpec, expand
from crucible.core.registry import register_policy, register_task
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec
from crucible.core.transcript import TIME_FIELDS, read_transcript
from crucible.policies.baselines import DoNothing

GUESS_JOBS = expand(RunSpec(task="guess", policy="reference", seeds=list(range(12)), repeats=2))


def _comparable(result: EpisodeResult) -> tuple[object, ...]:
    return (result.job, result.task, result.score, result.steps, result.stop_reason)


def _transcript_body(path: Path) -> str:
    t = read_transcript(path)
    drop = {f: "" for f in TIME_FIELDS}
    header = t.header.model_copy(update={"started_at": drop["started_at"]})
    footer = t.footer.model_copy(update={"ended_at": drop["ended_at"]})
    return "\n".join(r.model_dump_json() for r in (header, *t.steps, footer))


async def test_empty_run_writes_empty_summary(tmp_path: Path) -> None:
    executor: Executor = LocalExecutor()
    assert await executor.run([], tmp_path) == []
    assert (tmp_path / "summary.csv").read_text().count("\n") == 1


async def test_concurrency_8_matches_concurrency_1(tmp_path: Path) -> None:
    seq = await LocalExecutor(concurrency=1).run(GUESS_JOBS, tmp_path / "seq")
    par = await LocalExecutor(concurrency=8).run(GUESS_JOBS, tmp_path / "par")
    assert [r.job for r in par] == GUESS_JOBS  # job order, not finish order
    assert [_comparable(r) for r in par] == [_comparable(r) for r in seq]
    assert all(r.score.flags["solved"] for r in par)
    for a, b in zip(seq, par, strict=True):
        assert a.transcript_path == tmp_path / "seq" / "episodes" / f"{a.job.job_id}.jsonl"
        assert _transcript_body(a.transcript_path) == _transcript_body(b.transcript_path)


async def test_second_run_skips_all_jobs(tmp_path: Path) -> None:
    executor = LocalExecutor(concurrency=4)
    first = await executor.run(GUESS_JOBS, tmp_path)
    files = sorted((tmp_path / "episodes").iterdir())
    stamps = {p: p.stat().st_mtime_ns for p in files}
    second = await executor.run(GUESS_JOBS, tmp_path)
    assert [_comparable(r) for r in second] == [_comparable(r) for r in first]
    assert {p: p.stat().st_mtime_ns for p in sorted((tmp_path / "episodes").iterdir())} == stamps
    assert len(files) == len(GUESS_JOBS)


async def test_summary_csv_has_one_row_per_job(tmp_path: Path) -> None:
    results = await LocalExecutor(concurrency=3).run(GUESS_JOBS, tmp_path)
    with (tmp_path / "summary.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["job_id"] for r in rows] == [r.job.job_id for r in results]
    assert len(rows) == len(GUESS_JOBS)
    assert rows[0]["policy"] == "reference"
    assert float(rows[0]["total"]) == results[0].score.total
    assert rows[0]["retried"] == "0"


def test_rejects_bad_settings() -> None:
    with pytest.raises(ValueError):
        LocalExecutor(concurrency=0)
    with pytest.raises(ValueError):
        LocalExecutor(max_retries=-1)


async def test_duplicate_jobs_rejected(tmp_path: Path) -> None:
    job = Job(task="guess", policy="reference", seed=1)
    with pytest.raises(ValueError, match="Duplicate"):
        await LocalExecutor().run([job, job], tmp_path)


class _Flaky(Task):
    """Raises on reset until it has been tried `fail_times` times, per seed."""

    spec: ClassVar[TaskSpec] = TaskSpec(
        id="flaky", version=1, max_steps=1, title="t", description="d"
    )
    fail_times: ClassVar[int] = 0
    error: ClassVar[type[Exception]] = InfraError
    attempts: ClassVar[dict[int, int]] = {}

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        count = self.attempts.get(seed, 0) + 1
        self.attempts[seed] = count
        if count <= self.fail_times:
            raise self.error("boom")
        return Observation()

    def tools(self) -> list[ToolSpec]:
        return []

    async def step(self, call: ToolCall) -> ToolResult:
        return ToolResult(ok=True)

    def done(self) -> bool:
        return True

    async def grade(self) -> Score:
        return Score(total=1.0)

    def instruction(self) -> str:
        return ""


@pytest.fixture
def flaky() -> Generator[type[_Flaky]]:
    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        _Flaky.attempts = {}
        _Flaky.fail_times = 0
        _Flaky.error = InfraError
        register_task(_Flaky)
        register_policy("do_nothing")(DoNothing)
        yield _Flaky


JOB = Job(task="flaky", policy="do_nothing", seed=1)


async def test_infra_error_is_retried_and_marked(tmp_path: Path, flaky: type[_Flaky]) -> None:
    flaky.fail_times = 2
    [result] = await LocalExecutor(max_retries=2).run([JOB], tmp_path)
    assert result.retried == 2
    assert result.stop_reason == "task_done"
    assert result.score.total == 1.0
    assert flaky.attempts[1] == 3
    assert [p.name for p in (tmp_path / "episodes").iterdir()] == [f"{JOB.job_id}.jsonl"]
    with (tmp_path / "summary.csv").open(newline="") as f:
        assert next(csv.DictReader(f))["retried"] == "2"


async def test_infra_error_gives_up_after_max_retries(tmp_path: Path, flaky: type[_Flaky]) -> None:
    flaky.fail_times = 99
    [result] = await LocalExecutor(max_retries=2).run([JOB], tmp_path)
    assert flaky.attempts[1] == 3
    assert result.retried == 2
    assert result.stop_reason == "error"
    assert result.score.total == 0
    assert result.score.flags["env_error"] is True
    assert read_transcript(result.transcript_path).footer.error == "InfraError"


async def test_other_errors_are_not_retried(tmp_path: Path, flaky: type[_Flaky]) -> None:
    flaky.fail_times = 99
    flaky.error = ValueError
    [result] = await LocalExecutor(max_retries=2).run([JOB], tmp_path)
    assert flaky.attempts[1] == 1
    assert result.retried == 0
    assert result.stop_reason == "error"
    assert read_transcript(result.transcript_path).footer.error == "ValueError"


@pytest.mark.parametrize(
    ("job", "error"),
    [
        (Job(task="nope@1", policy="reference", seed=0), registry.UnknownTaskError),
        (Job(task="guess@1", policy="nope", seed=0), registry.UnknownPolicyError),
    ],
)
async def test_unknown_names_fail_fast(job: Job, error: type[Exception], tmp_path: Path) -> None:
    with pytest.raises(error):
        await LocalExecutor().run([job], tmp_path)
    assert not (tmp_path / "episodes").exists()


async def test_external_backend_evaluations_require_sequential_execution(tmp_path: Path) -> None:
    from crucible.core import registry
    from crucible.core.registry import register_policy, register_task
    from crucible.envs.guess.task import GuessNumber
    from crucible.policies.baselines import DoNothing

    class ExternalGuess(GuessNumber):
        spec = GuessNumber.spec.model_copy(update={"id": "external-test"})
        external_backend = True

    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        register_task(ExternalGuess)
        register_policy("do_nothing")(DoNothing)
        with pytest.raises(ValueError, match="concurrency=1"):
            await LocalExecutor(concurrency=2).run(
                [Job(task="external-test@1", policy="do_nothing", seed=0)], tmp_path
            )

"""Parallel runs give the same scores and transcripts as sequential runs."""

from pathlib import Path

from crucible.core.executor import LocalExecutor
from crucible.core.jobs import RunSpec, expand
from crucible.core.transcript import TIME_FIELDS, read_transcript

POLICIES = ["reference", "do_nothing"]
JOBS = [
    job
    for policy in POLICIES
    for job in expand(RunSpec(task="guess@1", policy=policy, seeds=list(range(10))))
]


def _body(path: Path) -> str:
    t = read_transcript(path)
    header = t.header.model_copy(update={"started_at": ""})
    footer = t.footer.model_copy(update={"ended_at": ""})
    assert set(TIME_FIELDS) == {"started_at", "ended_at"}
    return "\n".join(r.model_dump_json() for r in (header, *t.steps, footer))


async def test_concurrency_8_matches_concurrency_1(tmp_path: Path) -> None:
    seq = await LocalExecutor(concurrency=1).run(JOBS, tmp_path / "seq")
    par = await LocalExecutor(concurrency=8).run(JOBS, tmp_path / "par")
    assert [r.job for r in par] == JOBS
    for a, b in zip(seq, par, strict=True):
        assert a.score.total == b.score.total, a.job
        assert (a.steps, a.stop_reason) == (b.steps, b.stop_reason), a.job
        assert _body(a.transcript_path) == _body(b.transcript_path), a.job

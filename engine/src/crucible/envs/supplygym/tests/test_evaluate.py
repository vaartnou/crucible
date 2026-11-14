"""LLM reports require compatible, complete paired episodes for each tier."""

import argparse
from pathlib import Path

import pytest

from crucible.core.episode import EpisodeResult
from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.core.scoring import Score
from crucible.core.transcript import TranscriptWriter
from crucible.envs.supplygym.evaluate import report
from crucible.envs.supplygym.grader import SCORING_HASH


@pytest.mark.parametrize("tier", ["standard", "hard"])
@pytest.mark.parametrize("invalid", [None, "salted", "hash", "duplicate", "partial", "error"])
def test_llm_paired_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
    invalid: str | None,
) -> None:
    task = "supplygym@1" if tier == "standard" else "supplygym-hard@1"
    score = Score(total=0.5, breakdown={"profit": 123.0, "delta_vs_ss": 10.0})

    async def run(self: LocalExecutor, jobs: list[Job], out: Path) -> list[EpisodeResult]:
        return [
            EpisodeResult(
                job=j,
                task=j.task,
                policy=j.policy,
                seed=j.seed,
                score=score,
                steps=1,
                stop_reason="task_done",
                transcript_path=out / "ss.jsonl",
            )
            for j in jobs
        ]

    monkeypatch.setattr(LocalExecutor, "run", run)
    transcripts = tmp_path / "llm"
    for seed in [30000] if invalid == "partial" else [30000, 30001]:
        copies = 2 if invalid == "duplicate" else 1
        for copy in range(copies):
            path = transcripts / f"{seed}-{copy}.jsonl"
            writer = TranscriptWriter(path)
            writer.header(
                task=task,
                scoring_hash="wrong" if invalid == "hash" else SCORING_HASH,
                policy="fixture",
                seed=seed,
                repeat=0,
                job_id=str(seed),
                salted=invalid == "salted",
            )
            bad_score = score.model_copy(update={"flags": {"env_error": True}})
            writer.footer(bad_score if invalid == "error" else score, "task_done")
            writer.close()
    args = argparse.Namespace(
        split="fresh", n=2, policies="ss", repeats=1, concurrency=1, llm_transcripts=transcripts
    )
    if invalid:
        with pytest.raises(ValueError):
            report(args, task, tier, tmp_path / "out")
    else:
        report(args, task, tier, tmp_path / "out")
        output = capsys.readouterr().out
        assert f"tier={tier} task={task}" in output
        assert "llm:fixture" in output
        assert "not measured" not in output

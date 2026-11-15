"""Recompute Harbor rewards from sidecar evidence in a separate verifier container."""

import asyncio
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, JsonValue

from crucible.core.jobs import Job
from crucible.core.registry import get_task
from crucible.core.session import EpisodeSession
from crucible.core.transcript import Footer, Header, StepRecord, TranscriptWriter
from crucible.export.collect_episode import EVIDENCE_PATH


class Evidence(BaseModel):
    task: str
    seed: int
    transcript: list[dict[str, JsonValue]]
    cleanup_errors: list[str]


async def verify_episode(
    evidence_path: Path, out: Path, *, task_ref: str, seed: int, salt: str
) -> float:
    """Validate a complete transcript and replay calls; never trust its supplied reward."""
    evidence = Evidence.model_validate_json(await asyncio.to_thread(evidence_path.read_text))
    if evidence.task != task_ref or evidence.seed != seed:
        raise ValueError("Evidence world does not match verifier configuration")
    if evidence.cleanup_errors or len(evidence.transcript) < 2:
        raise ValueError("Episode evidence is incomplete or has cleanup errors")
    header = Header.model_validate(evidence.transcript[0])
    footer = Footer.model_validate(evidence.transcript[-1])
    steps = [StepRecord.model_validate(record) for record in evidence.transcript[1:-1]]
    task = get_task(task_ref)
    if (
        header.task != task_ref
        or header.seed != seed
        or not header.salted
        or header.scoring_hash != task.scoring_hash
    ):
        raise ValueError("Transcript identity or scoring hash does not match the frozen task")
    if footer.error or footer.stop_reason == "error" or footer.score.flags.get("env_error"):
        raise ValueError("Simulator reported an environment error")
    if footer.steps != len(steps) or len(steps) > task.spec.max_steps:
        raise ValueError("Transcript step count is invalid")
    if any(record.step != index for index, record in enumerate(steps)):
        raise ValueError("Transcript steps are not consecutive")

    with TemporaryDirectory() as directory:
        session = EpisodeSession(
            task, seed, TranscriptWriter(Path(directory) / "replay.jsonl"), salt=salt
        )
        await session.start(
            policy_name="harbor-verifier",
            job=Job(task=task_ref, policy="harbor-verifier", seed=seed),
        )
        try:
            for index, record in enumerate(steps):
                if task.done():
                    raise ValueError(
                        f"Transcript continues after episode completion at step {index}"
                    )
                result = await session.step(record.call)
                if result != record.result:
                    raise ValueError(f"Replayed tool result differs at step {index}")
            reason, score = await session.close()
        finally:
            await session.close()
    if session.cleanup_errors:
        raise ValueError("Verifier replay cleanup failed")
    if reason != footer.stop_reason or score != footer.score:
        raise ValueError("Replayed outcome differs from the recorded episode")
    if not math.isfinite(score.total) or not 0 <= score.total <= 1:
        raise ValueError("Replayed reward must be finite and between zero and one")

    summary: dict[str, JsonValue] = {
        "reward": score.total,
        "breakdown": dict(score.breakdown),
        "flags": dict(score.flags),
        "steps": len(steps),
        "stop_reason": reason,
    }
    await asyncio.to_thread(_write_outputs, out, summary, evidence, score.total)
    return score.total


def _write_outputs(
    out: Path, summary: dict[str, JsonValue], evidence: Evidence, reward: float
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "grade.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    (out / "transcript.json").write_text(json.dumps(evidence.transcript, indent=2) + "\n")
    (out / "reward.txt").write_text(f"{reward}\n")


def main() -> None:
    out = Path("/logs/verifier")
    out.mkdir(parents=True, exist_ok=True)
    # A failed replay must never leave an earlier successful reward behind.
    for name in ("reward.txt", "reward.json"):
        (out / name).unlink(missing_ok=True)
    try:
        reward = asyncio.run(
            verify_episode(
                EVIDENCE_PATH,
                out,
                task_ref=os.environ["CRUCIBLE_VERIFY_TASK"],
                seed=int(os.environ["CRUCIBLE_VERIFY_SEED"]),
                salt=os.environ["CRUCIBLE_VERIFY_SALT"],
            )
        )
    except Exception as exc:
        (out / "grade_error.txt").write_text(f"{type(exc).__name__}: {exc}\n")
        raise
    print(f"Replayed reward: {reward}")


if __name__ == "__main__":
    main()

"""The one episode loop."""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from crucible.core.jobs import Job
from crucible.core.policy import Policy
from crucible.core.scoring import Score
from crucible.core.session import EpisodeSession
from crucible.core.task import Task
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import StopReason, TranscriptWriter


class InfraError(Exception):
    """Transient infrastructure failure. The only error the executor retries."""


class EpisodeResult(BaseModel):
    """Outcome of one job."""

    model_config = ConfigDict(frozen=True)

    job: Job
    task: str
    policy: str
    seed: int
    score: Score
    steps: int
    stop_reason: StopReason
    transcript_path: Path
    retried: int = 0
    cleanup_errors: tuple[str, ...] = ()


async def _play(session: EpisodeSession, policy: Policy, job: Job) -> None:
    task = session.task
    obs = await session.start(policy_name=policy.name, job=job)
    await policy.reset(task.spec, task.tools(), task.instruction())
    for _ in range(session.max_steps):
        if task.done():
            break
        call = await policy.act(obs)
        if call is None:
            break
        result = await session.step(call)
        obs = Observation(text=result_text(result), last_result=result)


async def run_episode(
    task: Task,
    policy: Policy,
    seed: int,
    *,
    job: Job,
    writer: TranscriptWriter,
    record_infra_errors: bool = False,
) -> EpisodeResult:
    """Run one episode and write its transcript.

    Any exception ends the episode as `stop_reason="error"` with a zero score and
    `flags["env_error"]`, except `InfraError`: it is re-raised (and the partial transcript
    dropped) so the executor can retry, unless `record_infra_errors` is set (last attempt).
    """
    session = EpisodeSession(task, seed, writer)
    try:
        try:
            await _play(session, policy, job)
            reason, score = await session.close()
            error: str | None = None
        except InfraError as exc:
            if not record_infra_errors:
                raise
            error = _name(exc)
            reason, score = await session.close(error)
        except Exception as exc:
            error = _name(exc)
            reason, score = await session.close(error)
    except BaseException as exc:
        try:
            await session.close(type(exc).__name__)
        except Exception:
            logging.getLogger(__name__).exception("Episode finalization failed after primary error")
        finally:
            writer.abort()
            # Only a retryable InfraError or an interruption (cancellation, KeyboardInterrupt)
            # reaches here. Neither is a completed job, but close() has already renamed the
            # transcript into place, so drop it or a resumed run would skip this job.
            writer.path.unlink(missing_ok=True)
            writer.path.with_suffix(".cleanup.json").unlink(missing_ok=True)
        raise
    return EpisodeResult(
        job=job,
        task=task.spec.ref,
        policy=policy.name,
        seed=seed,
        score=score,
        steps=writer.step_count,
        stop_reason=reason,
        transcript_path=writer.path,
        cleanup_errors=tuple(session.cleanup_errors),
    )


def _name(exc: Exception) -> str:
    return type(exc).__name__


__all__ = ["EpisodeResult", "InfraError", "run_episode"]

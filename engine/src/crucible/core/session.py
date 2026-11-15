"""One episode's per-call rules, shared by the in-process loop, HTTP and MCP."""

import asyncio
import json
import logging

from crucible.core.jobs import Job
from crucible.core.scoring import Score
from crucible.core.task import Task
from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.core.transcript import StopReason, TranscriptWriter

logger = logging.getLogger(__name__)


class EpisodeSession:
    """Owns actions, final grading, transcripts and episode resource release."""

    def __init__(
        self,
        task: Task,
        seed: int,
        writer: TranscriptWriter,
        *,
        salt: str | None = None,
        difficulty: int = 0,
    ) -> None:
        self._task = task
        self._seed = seed
        self._salt = salt
        self._difficulty = difficulty
        self._writer = writer
        self._steps_used = 0
        self._outcome: tuple[StopReason, Score] | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[tuple[StopReason, Score]] | None = None
        self.cleanup_errors: list[str] = []

    @property
    def task(self) -> Task:
        return self._task

    @property
    def max_steps(self) -> int:
        return self._task.spec.max_steps

    @property
    def steps_used(self) -> int:
        return self._steps_used

    @property
    def closed(self) -> bool:
        return self._closing

    async def start(self, *, policy_name: str, job: Job) -> Observation:
        """Write the header and reset; release partial initialization on failure."""
        try:
            return await self._start(policy_name=policy_name, job=job)
        except BaseException as exc:
            await self._close_after_failure(exc)
            raise

    async def _start(self, *, policy_name: str, job: Job) -> Observation:
        async with self._lock:
            if self.closed:
                raise RuntimeError("Episode is closed")
            self._writer.header(
                task=self._task.spec.ref,
                scoring_hash=self._task.scoring_hash,
                policy=policy_name,
                seed=self._seed,
                repeat=job.repeat,
                job_id=job.job_id,
                salted=self._salt is not None,
            )
            if self._difficulty:
                return await self._task.reset(
                    self._seed, difficulty=self._difficulty, salt=self._salt
                )
            return await self._task.reset(self._seed, salt=self._salt)

    async def step(self, call: ToolCall) -> ToolResult:
        """Serialize actions against finalization; release resources on action failure."""
        if self.closed:
            return ToolResult(ok=False, error="Episode is closed")
        try:
            return await self._step(call)
        except BaseException as exc:
            await self._close_after_failure(exc)
            raise

    async def _step(self, call: ToolCall) -> ToolResult:
        async with self._lock:
            if self.closed:
                return ToolResult(ok=False, error="Episode is closed")
            if self._task.done():
                return ToolResult(ok=False, error="Episode is over")
            if self._steps_used >= self.max_steps:
                return ToolResult(
                    ok=False, error=f"Step budget of {self.max_steps} calls is used up"
                )
            result = await self._task.step(call)
            self._writer.step(self._steps_used, call, result)
            self._steps_used += 1
            return result

    async def close(self, error: str | None = None) -> tuple[StopReason, Score]:
        """Stop actions, freeze grading, finish the transcript, then release resources.

        Concurrent requests share one finalizer. Cancellation of a caller cannot cancel
        resource release. A grade failure is recorded and re-raised to the first caller;
        later closes return the frozen error outcome without grading again.
        """
        if self._close_task is not None and self._close_task.done() and self._outcome is not None:
            return self._outcome
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finalize(error))
        try:
            return await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # Complete cleanup before allowing the owner to finish its cancellation.
            try:
                await asyncio.shield(self._close_task)
            except Exception:
                logger.exception("Episode finalization failed during cancellation")
            raise
        except Exception:
            if error is not None and self._outcome is not None:
                return self._outcome
            raise

    async def _close_after_failure(self, exc: BaseException) -> None:
        try:
            await self.close(type(exc).__name__)
        except Exception:
            logger.exception("Episode finalization failed after %s", type(exc).__name__)

    async def _finalize(self, error: str | None) -> tuple[StopReason, Score]:
        async with self._lock:
            reason: StopReason = "error"
            score = Score(total=0.0, flags={"env_error": True})
            failure: Exception | None = None
            try:
                if error is None:
                    try:
                        if self._task.done():
                            reason = "task_done"
                        elif self._steps_used >= self.max_steps:
                            reason = "budget_exhausted"
                        else:
                            reason = "policy_stop"
                        graded = await self._task.grade()
                        flags = {**graded.flags, "budget_exhausted": reason == "budget_exhausted"}
                        score = graded.model_copy(update={"flags": flags})
                    except Exception as exc:
                        failure = exc
                        reason = "error"
                        error = type(exc).__name__
                self._outcome = (reason, score)
                self._writer.footer(score, reason, error)
                self._writer.close()
            finally:
                # A transcript I/O failure must not leak application resources either.
                try:
                    self._writer.abort()
                except OSError:
                    logger.exception("Could not close partial episode transcript")
                try:
                    await self._task.aclose()
                except Exception as exc:
                    self.cleanup_errors.append(type(exc).__name__)
                    logger.exception("Episode resource cleanup failed")
                    try:
                        self._writer.path.with_suffix(".cleanup.json").write_text(
                            json.dumps({"cleanup_errors": self.cleanup_errors}) + "\n"
                        )
                    except OSError:
                        logger.exception("Could not write cleanup diagnostics")
            if failure is not None:
                raise failure
            return self._outcome


__all__ = ["EpisodeSession"]

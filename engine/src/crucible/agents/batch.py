"""Salted direct-model batches, using the same session and transcript as the sidecar."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

from crucible.agents.compatible import CompatiblePolicy
from crucible.core.jobs import Job
from crucible.core.registry import get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.feedback.version import environment_hash, simulator_hash


async def agent_batch(
    task_ref: str,
    model: str,
    seeds: list[int],
    out: Path,
    *,
    base_url: str,
    api_key: str,
    salt: str,
    concurrency: int = 10,
) -> list[dict[str, object]]:
    """Write fresh transcripts; reject output reuse rather than mix different worlds."""
    started = time.perf_counter()
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=False)
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "task": task_ref,
                "model": model,
                "seeds": seeds,
                "base_url": base_url,
                "salt_sha256": hashlib.sha256(salt.encode()).hexdigest(),
                "simulator_sha256": simulator_hash(get_task(task_ref)),
                "environment_sha256": environment_hash(get_task(task_ref)),
            },
            indent=2,
        )
        + "\n"
    )
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120,
    ) as client:

        async def run(seed: int) -> dict[str, object]:
            async with semaphore:
                task = get_task(task_ref)
                policy = CompatiblePolicy(client, model)
                job = Job(task=task_ref, policy=model, seed=seed)
                session = EpisodeSession(
                    task, seed, TranscriptWriter(out / f"{seed}.jsonl"), salt=salt
                )
                error: str | None = None
                try:
                    obs = await session.start(policy_name=model, job=job)
                    await policy.reset(task.spec, task.tools(), task.instruction())
                    for _ in range(task.spec.max_steps):
                        if task.done():
                            break
                        call = await policy.act(obs)
                        if call is None:
                            break
                        result = await session.step(call)
                        obs = Observation(text=result_text(result), last_result=result)
                except asyncio.CancelledError:
                    error = "CancelledError"
                    raise
                except Exception as exc:
                    # Never put headers, API keys or provider response bodies in run artifacts.
                    error = type(exc).__name__
                finally:
                    reason, score = await session.close(error)
                return {
                    "seed": seed,
                    "reward": score.total,
                    "breakdown": score.breakdown,
                    "stop_reason": reason,
                    "error": error,
                }

        rows = await asyncio.gather(*(run(seed) for seed in seeds))
    (out / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    (out / "timing.json").write_text(
        json.dumps({"wall_seconds": time.perf_counter() - started}) + "\n"
    )
    return rows

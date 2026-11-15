"""Paired, salted bot measurements. Model results are an independent gate."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.feedback.version import environment_hash, simulator_hash

POLICIES = (
    "do_nothing",
    "loudest_alert",
    "graph_walk",
    "newest_change",
    "reference",
    "shotgun",
    "restart_all",
    "rollback_all",
    "guess_report",
)


async def measure(seeds: list[int], salt: str, out: Path) -> dict[str, object]:
    started = time.perf_counter()
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=False)
    source_hash = simulator_hash(get_task("oncall@1"))
    world_hash = environment_hash(get_task("oncall@1"))
    scores: dict[str, list[float]] = {}
    errors: list[str] = []
    for name in POLICIES:
        scores[name] = []
        for seed in seeds:
            task = get_task("oncall@1")
            policy = get_policy(name, "oncall@1")
            job = Job(task="oncall@1", policy=name, seed=seed)
            session = EpisodeSession(
                task, seed, TranscriptWriter(out / "episodes" / f"{name}-{seed}.jsonl"), salt=salt
            )
            error: str | None = None
            try:
                obs = await session.start(policy_name=name, job=job)
                await policy.reset(task.spec, task.tools(), task.instruction())
                for _ in range(task.spec.max_steps):
                    if task.done():
                        break
                    call = await policy.act(obs)
                    if call is None:
                        break
                    tool_result = await session.step(call)
                    obs = Observation(text=result_text(tool_result), last_result=tool_result)
            except Exception as exc:
                error = type(exc).__name__
                errors.append(f"{name}/{seed}: {error}")
            finally:
                _, score = await session.close(error)
            scores[name].append(score.total)
    means = {name: sum(values) / len(values) for name, values in scores.items()}
    checks = {
        "do_nothing_zero": all(v == 0 for v in scores["do_nothing"]),
        "loudest_alert_at_most_040": means["loudest_alert"] <= 0.40,
        "graph_walk_at_least_075": means["graph_walk"] >= 0.75,
        "reference_at_least_085": means["reference"] >= 0.85,
        # Strictly below is impossible when the loudest bot scores zero. Preserve that
        # finding instead of quietly weakening the requested gate to <=.
        "exploits_below_loudest": all(
            means[p] < means["loudest_alert"] for p in ("shotgun", "restart_all", "rollback_all")
        ),
        "no_errors": not errors,
    }
    result: dict[str, object] = {
        "task": "oncall@1",
        "wall_seconds": time.perf_counter() - started,
        "seeds": seeds,
        "salt_sha256": hashlib.sha256(salt.encode()).hexdigest(),
        "simulator_sha256": source_hash,
        "environment_sha256": world_hash,
        "scores": scores,
        "means": means,
        "checks": checks,
        "errors": errors,
        "passed": all(checks.values()),
        "model_gate": "not measured",
    }
    (out / "gates.json").write_text(json.dumps(result, indent=2) + "\n")
    return result

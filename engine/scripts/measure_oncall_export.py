"""Measure public rules on the exact private worlds in a Harbor export.

Run: uv run --directory engine python scripts/measure_oncall_export.py \
    --export ../runs/oncall-overlap-export-v2 --out ../runs/oncall-overlap-paired
Salt values remain in the original private export, never in this report.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from statistics import mean

import yaml

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.feedback.regrade import regrade
from crucible.feedback.version import environment_hash, simulator_hash


async def measure(export: Path, out: Path, policies: list[str]) -> None:
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=False)
    rows: list[dict[str, object]] = []
    composes = await asyncio.to_thread(
        lambda: sorted(export.glob("*/environment/docker-compose.yaml"))
    )
    if not composes:
        raise ValueError("No exported worlds found")
    for compose in composes:
        env = yaml.safe_load(compose.read_text())["services"]["mcp-server"]["environment"]
        task_ref, seed, salt = (
            str(env["SIDECAR_TASK"]),
            int(env["SIDECAR_SEED"]),
            str(env["SIDECAR_SALT"]),
        )
        for name in policies:
            task, policy = get_task(task_ref), get_policy(name, task_ref)
            path = out / "episodes" / f"{name}-{seed}.jsonl"
            session = EpisodeSession(task, seed, TranscriptWriter(path), salt=salt)
            invalid_calls = 0
            obs = await session.start(
                policy_name=name, job=Job(task=task_ref, policy=name, seed=seed)
            )
            await policy.reset(task.spec, task.tools(), task.instruction())
            try:
                while not task.done():
                    call = await policy.act(obs)
                    if call is None:
                        break
                    result = await session.step(call)
                    invalid_calls += not result.ok
                    obs = Observation(text=result_text(result), last_result=result)
            finally:
                reason, score = await session.close()
            replay = await regrade(path, salt=salt)
            row: dict[str, object] = {
                "task": task_ref,
                "seed": seed,
                "policy": name,
                "salt_sha256": hashlib.sha256(salt.encode()).hexdigest(),
                "environment_sha256": environment_hash(task),
                "policy_package_sha256": simulator_hash(task),
                "score": score.model_dump(mode="json"),
                "solved": task.solved(score),
                "steps": session.steps_used,
                "invalid_calls": invalid_calls,
                "stop_reason": reason,
                "replay_equal": replay["comparable"] and replay["reward"] == score.total,
            }
            rows.append(row)
            print(f"{name} seed={seed}: {score.total:.6f} solved={task.solved(score)}", flush=True)
    # No mutable simulator source should change during measurement.
    if len({str(row["environment_sha256"]) for row in rows}) != 1:
        raise ValueError("Simulator source changed during measurement")
    summaries = {}
    for name in policies:
        selected = [row for row in rows if row["policy"] == name]
        summaries[name] = {
            "episodes": len(selected),
            "mean": mean(row["score"]["total"] for row in selected),
            "solved": sum(bool(row["solved"]) for row in selected),
            "invalid_calls": sum(int(row["invalid_calls"]) for row in selected),
            "all_replays_equal": all(row["replay_equal"] for row in selected),
        }
    (out / "results.json").write_text(
        json.dumps({"export": str(export), "summaries": summaries, "rows": rows}, indent=2) + "\n"
    )
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--policies", default="reference,graph_walk,incident_runbook,newest_change,do_nothing"
    )
    args = parser.parse_args()
    asyncio.run(measure(args.export, args.out, args.policies.split(",")))

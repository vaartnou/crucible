"""Frozen schedules, trusted execution, and aggregation over every scheduled slot.

python -m crucible.envs.supplygym.scenarios freeze --out suite.json
python -m crucible.envs.supplygym.scenarios run --manifest suite.json --split dev --out runs/v2
"""

import argparse
import asyncio
import hashlib
import json
import math
import platform
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from crucible.core.episode import InfraError
from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.scoring import Score
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import TranscriptWriter, git_commit, read_transcript
from crucible.envs.supplygym.outcomes import RUBRIC, OutcomeTask


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def source_digest() -> str:
    """Include dirty source, configuration and packaged world/checkpoint data."""
    root = Path(__file__).parents[2]
    files = sorted(p for p in root.rglob("*") if p.suffix in {".py", ".yaml", ".json", ".npz"})
    return digest(
        {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    )


def runtime_identity() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        **{name: version(name) for name in ("numpy", "pydantic", "PyYAML")},
    }


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


TaskRef = Literal["supplygym@2", "supplygym-fresh@2", "supplygym-cala@2", "supplygym-cala@3"]


class Scenario(FrozenModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    task: TaskRef
    split: Literal["dev", "heldout"]
    seed: int = Field(ge=0)
    role: Literal["buyer"] = "buyer"
    weight: float = Field(gt=0)
    scoring_hash: str
    initial_state_hash: str


class Suite(FrozenModel):
    schema_version: Literal[1] = 1
    name: str
    source_hash: str
    runtime: dict[str, str] = Field(default_factory=runtime_identity)
    rubric: dict[str, float]
    max_steps: Literal[120, 200] = 120
    agent_timeout_seconds: float = Field(default=3600, gt=0)
    infrastructure_retries: int = Field(default=2, ge=0, le=10)
    scenarios: tuple[Scenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def fixed_schedule(self) -> Suite:
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Scenario IDs must be unique")
        identities = [(s.task, s.seed, s.role) for s in self.scenarios]
        if len(set(identities)) != len(identities):
            raise ValueError("Each task/seed/role occurs once, in exactly one split")
        seeds = {
            split: {s.seed for s in self.scenarios if s.split == split}
            for split in ("dev", "heldout")
        }
        if seeds["dev"] & seeds["heldout"]:
            raise ValueError("Development and held-out seed banks must be disjoint")
        if not math.isfinite(sum(s.weight for s in self.scenarios)):
            raise ValueError("Total scheduled weight must be finite")
        return self


def snapshot(task: OutcomeTask) -> dict[str, Any]:
    state = task.audit_state()
    assert state is not None
    return state


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def freeze_suite(
    tasks: tuple[TaskRef, ...] = ("supplygym@2", "supplygym-fresh@2", "supplygym-cala@2"),
) -> Suite:
    scenarios: list[Scenario] = []
    banks: list[tuple[Literal["dev", "heldout"], range]] = [
        ("dev", range(1000, 1010)),
        ("heldout", range(62000, 62010)),
    ]
    max_steps: Literal[120, 200] = 120
    for split, seeds in banks:
        for ref in tasks:
            for seed in seeds:
                task = get_task(ref)
                assert isinstance(task, OutcomeTask)
                if task.spec.max_steps == 200:
                    max_steps = 200
                await task.reset(seed)
                slug = ref.replace("@", "-v") if ref.endswith("@3") else ref.split("@")[0]
                scenarios.append(
                    Scenario(
                        id=f"{split}-{slug}-{seed}",
                        task=ref,
                        split=split,
                        seed=seed,
                        weight=1.0,
                        scoring_hash=task.scoring_hash,
                        initial_state_hash=digest(snapshot(task)),
                    )
                )
    name = (
        "supplygym-cala-v3" if tasks == ("supplygym-cala@3",) else "supplygym-outcomes-formulas-v1"
    )
    return Suite(
        name=name,
        source_hash=source_digest(),
        rubric=RUBRIC.model_dump(),
        max_steps=max_steps,
        scenarios=tuple(scenarios),
    )


def aggregate(scenarios: list[Scenario], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Missing and unfinished slots stay in the denominator. Infra leaves a provisional score."""
    if not scenarios:
        raise ValueError("A scored split must schedule at least one scenario")
    if set(results) - {s.id for s in scenarios}:
        raise ValueError("Result outside the scheduled scenario set")
    total_weight = sum(s.weight for s in scenarios)
    earned = 0.0
    unresolved = 0
    for scenario in scenarios:
        row = results.get(scenario.id, {"status": "not_started", "score": 0.0})
        score = row["score"]
        if score is None:
            score = 0.0
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Scenario score must be finite and in [0, 1]")
        if row["status"] in ("not_started", "running", "infrastructure_failure"):
            unresolved += 1
            score = 0.0
        earned += scenario.weight * score
    return {
        "overall_score": earned / total_weight,
        "scheduled_scenarios": len(scenarios),
        "scheduled_weight": total_weight,
        "verified_weighted_points": earned,
        "unresolved_scenarios": unresolved,
        "valid_for_comparison": unresolved == 0,
    }


async def run_attempt(
    suite: Suite,
    scenario: Scenario,
    policy_name: str,
    directory: Path,
) -> dict[str, Any]:
    await asyncio.to_thread(directory.mkdir)
    task = get_task(scenario.task)
    assert isinstance(task, OutcomeTask)
    policy = get_policy(policy_name, scenario.task)
    writer = TranscriptWriter(directory / "trace.jsonl")
    session = EpisodeSession(task, scenario.seed, writer)
    job = Job(task=scenario.task, policy=policy_name, seed=scenario.seed)
    start = time.perf_counter()
    agent_seconds = 0.0
    invalid = 0
    stage = "environment"
    status = "running"
    error = None
    score = Score(total=0.0)
    try:
        observation = await session.start(policy_name=policy_name, job=job)
        initial = snapshot(task)
        if digest(initial) != scenario.initial_state_hash:
            raise ValueError("Initial state differs from frozen manifest")
        write_json(directory / "initial_state.json", initial)
        stage = "agent"
        before = time.perf_counter()
        try:
            async with asyncio.timeout(suite.agent_timeout_seconds):
                await policy.reset(task.spec, task.tools(), task.instruction())
        finally:
            agent_seconds += time.perf_counter() - before
        for _ in range(min(suite.max_steps, task.spec.max_steps)):
            if task.done():
                break
            stage = "agent"
            remaining = suite.agent_timeout_seconds - agent_seconds
            if remaining <= 0:
                raise TimeoutError("Agent time budget exhausted")
            before = time.perf_counter()
            try:
                async with asyncio.timeout(remaining):
                    call = await policy.act(observation)
            finally:
                agent_seconds += time.perf_counter() - before
            if agent_seconds > suite.agent_timeout_seconds:
                raise TimeoutError("Agent time budget exhausted")
            if call is None:
                break
            stage = "environment"
            result = await session.step(call)
            invalid += int(not result.ok)
            observation = Observation(text=result_text(result), last_result=result)
        stage = "environment"
        stop_reason, score = await session.close()
        status = "completed" if score.flags["completed"] else "unfinished"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, InfraError) or stage == "environment":
            status = "infrastructure_failure"
        elif isinstance(exc, TimeoutError):
            status = "budget_exhausted"
        else:
            status = "agent_error"
        stop_reason = "error"
        # Preserve every attempted call even when reset, transport or verification fails.
        writer.footer(Score(total=0.0, flags={status: True}), "error", error)
        writer.close()
    finally:
        writer.close()
    final = task.audit_state()
    write_json(directory / "final_state.json", final)
    local_policy = type(policy).__module__ in {
        "crucible.envs.supplygym.policies.baselines",
        "crucible.policies.baselines",
    }
    result = {
        "status": status,
        "score": None if status == "infrastructure_failure" else score.total,
        "verified": score.model_dump(),
        "stop_reason": stop_reason,
        "error": error,
        "wall_seconds": time.perf_counter() - start,
        "agent_seconds": agent_seconds,
        "invalid_actions": invalid,
        "tool_calls": session.steps_used,
        "provider_cost_usd": 0.0 if local_policy else None,
        "provider_tokens": 0 if local_policy else None,
        "trace": str(directory / "trace.jsonl"),
        "initial_state": str(directory / "initial_state.json"),
        "final_state": str(directory / "final_state.json"),
        "final_state_hash": digest(final),
        "agent_settings": {
            "registered_policy": policy_name,
            "implementation": f"{type(policy).__module__}.{type(policy).__qualname__}",
            "configuration": "registered factory defaults",
            "instruction": task.instruction(),
        },
    }
    write_json(directory / "attempt.json", result)
    return result


async def run_suite(
    suite: Suite,
    split: Literal["dev", "heldout"],
    policy_name: str,
    out: Path,
) -> dict[str, Any]:
    if suite.source_hash != source_digest() or suite.rubric != RUBRIC.model_dump():
        raise ValueError("Source or rubric changed after schedule was frozen")
    if suite.runtime != runtime_identity():
        raise ValueError("Runtime differs from frozen manifest")
    scenarios = [s for s in suite.scenarios if s.split == split]
    for scenario in scenarios:
        if get_task(scenario.task).scoring_hash != scenario.scoring_hash:
            raise ValueError("Scoring identity differs from manifest")
        get_policy(policy_name, scenario.task)
    aggregate(scenarios, {})
    # Exclusive directory creation prevents concurrent runs and accidental result replacement.
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=False)
    write_json(out / "manifest.json", suite.model_dump(mode="json"))
    rows: dict[str, dict[str, Any]] = {
        s.id: {
            **s.model_dump(),
            "status": "not_started",
            "score": 0.0,
            "attempts": [],
        }
        for s in scenarios
    }

    def save() -> dict[str, Any]:
        attempts = [a for row in rows.values() for a in row["attempts"]]
        costs = [a["provider_cost_usd"] for a in attempts]
        report = {
            "manifest_hash": digest(suite.model_dump(mode="json")),
            "source_hash": suite.source_hash,
            "git_commit": git_commit(),
            "policy": policy_name,
            "split": split,
            **aggregate(scenarios, rows),
            "telemetry": {
                key: sum(a[key] for a in attempts)
                for key in ("wall_seconds", "agent_seconds", "invalid_actions", "tool_calls")
            },
            "provider_cost_usd": sum(costs) if all(c is not None for c in costs) else None,
            "scenarios": list(rows.values()),
        }
        write_json(out / "report.json", report)
        return report

    save()
    for scenario in scenarios:
        row = rows[scenario.id]
        scenario_dir = out / scenario.id
        scenario_dir.mkdir()
        for attempt in range(suite.infrastructure_retries + 1):
            row["status"] = "running"
            save()
            result = await run_attempt(
                suite, scenario, policy_name, scenario_dir / f"attempt-{attempt}"
            )
            row["attempts"].append(result)
            row["status"], row["score"] = result["status"], result["score"]
            save()
            if result["status"] != "infrastructure_failure":
                break
    return save()


async def audit_report(report_path: Path) -> dict[str, Any]:
    """Rebuild completed and agent-failed outcomes from tool calls, without invoking an agent."""
    report = json.loads(await asyncio.to_thread(report_path.read_text, encoding="utf-8"))
    manifest_path = report_path.with_name("manifest.json")
    suite = Suite.model_validate_json(
        await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
    )
    if digest(suite.model_dump(mode="json")) != report["manifest_hash"]:
        raise ValueError("Manifest hash mismatch")
    if source_digest() != suite.source_hash:
        raise ValueError("Replay requires the recorded source version")
    if suite.runtime != runtime_identity():
        raise ValueError("Replay requires the recorded runtime")
    scheduled = [s for s in suite.scenarios if s.split == report["split"]]
    rows = {r["id"]: r for r in report["scenarios"]}
    if len(rows) != len(report["scenarios"]) or set(rows) != {s.id for s in scheduled}:
        raise ValueError("Report must contain exactly one row per scheduled scenario")
    for scenario in scheduled:
        row = rows[scenario.id]
        if row["status"] in ("not_started", "running", "infrastructure_failure"):
            continue
        attempt = row["attempts"][-1]
        trace = read_transcript(Path(attempt["trace"]))
        if (trace.header.task, trace.header.seed, trace.header.policy) != (
            scenario.task,
            scenario.seed,
            report["policy"],
        ):
            raise ValueError("Trace identity mismatch")
        task = get_task(scenario.task)
        assert isinstance(task, OutcomeTask)
        limit = min(suite.max_steps, task.spec.max_steps)
        if trace.header.scoring_hash != scenario.scoring_hash or len(trace.steps) > limit:
            raise ValueError("Trace rubric or call budget mismatch")
        await task.reset(scenario.seed)
        if digest(snapshot(task)) != scenario.initial_state_hash:
            raise ValueError("Replay initial state mismatch")
        for step in trace.steps:
            result = await task.step(step.call)
            if result != step.result:
                raise ValueError("Replay tool result mismatch")
        score = await task.grade()
        if digest(snapshot(task)) != attempt["final_state_hash"]:
            raise ValueError("Replay final state mismatch")
        if score.total != row["score"] or score.total != trace.footer.score.total:
            raise ValueError("Replay score mismatch")
    recomputed = aggregate(scheduled, rows)
    if any(report[key] != value for key, value in recomputed.items()):
        raise ValueError("Aggregate mismatch")
    return recomputed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--out", type=Path, required=True)
    freeze.add_argument(
        "--tasks",
        default="supplygym@2,supplygym-fresh@2,supplygym-cala@2",
    )
    run = commands.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--split", choices=("dev", "heldout"), required=True)
    run.add_argument(
        "--policies",
        default="ss,base_stock,fixed_lot,fill_adjusted_base_stock,reference,do_nothing,stop_early",
    )
    run.add_argument("--out", type=Path, required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "audit":
        print(json.dumps(await audit_report(args.report), indent=2))
        return
    if args.command == "freeze":
        selected = TypeAdapter(tuple[TaskRef, ...]).validate_python(
            tuple(item for item in args.tasks.split(",") if item)
        )
        suite = await freeze_suite(selected)
        with args.out.open("x", encoding="utf-8") as stream:
            stream.write(suite.model_dump_json(indent=2) + "\n")
        print(f"Frozen {len(suite.scenarios)} scenarios in {args.out}")
        return
    suite = Suite.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    policies = args.policies.split(",")
    if len(set(policies)) != len(policies):
        parser.error("Each policy must occur once")
    for index, policy in enumerate(policies):
        report = await run_suite(suite, args.split, policy, args.out / f"agent-{index}")
        print(json.dumps({k: v for k, v in report.items() if k != "scenarios"}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

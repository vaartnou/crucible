"""Run matched Cala baselines and an OpenCode Harbor evaluation.

Each invocation creates a new, private output directory. Exports (and their salts) are
created once, before any rollout. Never reuse an output directory for a new run.
"""

import argparse
import asyncio
import getpass
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.envs.supplygym.cala_upper_bound import surplus_upper_bound
from crucible.envs.supplygym.play import CALA_REGIME, Play
from crucible.export.harbor import (
    ExportError,
    build_engine_artifacts,
    export_harbor,
    task_folder_name,
)
from crucible.server.spectator import Spectator

TASK = "supplygym-cala@3"
SPECTATOR_BASELINES = ("reference", "ss", "base_stock", "fixed_lot", "fill_adjusted_base_stock")
INCOMPLETE_BASELINES = ("stop_early", "do_nothing")
BASELINES = (*SPECTATOR_BASELINES, *INCOMPLETE_BASELINES)
SIDECAR_SPECTATOR_TARGET = "/app/spectator-events.jsonl"


def read_dotenv_key(key_env: str, paths: Sequence[Path]) -> str:
    """Read `key_env` from the first matching `.env` file without evaluating shell syntax."""
    for path in paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            lhs, rhs = line.split("=", 1)
            name = lhs.removeprefix("export ").strip()
            if name == key_env:
                value = rhs.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if value:
                    return value
    return ""


def configure_spectator(
    compose: Path, events: Path, run_id: str, strategy: str = "opencode"
) -> None:
    """Bind one pre-created event file only into the sidecar."""
    config = yaml.safe_load(compose.read_text(encoding="utf-8"))
    sidecar = config["services"]["mcp-server"]
    events = events.resolve()
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch(exist_ok=False)
    # The container runs as uid 10001. Mount under /app (non-sticky) rather than /tmp so
    # Linux fs.protected_regular allows uid 10001 to append to the host-created file.
    events.chmod(0o666)
    sidecar.setdefault("volumes", []).append(
        {"type": "bind", "source": events.as_posix(), "target": SIDECAR_SPECTATOR_TARGET}
    )
    sidecar["environment"].update(
        {
            "SPECTATOR_PATH": SIDECAR_SPECTATOR_TARGET,
            "SPECTATOR_RUN_ID": run_id,
            "SPECTATOR_STRATEGY": strategy,
        }
    )
    compose.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


async def run_baseline(
    seed: int, salt: str, policy_name: str, output: Path, run_id: str
) -> dict[str, Any]:
    task = get_task(TASK)
    policy = get_policy(policy_name, TASK)
    job = Job(task=TASK, policy=policy_name, seed=seed)
    session = EpisodeSession(task, seed, TranscriptWriter(output / "transcript.jsonl"), salt=salt)
    obs = await session.start(policy_name=policy_name, job=job)
    spectator = Spectator(output / "events.jsonl", task, seed, salt, run_id, policy_name)
    try:
        await policy.reset(task.spec, task.tools(), task.instruction())
        for _ in range(session.max_steps):
            if task.done():
                break
            call = await policy.act(obs)
            if call is None:
                break
            before = spectator.before_step(call)
            result = await session.step(call)
            spectator.after_step(call, result, before)
            if not result.ok:
                raise RuntimeError(f"Policy {policy_name} made an invalid call on seed {seed}")
            obs = Observation(text=result_text(result), last_result=result)
        reason, score = await session.close()
        spectator.finalize(reason, score)
        expected_complete = policy_name not in INCOMPLETE_BASELINES
        if bool(score.flags.get("completed")) != expected_complete:
            raise RuntimeError(
                f"Policy {policy_name} had unexpected completion state on seed {seed}"
            )
        if not expected_complete and score.total != 0.0:
            raise RuntimeError(
                f"Incomplete policy {policy_name} scored {score.total} on seed {seed}"
            )
        return {
            "seed": seed,
            "policy": policy_name,
            "reason": reason,
            "score": score.model_dump(mode="json"),
        }
    except Exception:
        if not session.closed:
            reason, score = await session.close("baseline_error")
            spectator.finalize(reason, score)
        raise


def _read_harbor_control_grade(out: Path, job_name: str, seed: int) -> dict[str, Any] | None:
    name = task_folder_name(TASK, seed)
    trials = list((out / "jobs" / job_name).glob(f"{name}__*/result.json"))
    if len(trials) != 1:
        return None
    trial = trials[0]
    result = json.loads(trial.read_text(encoding="utf-8"))
    grade_path = trial.parent / "verifier" / "grade.json"
    if result.get("exception_info") or not grade_path.is_file():
        return None
    grade = json.loads(grade_path.read_text(encoding="utf-8"))
    return {
        "trial": str(trial.parent),
        "reward": grade["reward"],
        "operating_surplus": grade["breakdown"]["operating_surplus"],
        "service": grade["breakdown"]["service"],
        "completed": bool(grade["flags"].get("completed")),
        "steps": grade["steps"],
        "stop_reason": grade["stop_reason"],
    }


def collect_results(
    out: Path, bounds: dict[int, float], *, check_harbor_controls: bool = False
) -> bool:
    """Report only verified trials whose event identity matches the local realization."""
    seeds = list(bounds)
    baseline_scores = json.loads((out / "baseline-scores.json").read_text(encoding="utf-8"))
    ref_by_seed = {
        row["seed"]: row["score"] for row in baseline_scores if row["policy"] == "reference"
    }
    rows: list[dict[str, Any]] = []
    for seed in seeds:
        name = task_folder_name(TASK, seed)
        trials = list((out / "jobs" / "opencode").glob(f"{name}__*/result.json"))
        row: dict[str, Any] = {"seed": seed, "status": "missing_or_ambiguous_trial"}
        if len(trials) == 1:
            trial = trials[0]
            result = json.loads(trial.read_text(encoding="utf-8"))
            trace = trial.parent / "verifier" / "transcript.json"
            events = out / "live" / f"seed-{seed}" / "events.jsonl"
            live = (
                [
                    json.loads(line)
                    for line in events.read_text(encoding="utf-8").splitlines()
                    if line
                ]
                if events.is_file()
                else []
            )
            local_finals: list[dict[str, Any]] = []
            for policy in BASELINES:
                local_path = out / "baselines" / f"seed-{seed}" / policy / "events.jsonl"
                local_finals.extend(
                    event
                    for line in local_path.read_text(encoding="utf-8").splitlines()
                    if line and (event := json.loads(line))["event"] == "final"
                )
            finals = [event for event in live if event.get("event") == "final"]
            identities = {event.get("scenario_comparison_id") for event in live + local_finals}
            if result.get("exception_info"):
                row["status"] = "trial_error"
            elif not trace.is_file() or len(finals) != 1 or len(local_finals) != len(BASELINES):
                row["status"] = "missing_verified_result"
            elif len(identities) != 1 or None in identities:
                row["status"] = "realization_mismatch"
            elif any(
                event["score"]["breakdown"].get("surplus_upper_bound") != bounds[seed]
                or event["scorer_version"] != finals[0]["scorer_version"]
                or event["simulator_version"] != finals[0]["simulator_version"]
                for event in [*local_finals, *finals]
            ):
                row["status"] = "scoring_configuration_mismatch"
            elif json.loads(trace.read_text(encoding="utf-8"))[-1]["score"] != finals[0]["score"]:
                row["status"] = "verifier_score_mismatch"
            else:
                control_ok = True
                if check_harbor_controls:
                    oracle_grade = _read_harbor_control_grade(out, "oracle", seed)
                    nop_grade = _read_harbor_control_grade(out, "nop", seed)
                    ref_score = ref_by_seed.get(seed)
                    if (
                        oracle_grade is None
                        or nop_grade is None
                        or ref_score is None
                        or not oracle_grade["completed"]
                        or not math.isclose(
                            oracle_grade["reward"], ref_score["total"], abs_tol=1e-9
                        )
                        or nop_grade["completed"]
                        or nop_grade["reward"] != 0.0
                    ):
                        row["status"] = "harbor_control_mismatch"
                        control_ok = False
                    else:
                        row["harbor_oracle"] = oracle_grade
                        row["harbor_nop"] = nop_grade
                if control_ok:
                    row.update(
                        status="verified",
                        score=finals[0]["score"],
                        trial=str(trial.parent),
                        scenario_comparison_id=next(iter(identities)),
                    )
        rows.append(row)
    valid = all(row["status"] == "verified" for row in rows)
    report: dict[str, Any] = {"task": TASK, "scenarios": rows, "valid_for_comparison": valid}
    if valid:
        aggregates: dict[str, Any] = {}
        for policy in BASELINES:
            scores = [row["score"] for row in baseline_scores if row["policy"] == policy]
            if len(scores) != len(seeds):
                raise ValueError(f"Missing baseline scores for {policy}")
            aggregates[policy] = {
                "mean_score": sum(score["total"] for score in scores) / len(seeds),
                "mean_operating_surplus_usd": sum(
                    score["breakdown"]["operating_surplus"] for score in scores
                )
                / len(seeds),
                "mean_service": sum(score["breakdown"]["service"] for score in scores) / len(seeds),
            }
        if check_harbor_controls:
            aggregates["harbor_oracle"] = {
                "mean_score": sum(row["harbor_oracle"]["reward"] for row in rows) / len(seeds),
                "mean_operating_surplus_usd": sum(
                    row["harbor_oracle"]["operating_surplus"] for row in rows
                )
                / len(seeds),
                "mean_service": sum(row["harbor_oracle"]["service"] for row in rows) / len(seeds),
            }
            aggregates["harbor_nop"] = {
                "mean_score": sum(row["harbor_nop"]["reward"] for row in rows) / len(seeds),
                "mean_operating_surplus_usd": sum(
                    row["harbor_nop"]["operating_surplus"] for row in rows
                )
                / len(seeds),
                "mean_service": sum(row["harbor_nop"]["service"] for row in rows) / len(seeds),
            }
        scores = [row["score"] for row in rows]
        aggregates["opencode"] = {
            "mean_score": sum(score["total"] for score in scores) / len(seeds),
            "mean_operating_surplus_usd": sum(
                score["breakdown"]["operating_surplus"] for score in scores
            )
            / len(seeds),
            "mean_service": sum(score["breakdown"]["service"] for score in scores) / len(seeds),
        }
        report["aggregates"] = aggregates
    (out / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return bool(report["valid_for_comparison"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-seed", type=int, default=62000)
    parser.add_argument("--count", type=int, default=3, help="Number of consecutive simulations")
    parser.add_argument(
        "--model",
        required=True,
        help="Provider/model, e.g. anthropic/claude-sonnet-5-5 or google/gemini-3.8-flash",
    )
    parser.add_argument(
        "--key-env",
        default="ANTHROPIC_API_KEY",
        help="Provider key env var (e.g. ANTHROPIC_API_KEY or GOOGLE_GENERATIVE_AI_API_KEY)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional .env file to read --key-env from when not exported in the environment",
    )
    parser.add_argument(
        "--prompt-key", action="store_true", help="Prompt for key without echoing it"
    )
    parser.add_argument(
        "--harbor-controls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Harbor oracle and nop on the exported dataset before OpenCode (default: true)",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--out", required=True, type=Path, help="New output directory")
    parser.add_argument("--harbor", default="harbor", help="Harbor executable")
    args = parser.parse_args()
    if args.start_seed < 0 or args.count < 1 or args.concurrency < 1:
        parser.error("start-seed must be nonnegative; count and concurrency must be positive")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_env):
        parser.error("invalid key environment variable name")
    if not re.fullmatch(r"[^/\s]+/[^\s]+", args.model):
        parser.error("model must be provider/model")
    return args


def harbor_control_command(args: argparse.Namespace, out: Path, agent: str) -> list[str]:
    """Run a deterministic Harbor control agent (`oracle` or `nop`) across the dataset."""
    return [
        args.harbor,
        "run",
        "-p",
        str(out / "dataset"),
        "-a",
        agent,
        "-o",
        str(out / "jobs"),
        "--job-name",
        agent,
        "-n",
        str(args.concurrency),
    ]


def harbor_command(args: argparse.Namespace, out: Path) -> list[str]:
    """Schedule the whole dataset; concurrency is not a task limit."""
    return [
        args.harbor,
        "run",
        "-p",
        str(out / "dataset"),
        "-a",
        "opencode",
        "-m",
        args.model,
        "--ak",
        "version=1.19.5",
        "--ak",
        "disable_web_search=true",
        "--api-key-name",
        args.key_env,
        "--ae",
        f"{args.key_env}=${{{args.key_env}}}",
        "-o",
        str(out / "jobs"),
        "--job-name",
        "opencode",
        "-n",
        str(args.concurrency),
    ]


def _resolve_api_key(args: argparse.Namespace) -> str:
    if args.prompt_key:
        return getpass.getpass(f"{args.key_env}: ")
    if value := os.environ.get(args.key_env, ""):
        return value
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [args.env_file] if args.env_file is not None else []
    candidates.extend([Path.cwd() / ".env", repo_root / ".env"])
    return read_dotenv_key(args.key_env, candidates)


async def run(args: argparse.Namespace) -> int:
    key = _resolve_api_key(args)
    if not key:
        raise ValueError(
            f"No provider key in {args.key_env}; export it, add to .env, "
            "pass --env-file, or use --prompt-key"
        )
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    seeds = list(range(args.start_seed, args.start_seed + args.count))
    invocation_id = uuid4().hex
    artifacts = build_engine_artifacts(out / "build")
    folders = export_harbor(TASK, seeds, out / "dataset", artifacts)
    scenarios: dict[int, str] = {}
    bounds: dict[int, float] = {}
    for seed, folder in zip(seeds, folders, strict=True):
        compose = folder / "environment" / "docker-compose.yaml"
        env = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["mcp-server"][
            "environment"
        ]
        salt = env["SIDECAR_SALT"]
        if not salt or env["SIDECAR_TASK"] != TASK or int(env["SIDECAR_SEED"]) != seed:
            raise ValueError("Export identity mismatch")
        scenarios[seed] = salt
        bounds[seed] = surplus_upper_bound(Play(seed, CALA_REGIME, salt)) / 100
    (out / "surplus-bounds.json").write_text(
        json.dumps({"task": TASK, "surplus_upper_bound_usd": bounds}, indent=2) + "\n",
        encoding="utf-8",
    )
    baselines: list[dict[str, Any]] = []
    for seed, salt in scenarios.items():
        for policy in BASELINES:
            baselines.append(
                await run_baseline(
                    seed,
                    salt,
                    policy,
                    out / "baselines" / f"seed-{seed}" / policy,
                    f"{invocation_id}-seed-{seed}-{policy}",
                )
            )
    (out / "baseline-scores.json").write_text(
        json.dumps(baselines, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    child_env = os.environ.copy()
    child_env[args.key_env] = key
    control_failure = 0
    if args.harbor_controls:
        for control_agent in ("oracle", "nop"):
            print(
                f"Starting Harbor {control_agent} for {len(seeds)} seeds; artifacts: {out}",
                flush=True,
            )
            ctrl_res = await asyncio.to_thread(
                subprocess.run,
                harbor_control_command(args, out, control_agent),
                env=child_env,
                check=False,
            )
            if ctrl_res.returncode:
                control_failure = ctrl_res.returncode
                job_dir = out / "jobs" / control_agent
                print(
                    f"Harbor {control_agent} failed with exit code {ctrl_res.returncode}; "
                    f"inspect {job_dir}",
                    file=sys.stderr,
                )
    for seed, folder in zip(seeds, folders, strict=True):
        configure_spectator(
            folder / "environment" / "docker-compose.yaml",
            out / "live" / f"seed-{seed}" / "events.jsonl",
            f"{invocation_id}-seed-{seed}-opencode",
        )
    manifest: list[dict[str, Any]] = []
    for seed in seeds:
        base = out / "baselines" / f"seed-{seed}"
        first_event = json.loads(
            (base / SPECTATOR_BASELINES[0] / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        manifest.append(
            {
                "seed": seed,
                "scenario_comparison_id": first_event["scenario_comparison_id"],
                "events": {
                    **{
                        policy: (
                            Path("baselines") / f"seed-{seed}" / policy / "events.jsonl"
                        ).as_posix()
                        for policy in SPECTATOR_BASELINES
                    },
                    "opencode": (Path("live") / f"seed-{seed}" / "events.jsonl").as_posix(),
                },
            }
        )
    (out / "spectator-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    command = harbor_command(args, out)
    print(f"Starting Harbor opencode for {len(seeds)} seeds; artifacts: {out}", flush=True)
    result = await asyncio.to_thread(subprocess.run, command, env=child_env, check=False)
    if result.returncode:
        print(
            f"Harbor failed with exit code {result.returncode}; inspect {out / 'jobs'}",
            file=sys.stderr,
        )
    valid = collect_results(out, bounds, check_harbor_controls=args.harbor_controls)
    if not valid:
        print(f"Some trials are unresolved; inspect {out / 'report.json'}", file=sys.stderr)
    return control_failure or result.returncode or (0 if valid else 1)


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except (OSError, ValueError, RuntimeError, ExportError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

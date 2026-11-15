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
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.scoring import Score
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
INCOMPLETE_BASELINES = ("do_nothing", "stop_early")
BASELINES = (
    "reference",
    "ss",
    "base_stock",
    "fixed_lot",
    "fill_adjusted_base_stock",
    *INCOMPLETE_BASELINES,
)


def configure_spectator(
    compose: Path, events: Path, run_id: str, strategy: str = "opencode"
) -> None:
    """Bind one pre-created event file only into the sidecar."""
    config = yaml.safe_load(compose.read_text(encoding="utf-8"))
    sidecar = config["services"]["mcp-server"]
    events = events.resolve()
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch(exist_ok=False)
    # The container runs as uid 10001. Parent directories remain private to the host user.
    events.chmod(0o666)
    sidecar.setdefault("volumes", []).append(
        {"type": "bind", "source": events.as_posix(), "target": "/tmp/spectator-events.jsonl"}
    )
    sidecar["environment"].update(
        {
            "SPECTATOR_PATH": "/tmp/spectator-events.jsonl",
            "SPECTATOR_RUN_ID": run_id,
            "SPECTATOR_STRATEGY": strategy,
        }
    )
    compose.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


async def run_baseline(seed: int, salt: str, policy_name: str, output: Path, run_id: str) -> dict:
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
        if policy_name not in INCOMPLETE_BASELINES and not score.flags.get("completed"):
            raise RuntimeError(f"Policy {policy_name} did not complete seed {seed}")
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


def read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Expected a JSON object in the spectator stream")
            events.append(event)
    return events


def verify_trial(
    out: Path, seed: int, bound: float, trial: Path
) -> tuple[dict[str, Any], dict[str, Score]]:
    result = json.loads(trial.read_text(encoding="utf-8"))
    if result.get("exception_info"):
        return {"status": "trial_error"}, {}

    trace = (trial.parent / "verifier" / "transcript.json").read_text(encoding="utf-8")
    transcript = json.loads(trace) if trace.strip() else []
    streams = {"opencode": out / "live" / f"seed-{seed}" / "events.jsonl"}
    streams.update(
        {
            policy: out / "baselines" / f"seed-{seed}" / policy / "events.jsonl"
            for policy in BASELINES
        }
    )
    finals = {}
    identities = set()
    for strategy, path in streams.items():
        events = read_events(path)
        endings = [event for event in events if event.get("event") == "final"]
        if len(endings) != 1:
            return {"status": "missing_verified_result"}, {}
        finals[strategy] = endings[0]
        identities.update(event.get("scenario_comparison_id") for event in events)

    if not transcript:
        return {"status": "missing_verified_result"}, {}
    if len(identities) != 1 or None in identities:
        return {"status": "realization_mismatch"}, {}

    live = finals["opencode"]
    scores = {strategy: Score.model_validate(event["score"]) for strategy, event in finals.items()}
    if any(
        scores[strategy].breakdown.get("surplus_upper_bound") != bound
        or event["scorer_version"] != live["scorer_version"]
        or event["simulator_version"] != live["simulator_version"]
        for strategy, event in finals.items()
    ):
        return {"status": "scoring_configuration_mismatch"}, {}
    if transcript[-1]["score"] != live["score"]:
        return {"status": "verifier_score_mismatch"}, {}
    if any(
        not 0 <= score.total <= 1
        or "operating_surplus" not in score.breakdown
        or not all(map(math.isfinite, score.breakdown.values()))
        for score in scores.values()
    ):
        return {"status": "invalid_trial_artifacts"}, {}
    return {
        "status": "verified",
        "score": scores["opencode"].model_dump(mode="json"),
        "trial": str(trial.parent),
        "scenario_comparison_id": next(iter(identities)),
    }, scores


def collect_results(out: Path, bounds: dict[int, float], harbor_exit_code: int = 0) -> bool:
    """Report only verified trials whose event identity matches the local realization."""
    rows = []
    verified_scores = []
    for seed, bound in bounds.items():
        name = task_folder_name(TASK, seed)
        trials = list((out / "jobs" / "opencode").glob(f"{name}__*/result.json"))
        row = {"status": "missing_or_ambiguous_trial"}
        if len(trials) == 1:
            try:
                row, scores = verify_trial(out, seed, bound, trials[0])
            except FileNotFoundError:
                row = {"status": "missing_verified_result"}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                row = {"status": "invalid_trial_artifacts"}
            else:
                if scores:
                    verified_scores.append(scores)
        rows.append({"seed": seed, **row})

    valid = bool(bounds) and harbor_exit_code == 0 and len(verified_scores) == len(bounds)
    report = {
        "task": TASK,
        "scenarios": rows,
        "harbor_exit_code": harbor_exit_code,
        "valid_for_comparison": valid,
    }
    if valid:
        report["aggregates"] = {
            strategy: {
                "mean_score": sum(row[strategy].total for row in verified_scores) / len(bounds),
                "mean_operating_surplus_usd": sum(
                    row[strategy].breakdown["operating_surplus"] for row in verified_scores
                )
                / len(bounds),
            }
            for strategy in (*BASELINES, "opencode")
        }
    (out / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return valid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-seed", type=int, default=0)
    parser.add_argument("--count", type=int, default=1, help="Number of consecutive simulations")
    parser.add_argument(
        "--model", required=True, help="Provider/model, e.g. google/gemini-3.8-flash"
    )
    parser.add_argument(
        "--key-env",
        default="GOOGLE_GENERATIVE_AI_API_KEY",
        help="Provider key environment variable",
    )
    parser.add_argument(
        "--prompt-key", action="store_true", help="Prompt for key without echoing it"
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


def harbor_command(args: argparse.Namespace, out: Path, key: str) -> list[str]:
    """Build the execution argv, including the credential for the agent container."""
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
        f"{args.key_env}={key}",
        "-o",
        str(out / "jobs"),
        "--job-name",
        "opencode",
        "-n",
        str(args.concurrency),
    ]


def run_harbor(command: list[str], env: dict[str, str], key: str) -> int:
    """Relay Harbor diagnostics with the provider credential redacted."""
    with subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            print(line.replace(key, "<redacted>"), end="", flush=True)
        return process.wait()


async def run(args: argparse.Namespace) -> int:
    key = (
        getpass.getpass(f"{args.key_env}: ")
        if args.prompt_key
        else os.environ.get(args.key_env, "")
    )
    if not key:
        raise ValueError(f"No provider key in {args.key_env}; set it or use --prompt-key")
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    seeds = list(range(args.start_seed, args.start_seed + args.count))
    invocation_id = uuid4().hex
    artifacts = build_engine_artifacts(out / "build")
    folders = export_harbor(TASK, seeds, out / "dataset", artifacts)
    scenarios = {}
    bounds = {}
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
        configure_spectator(
            compose,
            out / "live" / f"seed-{seed}" / "events.jsonl",
            f"{invocation_id}-seed-{seed}-opencode",
        )
    (out / "surplus-bounds.json").write_text(
        json.dumps({"task": TASK, "surplus_upper_bound_usd": bounds}, indent=2) + "\n",
        encoding="utf-8",
    )
    baselines = []
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
    manifest = []
    for seed in seeds:
        base = out / "baselines" / f"seed-{seed}"
        first_event = json.loads(
            (base / BASELINES[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
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
                        for policy in BASELINES
                    },
                    "opencode": (Path("live") / f"seed-{seed}" / "events.jsonl").as_posix(),
                },
            }
        )
    (out / "spectator-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    child_env = os.environ.copy()
    child_env[args.key_env] = key
    print(f"Starting Harbor for {len(seeds)} seeds; artifacts: {out}", flush=True)
    exit_code = await asyncio.to_thread(
        run_harbor, harbor_command(args, out, key), child_env, key
    )
    if exit_code:
        print(
            f"Harbor failed with exit code {exit_code}; inspect {out / 'jobs'}",
            file=sys.stderr,
        )
    valid = collect_results(out, bounds, exit_code)
    if not valid:
        print(f"Some trials are unresolved; inspect {out / 'report.json'}", file=sys.stderr)
    return exit_code or (0 if valid else 1)


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except (OSError, ValueError, RuntimeError, ExportError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

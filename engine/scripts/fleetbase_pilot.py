#!/usr/bin/env python3
"""Freeze and run a sequential three-world Fleetbase pilot through Harbor 0.26.0.

Ported to the v2 tool set (plan step 5): the script is tool-agnostic, and a freeze
creates the new experiment identity ``fleetbase-native-v2-pilot-<candidate>`` with
three fresh salted worlds. v1 pilot records are never resumed by it.

The freeze stores private salts/credentials under the supplied ignored output
folder. Run only after clean local/Harbor reference validation. Failed attempts
are append-only; rerunning resumes completed valid trials and retries failures
with a new attempt number. Never print resolved model credentials.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import JsonValue

from crucible.core.registry import get_task
from crucible.envs.fleetbase_dispatch.world import make_world
from crucible.experiments.paired import OPERATORS, PilotSpec, Trial, analyze
from crucible.export.harbor import build_engine_artifacts, export_harbor

ENGINE = Path(__file__).resolve().parents[1]


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_hash() -> str:
    inventory: dict[str, str] = {}
    for path in sorted((ENGINE / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            inventory[str(path.relative_to(ENGINE))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for rel in ("pyproject.toml", "uv.lock", "scripts/fleetbase_pilot.py"):
        inventory[rel] = hashlib.sha256((ENGINE / rel).read_bytes()).hexdigest()
    return canonical_hash(inventory)


def freeze(root: Path, reference_verified: bool) -> None:
    if not reference_verified:
        raise RuntimeError("Complete clean local and Harbor reference validation before freezing")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(
            "Freeze output must be empty; candidate identities are never overwritten"
        )
    root.mkdir(parents=True, exist_ok=True)
    candidate = source_hash()
    artifacts = build_engine_artifacts(root / "build")
    if source_hash() != candidate:
        raise RuntimeError("Source changed during build; freeze a new experiment")
    folders = export_harbor(
        "fleetbase_dispatch@1", [100001, 100002, 100003], root / "dataset", artifacts
    )
    world_ids = ("fresh-world-1", "fresh-world-2", "fresh-world-3")
    task = get_task("fleetbase_dispatch@1")
    bundle = task.deployment_bundle()
    assert bundle is not None
    spec = PilotSpec(
        experiment_id="fleetbase-native-v2-pilot-" + candidate[:12],
        candidate_sha256=candidate,
        world_ids=world_ids,
        reference_verified=True,
        model_settings={
            "agent": "opencode",
            "version": "1.19.5",
            "model": "google/gemini-3.8-flash",
            "independent_repeats": 3,
            "disable_web_search": True,
            "sampling": "OpenCode/provider defaults, independently initialized conversations",
        },
        rules={
            "frozen_at_utc": datetime.now(UTC).isoformat(),
            "backend_sources": dict(bundle.source_fingerprints),
            "harbor": "0.26.0",
            "task": task.spec.ref,
            "max_steps": task.spec.max_steps,
            "tools": sorted(tool.name for tool in task.tools()),
            "instruction": task.instruction(),
            "continuation": "committed-work-to-horizon",
            "analysis_unit": "world mean across three agent repeats",
            "fresh_worlds": 3,
            "reference_validated_before_freeze": True,
        },
    )
    spec_path = root / "spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n")
    worlds = {}
    for world_id, folder in zip(world_ids, folders, strict=True):
        metadata = tomllib.loads((folder / "task.toml").read_text())["metadata"]
        compose = yaml.safe_load((folder / "environment/docker-compose.yaml").read_text())
        salt = compose["services"]["mcp-server"]["environment"]["SIDECAR_SALT"]
        world = asdict(make_world(metadata["seed"], salt))
        worlds[world_id] = {
            "task": str(folder.resolve()),
            "initial_state_sha256": canonical_hash(world),
        }
    (root / "worlds.json").write_text(json.dumps(worlds, indent=2) + "\n")
    (root / "trials.json").write_text("[]\n")
    print(f"Frozen {spec.experiment_id}: three fresh paired worlds", flush=True)


def run(root: Path, operators: list[str], credential_file: Path | None) -> None:
    spec = PilotSpec.model_validate_json((root / "spec.json").read_text())
    if not spec.experiment_id.startswith("fleetbase-native-v2-pilot-"):
        raise RuntimeError("v1 pilot experiments are archived; freeze a new v2 experiment")
    if source_hash() != spec.candidate_sha256:
        raise RuntimeError(
            "Candidate source changed after freeze; create a new experiment with new worlds"
        )
    if subprocess.check_output(["harbor", "--version"], text=True).strip() != "0.26.0":
        raise RuntimeError("This pilot requires Harbor 0.26.0")
    worlds = json.loads((root / "worlds.json").read_text())
    records = [Trial.model_validate(row) for row in json.loads((root / "trials.json").read_text())]
    model_env = os.environ.copy()
    if credential_file is not None:
        key = dotenv_values(credential_file).get("GOOGLE_GENERATIVE_AI_API_KEY")
        if key:
            model_env["GOOGLE_GENERATIVE_AI_API_KEY"] = key
    if "agent" in operators and not model_env.get("GOOGLE_GENERATIVE_AI_API_KEY"):
        raise RuntimeError("Gemini credential is required for model trials")
    for operator in operators:
        if operator not in OPERATORS:
            raise ValueError(f"Unknown operator: {operator}")
        for world_id in spec.world_ids:
            for repeat in range(3 if operator == "agent" else 1):
                if source_hash() != spec.candidate_sha256:
                    raise RuntimeError("Source changed during pilot; freeze a new experiment")
                prior = [
                    t
                    for t in records
                    if (t.world_id, t.operator, t.repeat) == (world_id, operator, repeat)
                ]
                if prior and max(prior, key=lambda trial: trial.attempt).status == "valid":
                    continue
                attempt = max((t.attempt for t in prior), default=-1) + 1
                name = f"{world_id}-{operator}-{repeat}-attempt-{attempt}"
                prepared = root / "prepared" / name
                shutil.copytree(worlds[world_id]["task"], prepared)
                if operator in {"greedy", "stop_early"}:
                    config = prepared / "task.toml"
                    config.write_text(
                        config.read_text().replace(
                            'CRUCIBLE_POLICY = "reference"', f'CRUCIBLE_POLICY = "{operator}"'
                        )
                    )
                command = [
                    "harbor",
                    "trials",
                    "start",
                    "-p",
                    str(prepared.resolve()),
                    "--trials-dir",
                    str((root / "results").resolve()),
                    "--trial-name",
                    name,
                    "-a",
                    "opencode"
                    if operator == "agent"
                    else "nop"
                    if operator == "do_nothing"
                    else "oracle",
                ]
                if operator == "agent":
                    command += [
                        "-m",
                        "google/gemini-3.8-flash",
                        "--agent-kwarg",
                        "version=1.19.5",
                        "--agent-kwarg",
                        "disable_web_search=true",
                        "--api-key-name",
                        "GOOGLE_GENERATIVE_AI_API_KEY",
                    ]
                print(f"{name}: starting", flush=True)
                log = root / f"{name}.log"
                error = None
                outcome: dict[str, JsonValue] = {}
                metrics: dict[str, float] = {}
                evidence: dict[str, JsonValue] = {}
                result_dir = root / "results" / name
                try:
                    with log.open("w") as output:
                        result = subprocess.run(
                            command,
                            env=model_env,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                    if result.returncode:
                        raise RuntimeError("Harbor process failed")
                    harbor_result = json.loads((result_dir / "result.json").read_text())
                    if harbor_result.get("exception_info"):
                        raise RuntimeError("Harbor trial infrastructure exception")
                    verifier = result_dir / "verifier"
                    if not (verifier / "transcript.json").is_file():
                        raise RuntimeError("Missing trial transcript")
                    outcome = json.loads((verifier / "grade.json").read_text())
                    evidence = json.loads((verifier / "evidence.json").read_text())
                    flags = outcome.get("flags")
                    if outcome.get("cleanup_errors"):
                        raise RuntimeError("Episode resource cleanup failed")
                    if isinstance(flags, dict) and flags.get("env_error"):
                        raise RuntimeError("Environment failed during trial")
                    if (
                        canonical_hash(evidence.get("world"))
                        != worlds[world_id]["initial_state_sha256"]
                    ):
                        raise RuntimeError("Frozen world does not match trial physical evidence")
                    breakdown = outcome.get("breakdown")
                    if not isinstance(breakdown, dict):
                        raise RuntimeError("Missing economic breakdown")
                    metrics = {
                        key: float(value)
                        for key, value in breakdown.items()
                        if isinstance(value, (int, float))
                    }
                    if "utility" not in metrics:
                        raise RuntimeError("Missing raw utility")
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                trial = Trial(
                    experiment_id=spec.experiment_id,
                    candidate_sha256=spec.candidate_sha256,
                    world_id=world_id,
                    initial_state_sha256=worlds[world_id]["initial_state_sha256"],
                    operator=operator,
                    repeat=repeat,
                    attempt=attempt,
                    status="infra_error" if error else "valid",
                    reward=float(str(outcome["reward"])) if error is None else None,
                    utility=metrics.get("utility") if error is None else None,
                    metrics=metrics,
                    transcript=str(result_dir / "verifier/transcript.json"),
                    error=error,
                )
                records.append(trial)
                (root / "trials.json").write_text(
                    json.dumps([t.model_dump(mode="json") for t in records], indent=2) + "\n"
                )
                (root / "findings.json").write_text(
                    json.dumps(analyze(spec, records), indent=2) + "\n"
                )
                print(
                    f"{name}: {trial.status}"
                    + (
                        f" reward={trial.reward:.6f}" if trial.reward is not None else f" ({error})"
                    ),
                    flush=True,
                )
                if error:
                    raise RuntimeError(
                        "Pilot stopped at an infrastructure failure; "
                        "retain attempt and fix before resuming"
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reference-verified", action="store_true")
    parser.add_argument("--operators", default=",".join(OPERATORS))
    parser.add_argument("--credential-file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "freeze":
        freeze(args.out.resolve(), args.reference_verified)
    else:
        run(args.out.resolve(), args.operators.split(","), args.credential_file)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

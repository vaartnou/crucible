"""Compare completed Harbor model trials with rules measured on their original export."""

import argparse
import asyncio
import hashlib
import json
import math
import zipfile
from pathlib import Path
from statistics import mean

import yaml

from crucible.core.registry import get_task
from crucible.feedback.regrade import regrade
from crucible.feedback.version import environment_hash


def exported_environment_hash(export: Path) -> str:
    """Hash the shipped simulator files with the same algorithm as environment_hash."""
    manifest = json.loads((export / "manifest.json").read_text())
    if manifest["task_ref"] != "oncall@2":
        raise ValueError("Expected an oncall@2 export")
    wheel = export / manifest["tasks"][0] / "environment/mcp-server" / manifest["wheel"]
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != manifest["wheel_sha256"]:
        raise ValueError("Exported wheel does not match its manifest")
    prefix = "crucible/envs/oncall/"
    digest = hashlib.sha256()
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith(prefix):
                continue
            relative = Path(name.removeprefix(prefix))
            if relative.suffix not in (".py", ".yaml") or {"tests", "policies"} & set(
                relative.parts
            ):
                continue
            digest.update(str(relative).encode())
            digest.update(archive.read(name))
    return digest.hexdigest()


async def compare(bots: Path, jobs: list[Path], export: Path, policy: str, out: Path) -> None:
    bot_data = json.loads(await asyncio.to_thread(bots.read_text))
    baseline = {row["seed"]: row for row in bot_data["rows"] if row["policy"] == policy}
    if any(row["task"] != "oncall@2" for row in baseline.values()):
        raise ValueError("Expected oncall@2 rule measurements")
    expected_hash = exported_environment_hash(export)
    if not baseline or any(row["environment_sha256"] != expected_hash for row in baseline.values()):
        raise ValueError("Rule measurements and exported simulator source hashes differ")
    if environment_hash(get_task("oncall@2")) != expected_hash:
        raise ValueError("Current simulator source differs from the exported source")
    composes = await asyncio.to_thread(
        lambda: list(export.glob("*/environment/docker-compose.yaml"))
    )
    world_hashes = {}
    for compose in composes:
        env = yaml.safe_load(compose.read_text())["services"]["mcp-server"]["environment"]
        world_hashes[int(env["SIDECAR_SEED"])] = hashlib.sha256(
            str(env["SIDECAR_SALT"]).encode()
        ).hexdigest()
    if any(world_hashes.get(seed) != row["salt_sha256"] for seed, row in baseline.items()):
        raise ValueError("Rule measurements and exported world salts differ")
    paired, excluded = [], []
    agent_identities = set()
    for job in jobs:
        results = await asyncio.to_thread(lambda job=job: sorted(job.glob("*/result.json")))
        for result_path in results:
            result = json.loads(result_path.read_text())
            exception = result.get("exception_info")
            if exception:
                excluded.append(
                    {"trial": str(result_path.parent), "error": exception["exception_type"]}
                )
                continue
            info = result["agent_info"]
            agent_identities.add((info["name"], info["version"], info["model_info"]["name"]))
            transcript = result_path.parent / "verifier/transcript.json"
            records = json.loads(transcript.read_text())
            header, footer = records[0], records[-1]
            seed = header["seed"]
            if header["task"] != "oncall@2" or seed not in baseline:
                raise ValueError("Model trial has no matching rule measurement")
            if any(row["seed"] == seed for row in paired):
                raise ValueError(
                    "Duplicate model trial for a seed; select a single attempt explicitly"
                )
            replay = await regrade(transcript, export=export, numeric_tolerance=1e-12)
            score = footer["score"]
            if not replay["comparable"] or not math.isclose(
                replay["reward"], score["total"], rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValueError("Model transcript does not replay on the exported simulator")
            paired.append(
                {
                    "seed": seed,
                    "model_reward": score["total"],
                    "workflow_reward": baseline[seed]["score"]["total"],
                    "gap": baseline[seed]["score"]["total"] - score["total"],
                    "model_solved": bool(
                        score["flags"].get("verified") and score["flags"].get("diagnosis_correct")
                    ),
                    "model_calls": footer["steps"],
                    "workflow_calls": baseline[seed]["steps"],
                    "transcript": str(transcript),
                    "replay": replay,
                }
            )
    if not paired:
        raise ValueError("No valid completed model trials")
    if len(agent_identities) != 1:
        raise ValueError("Model trials mix different models or agent versions")
    paired.sort(key=lambda row: row["seed"])
    gap = mean(row["gap"] for row in paired)
    large_gaps = sum(row["gap"] >= 0.20 for row in paired)
    report = {
        "task": "oncall@2",
        "agent": list(next(iter(agent_identities))),
        "environment_sha256": expected_hash,
        "workflow": policy,
        "n": len(paired),
        "expected_worlds": len(baseline),
        "missing_seeds": sorted(set(baseline) - {row["seed"] for row in paired}),
        "model_mean": mean(row["model_reward"] for row in paired),
        "workflow_mean": mean(row["workflow_reward"] for row in paired),
        "mean_gap": gap,
        "seeds_gap_at_least_020": large_gaps,
        "historical_difficulty_gate_passed": len(paired) >= 10 and gap >= 0.20 and large_gaps >= 7,
        "excluded": excluded,
        "rows": paired,
    }
    await asyncio.to_thread(out.write_text, json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("rows", "excluded")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, nargs="+", required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--policy", default="incident_runbook")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(compare(args.bots, args.jobs, args.export, args.policy, args.out))

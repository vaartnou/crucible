#!/usr/bin/env python3
"""Prepare/run a scripted native replay outside the frozen Fleetbase pilot.

v1-ONLY: the semantic observation comparison and replayed prefixes assume the v1 tool
set and observation shape of the archived v1 pilot. It refuses to run until it is
ported to the v2 tools (``fleetbase_dispatch@1`` after plan step 5).

No model is called. Preparation copies a frozen task and replaces only its oracle
solution with a public MCP client: replay the original public prefix, verify the
semantic observations, then use the reference for the remaining 128-call budget.
Run refuses to start until all nine approved model trials and twelve scripted
baselines have valid results. Never modifies the pilot dataset or trial records.
"""

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from crucible.envs.fleetbase_dispatch.sim import WorldSimulator
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World


# These helpers are also copied into the private oracle client. They compare the
# whole public observation, stripping only incidental native identifiers, episode
# labels and wall-clock record timestamps. Business fields and commitments stay.
def semantic_state(observation):
    identifiers = {}
    orders = observation.get("orders", {})
    for request_id, order in orders.items():
        identifiers[order["id"]] = "order:" + request_id
        for key, kind in (("driver_assigned", "driver"), ("vehicle_assigned", "vehicle")):
            resource = order.get(key)
            if not isinstance(resource, dict):
                continue
            match = re.search(r"-(" + kind + r"-\d+)$", resource.get("internal_id", ""))
            if match is None:
                raise RuntimeError("Native resource has no stable episode resource label")
            label = match.group(1)
            identifiers[resource["id"]] = label
            if kind == "driver":
                if resource.get("user"):
                    identifiers[resource["user"]] = "user:" + label
                if resource.get("company"):
                    identifiers[resource["company"]] = "company:benchmark"
        payload = order.get("payload")
        if isinstance(payload, dict):
            if payload.get("id"):
                identifiers[payload["id"]] = "payload:" + request_id
            for role in ("pickup", "dropoff", "return"):
                place = payload.get(role)
                if isinstance(place, dict) and place.get("id"):
                    identifiers[place["id"]] = "place:" + request_id + ":" + role

    def normalize(value):
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key not in {"created_at", "updated_at", "dispatched_at", "started_at"}
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            if value in identifiers:
                return identifiers[value]
            normalized = re.sub(r"crucible-[0-9a-f]{32}-", "crucible-EPISODE-", value, flags=re.I)
            if re.search(
                r"\b(?:order|driver|vehicle|user|company|payload|place)_[a-z0-9]+\b", normalized
            ):
                raise RuntimeError("Unmapped incidental native identifier in public observation")
            return normalized
        return value

    return normalize(observation)


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


CLIENT = """
import asyncio
import hashlib
import json
import re
from pathlib import Path
from crucible.agents.remote import McpEpisode
from crucible.core.registry import get_task
from crucible.core.tools import Observation, ToolCall
from crucible.envs.fleetbase_dispatch.policies import ReferenceOperator

{helpers}

async def main():
    prefix = json.loads(Path(__file__).with_name("prefix.json").read_text())
    task = get_task("fleetbase_dispatch@1")
    remote, instruction = await McpEpisode.connect("http://mcp-server:8000/mcp")
    calls = 0
    observation = Observation()
    try:
        for expected in prefix:
            result = await remote.step(ToolCall.model_validate(expected["call"]))
            calls += 1
            if not result.ok or not isinstance(result.content, dict):
                raise RuntimeError("Original public prefix did not execute successfully")
            actual = semantic_state(result.content)
            original = semantic_state(expected["result"]["content"])
            if actual != original:
                raise RuntimeError(f"Semantic public prefix diverged at call {{calls}}")
            observation = Observation(last_result=result)
        print("COUNTERFACTUAL_BRANCH " + json.dumps({{
            "public_prefix_equal": True,
            "public_state_sha256": canonical_hash(actual),
            "prefix_calls": calls,
            "branch_time": result.content["now"],
        }}), flush=True)
        policy = ReferenceOperator()
        await policy.reset(task.spec, task.tools(), instruction)
        for _ in range(task.spec.max_steps - calls):
            call = await policy.act(observation)
            if call is None:
                break
            result = await remote.step(call)
            calls += 1
            if not result.ok:
                raise RuntimeError("Scripted alternative was rejected by native application")
            observation = Observation(last_result=result)
            if call.name == "finish":
                break
        print("COUNTERFACTUAL_COMPLETE " + json.dumps({{
            "calls": calls,
            "public_budget": task.spec.max_steps,
            "continuation": "reference-from-identical-public-branch-state",
        }}), flush=True)
    finally:
        await remote.close()

asyncio.run(main())
"""


def _world(value):
    return World(
        tuple(
            Request(**{**r, "pickup": tuple(r["pickup"]), "dropoff": tuple(r["dropoff"])})
            for r in value["requests"]
        ),
        tuple(Vehicle(**{**v, "position": tuple(v["position"])}) for v in value["vehicles"]),
        value["horizon"],
        value["speed"],
        value["cost_per_distance"],
    )


def _physical_prefix(world, prefix):
    """Reconstruct original prefix events from its public calls and commitments.

    The event prefix must also match the original native trial's trusted final
    evidence, and the final replay evidence must match that same prefix.
    """
    sim = WorldSimulator(_world(world))
    committed = {}
    for step in prefix:
        call = step["call"]
        sim.reconcile(committed)
        if call["name"] == "advance":
            sim.advance(call["args"]["minutes"])
        elif call["name"] == "finish":
            sim.finish()
        public = step["result"]["content"]
        committed = {
            request["id"]: {
                "vehicle_id": request.get("vehicle_id"),
                "dispatched": request.get("dispatched") is True,
            }
            for request in public["requests"]
            if request.get("native_id")
        }
        sim.reconcile(committed)
    if sim.now != prefix[-1]["result"]["content"]["now"]:
        raise RuntimeError("Original physical prefix reconstruction diverged")
    return [event.to_dict() for event in sim.events]


def prepare(args):
    pilot = args.pilot.resolve()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("Replay output must be empty; existing evidence is never overwritten")
    spec = json.loads((pilot / "spec.json").read_text())
    rows = json.loads((pilot / "trials.json").read_text())
    candidates = [
        t
        for t in rows
        if t["world_id"] == args.world
        and t["operator"] == "agent"
        and t["repeat"] == args.repeat
        and t["status"] == "valid"
    ]
    if not candidates:
        raise RuntimeError("A valid original model trial is required")
    original = max(candidates, key=lambda t: t["attempt"])
    transcript = Path(original["transcript"])
    records = json.loads(transcript.read_text())
    steps = [r for r in records if r["type"] == "step"]
    if not 0 < args.prefix_calls < len(steps):
        raise ValueError("prefix calls must leave at least one original call to replace")
    prefix = steps[: args.prefix_calls]
    if any(not step["result"]["ok"] for step in prefix):
        raise RuntimeError("This harness requires an originally successful public prefix")
    evidence = json.loads(transcript.with_name("evidence.json").read_text())
    world = evidence["world"]
    if canonical_hash(world) != original["initial_state_sha256"]:
        raise RuntimeError("Original physical world does not match frozen fingerprint")
    events = _physical_prefix(world, prefix)
    if evidence["events"][: len(events)] != events:
        raise RuntimeError("Reconstructed events do not match original trusted event prefix")
    public_hash = canonical_hash(semantic_state(prefix[-1]["result"]["content"]))
    worlds = json.loads((pilot / "worlds.json").read_text())
    source = Path(worlds[args.world]["task"])
    out.mkdir(parents=True, exist_ok=True)
    task = out / "task"
    shutil.copytree(source, task)
    solution = task / "solution"
    helpers = "\n\n".join(inspect.getsource(fn) for fn in (semantic_state, canonical_hash))
    (solution / "counterfactual_client.py").write_text(CLIENT.format(helpers=helpers))
    (solution / "prefix.json").write_text(json.dumps(prefix) + "\n")
    solve = solution / "solve.sh"
    text = solve.read_text()
    marker = "/tmp/crucible-solution/bin/crucible play --mcp"
    if marker not in text:
        raise RuntimeError("Frozen oracle bootstrap format changed")
    solve.write_text(
        text[: text.index(marker)]
        + '/tmp/crucible-solution/bin/python "$SOLUTION_DIR/counterfactual_client.py"\n'
    )
    metadata = {
        "purpose": "posthoc-scripted-causal-replay-not-an-independent-world",
        "experiment_id": spec["experiment_id"],
        "candidate_sha256": spec["candidate_sha256"],
        "world_id": args.world,
        "repeat": args.repeat,
        "prefix_calls": args.prefix_calls,
        "branch_time": prefix[-1]["result"]["content"]["now"],
        "branch_public_state_sha256": public_hash,
        "physical_world_sha256": original["initial_state_sha256"],
        "prefix_events": events,
        "original": original,
        "pilot": str(pilot),
        "status": "prepared-not-run",
    }
    (out / "replay.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Prepared {len(prefix)}-call public prefix at virtual time {metadata['branch_time']}")
    print(f"Original event prefix and semantic public-state hash verified; task copied to {task}")


def run(args):
    out = args.out.resolve()
    metadata = json.loads((out / "replay.json").read_text())
    pilot = Path(metadata["pilot"])
    rows = json.loads((pilot / "trials.json").read_text())
    latest = {}
    for row in rows:
        key = (row["world_id"], row["operator"], row["repeat"])
        if key not in latest or row["attempt"] > latest[key]["attempt"]:
            latest[key] = row
    if len(latest) != 21 or any(t["status"] != "valid" for t in latest.values()):
        raise RuntimeError(
            "Finish all 21 valid pilot episodes before starting this scripted replay"
        )
    if sum(t["operator"] == "agent" for t in latest.values()) != 9:
        raise RuntimeError("The nine approved model trials are not complete")
    if metadata["status"] != "prepared-not-run":
        raise RuntimeError(
            "Replay has already been attempted; preserve evidence and prepare a new one"
        )
    if subprocess.check_output(["harbor", "--version"], text=True).strip() != "0.26.0":
        raise RuntimeError("Replay requires pinned Harbor 0.26.0")
    name = f"{metadata['world_id']}-agent-{metadata['repeat']}-prefix-{metadata['prefix_calls']}"
    result_dir = out / "results" / name
    command = [
        "harbor",
        "trials",
        "start",
        "-p",
        str(out / "task"),
        "--trials-dir",
        str(out / "results"),
        "--trial-name",
        name,
        "-a",
        "oracle",
    ]
    # The oracle needs no model credentials; do not expose inherited model keys.
    environment = {key: value for key, value in os.environ.items() if "GOOGLE" not in key}
    metadata["status"] = "running"
    (out / "replay.json").write_text(json.dumps(metadata, indent=2) + "\n")
    try:
        with (out / "harbor.log").open("w") as output:
            subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=environment,
                check=True,
                timeout=2400,
            )
        result = json.loads((result_dir / "result.json").read_text())
        if result.get("exception_info"):
            raise RuntimeError("Scripted replay had an infrastructure exception")
        exit_code = result_dir / "agent" / "exit-code.txt"
        if exit_code.exists() and exit_code.read_text().strip() != "0":
            raise RuntimeError("Scripted replay's public prefix or alternative failed")
        oracle = (result_dir / "agent" / "oracle.txt").read_text()
        branch = None
        completed = None
        for line in oracle.splitlines():
            if line.startswith("COUNTERFACTUAL_BRANCH "):
                branch = json.loads(line.removeprefix("COUNTERFACTUAL_BRANCH "))
            if line.startswith("COUNTERFACTUAL_COMPLETE "):
                completed = json.loads(line.removeprefix("COUNTERFACTUAL_COMPLETE "))
        if branch is None or completed is None:
            raise RuntimeError("Missing semantic branch or public-budget verification")
        if (
            not branch["public_prefix_equal"]
            or branch["public_state_sha256"] != metadata["branch_public_state_sha256"]
            or completed["calls"] > completed["public_budget"]
        ):
            raise RuntimeError("Scripted replay did not preserve its legal public branch")
        verifier = result_dir / "verifier"
        grade = json.loads((verifier / "grade.json").read_text())
        evidence = json.loads((verifier / "evidence.json").read_text())
        if grade["flags"].get("env_error") or grade.get("cleanup_errors"):
            raise RuntimeError("Scripted replay grading or cleanup failed")
        if canonical_hash(evidence["world"]) != metadata["physical_world_sha256"]:
            raise RuntimeError("Scripted replay changed the frozen physical world")
        events = metadata["prefix_events"]
        if evidence["events"][: len(events)] != events:
            raise RuntimeError("Scripted replay changed the original verified event prefix")
        original = metadata["original"]
        metrics = grade["breakdown"]
        metadata.update(
            {
                "status": "valid-scripted-causal-replay",
                "branch_verification": branch,
                "calls": completed["calls"],
                "reward": grade["reward"],
                "metrics": metrics,
                "utility_gain": metrics["utility"] - original["utility"],
                "reward_gain": grade["reward"] - original["reward"],
                "distance_saved": original["metrics"]["distance"] - metrics["distance"],
                "events_prefix_equal": True,
                "world_equal": True,
                "transcript": str(verifier / "transcript.json"),
            }
        )
    except Exception as exc:
        metadata["status"] = "failed-scripted-replay"
        metadata["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        (out / "replay.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: metadata[key]
                for key in (
                    "status",
                    "calls",
                    "reward",
                    "utility_gain",
                    "distance_saved",
                    "world_equal",
                    "events_prefix_equal",
                )
            },
            indent=2,
        )
    )


V1_ONLY = (
    "fleetbase_counterfactual.py is v1-only: it replays v1 tool prefixes and compares the "
    "v1 observation shape. Port it to the v2 tools before use."
)


def main():
    raise SystemExit(V1_ONLY)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--pilot", type=Path, default=Path("../runs/fleetbase-pilot-v1"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--world", default="fresh-world-1")
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--prefix-calls", type=int, default=27)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "prepare":
        prepare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()

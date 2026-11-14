#!/usr/bin/env python3
"""Difficulty sweep of `fleetbase_sim@1`: the policy ladder and exploits over a WorldConfig grid.

Every (config, seed) runs all policies on the in-memory backend, in parallel worker
processes (one hindsight solve per world and process). For each config it prints the mean
and SD of the reward per policy, the gaps, the best exploit, the reference's budget use, how
often the reference beats hindsight, and PASS/FAIL against the gates:

    do_nothing = 0; greedy 0.35-0.55; reference 0.75-0.85; reference - greedy >= 0.25;
    greedy - greedy_no_replan >= 0.05 (only checked when disruptions are on);
    every exploit < greedy (mean).

Knobs (each `--grid KNOB=v1,v2,...` adds a grid axis; `--set FIELD=value` sets any
`WorldConfig` field for every config):

    ratio       demand_capacity_ratio
    tightness   multiplies deadline_slack_min and deadline_slack_max
    disruption  multiplies breakdown, cancellation, dropoff change and traffic rates
    silent      silent_share

Example (from the repository root):

    uv run --directory engine python scripts/fleetbase_sweep.py --seeds 50 \\
        --grid ratio=1.2,1.4 --grid disruption=0,1 --json ../runs/sweep.json
"""

import argparse
import asyncio
import itertools
import json
import math
import statistics
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, ClassVar

from crucible.core.registry import get_policy
from crucible.core.tools import Observation
from crucible.envs.fleetbase_dispatch.policies import EXPLOITS, LADDER
from crucible.envs.fleetbase_dispatch.task import FleetbaseSim
from crucible.envs.fleetbase_dispatch.world import WorldConfig

POLICIES = (*LADDER, *EXPLOITS)
KNOBS = ("ratio", "tightness", "disruption", "silent")
GATES = {
    "greedy": (0.35, 0.55),
    "reference": (0.75, 0.85),
    "gap": 0.25,
    "replan_gap": 0.05,
}


def make_config(base: WorldConfig, knobs: dict[str, float]) -> WorldConfig:
    config = base
    if "ratio" in knobs:
        config = replace(config, demand_capacity_ratio=knobs["ratio"])
    if "tightness" in knobs:
        t = knobs["tightness"]
        config = replace(
            config,
            deadline_slack_min=base.deadline_slack_min * t,
            deadline_slack_max=base.deadline_slack_max * t,
        )
    if "disruption" in knobs:
        d = knobs["disruption"]
        config = replace(
            config,
            breakdown_rate=min(1.0, base.breakdown_rate * d),
            cancellation_rate=min(1.0, base.cancellation_rate * d),
            dropoff_change_rate=min(1.0, base.dropoff_change_rate * d),
            traffic_rate=min(1.0, base.traffic_rate * d),
        )
    if "silent" in knobs:
        config = replace(config, silent_share=knobs["silent"])
    return config


def task_class(config: WorldConfig, max_steps: int | None) -> type[FleetbaseSim]:
    spec = FleetbaseSim.spec
    if max_steps is not None:
        spec = spec.model_copy(update={"max_steps": max_steps})

    class SweepSim(FleetbaseSim):
        world_config: ClassVar[WorldConfig] = config

    SweepSim.spec = spec
    return SweepSim


@dataclass(frozen=True)
class Unit:
    index: int
    config: WorldConfig
    seed: int
    policies: tuple[str, ...]
    max_steps: int | None


async def _episode(cls: type[FleetbaseSim], policy_name: str, seed: int) -> dict[str, Any]:
    task = cls()
    policy = get_policy(policy_name, "fleetbase_sim@1")
    error: str | None = None
    calls = 0
    try:
        observation = await task.reset(seed)
        await policy.reset(task.spec, task.tools(), task.instruction())
        for _ in range(task.spec.max_steps):
            if task.done():
                break
            call = await policy.act(observation)
            if call is None:
                break
            result = await task.step(call)
            calls += 1
            observation = Observation(last_result=result)
        score = await task.grade()
        reward, breakdown = score.total, dict(score.breakdown)
    except Exception as exc:  # reported as a failure, never hidden
        error = f"{type(exc).__name__}: {exc}"
        reward, breakdown = 0.0, {}
    finally:
        await task.aclose()
    return {
        "reward": reward,
        "utility": breakdown.get("utility", 0.0),
        "u_hindsight": breakdown.get("u_hindsight", 0.0),
        "calls": calls,
        "error": error,
    }


def run_unit(unit: Unit) -> tuple[int, int, dict[str, dict[str, Any]]]:
    cls = task_class(unit.config, unit.max_steps)

    async def run_all() -> dict[str, dict[str, Any]]:
        return {name: await _episode(cls, name, unit.seed) for name in unit.policies}

    return unit.index, unit.seed, asyncio.run(run_all())


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else math.nan


def _sd(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def summarize(
    config: WorldConfig, knobs: dict[str, float], runs: dict[int, dict[str, dict[str, Any]]]
) -> dict[str, Any]:
    seeds = sorted(runs)
    policies = list(next(iter(runs.values())))
    rewards = {p: [runs[s][p]["reward"] for s in seeds] for p in policies}
    stats = {p: {"mean": _mean(v), "sd": _sd(v)} for p, v in rewards.items()}
    mean = {p: stats[p]["mean"] for p in policies}
    greedy = mean.get("greedy_nearest", math.nan)
    reference = mean.get("reference", math.nan)
    exploits = {p: mean[p] for p in policies if p in EXPLOITS}
    errors = [(s, p, runs[s][p]["error"]) for s in seeds for p in policies if runs[s][p]["error"]]
    ref_calls = [runs[s]["reference"]["calls"] for s in seeds] if "reference" in mean else []
    # Reward 1.0 means the online reference reached or beat the hindsight utility.
    above = [s for s in seeds if "reference" in mean and runs[s]["reference"]["reward"] >= 1.0]
    ref_max = max((runs[s]["reference"]["reward"] for s in seeds), default=math.nan)
    disruptions = config.breakdown_rate + config.cancellation_rate + config.dropoff_change_rate
    gates: dict[str, bool | None] = {
        "do_nothing = 0": (mean["do_nothing"] == 0) if "do_nothing" in mean else None,
        "greedy in [0.35, 0.55]": GATES["greedy"][0] <= greedy <= GATES["greedy"][1],
        "reference in [0.75, 0.85]": GATES["reference"][0] <= reference <= GATES["reference"][1],
        "reference - greedy >= 0.25": reference - greedy >= GATES["gap"],
        "greedy - no_replan >= 0.05": (
            greedy - mean["greedy_no_replan"] >= GATES["replan_gap"]
            if "greedy_no_replan" in mean and disruptions > 0
            else None
        ),
        "exploits < greedy": all(v < greedy for v in exploits.values()) if exploits else None,
        "no errors": not errors,
    }
    return {
        "knobs": knobs,
        "config": asdict(config),
        "seeds": seeds,
        "stats": stats,
        "gap": reference - greedy,
        "replan_gap": greedy - mean.get("greedy_no_replan", math.nan),
        "max_exploit": max(exploits.items(), key=lambda kv: kv[1]) if exploits else None,
        "reference_calls": {"mean": _mean(ref_calls), "max": max(ref_calls, default=0)},
        "reference_at_or_above_hindsight": above,
        "reference_max": ref_max if "reference" in mean else None,
        "errors": errors,
        "gates": gates,
        "pass": all(v is not False for v in gates.values()),
    }


def print_summary(summary: dict[str, Any], max_steps: int) -> None:
    knobs = ", ".join(f"{k}={v:g}" for k, v in summary["knobs"].items()) or "defaults"
    print(f"\n== {knobs}  ({len(summary['seeds'])} seeds)")
    print(f"{'policy':<24}{'mean':>8}{'sd':>8}")
    for name, stat in summary["stats"].items():
        print(f"{name:<24}{stat['mean']:>8.3f}{stat['sd']:>8.3f}")
    calls = summary["reference_calls"]
    print(f"reference - greedy        {summary['gap']:+.3f}")
    print(f"greedy - greedy_no_replan {summary['replan_gap']:+.3f}")
    if summary["max_exploit"] is not None:
        name, value = summary["max_exploit"]
        print(f"max exploit               {name} {value:.3f}")
    print(
        f"reference calls           mean {calls['mean']:.1f} max {calls['max']} "
        f"of {max_steps} ({calls['mean'] / max_steps:.0%})"
    )
    if summary["reference_max"] is not None:
        print(
            f"reference >= hindsight    {len(summary['reference_at_or_above_hindsight'])} "
            f"seeds (max reward {summary['reference_max']:.3f})"
        )
    for gate, ok in summary["gates"].items():
        status = "n/a " if ok is None else ("PASS" if ok else "FAIL")
        print(f"  [{status}] {gate}")
    for seed, policy, error in summary["errors"][:10]:
        print(f"  error seed {seed} {policy}: {error}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, default=50, help="number of dev seeds")
    parser.add_argument("--start", type=int, default=0, help="first seed")
    parser.add_argument("--workers", type=int, default=None, help="processes (default: CPUs)")
    parser.add_argument("--policies", default=",".join(POLICIES), help="comma-separated")
    parser.add_argument("--grid", action="append", default=[], help="KNOB=v1,v2,...")
    parser.add_argument("--set", action="append", default=[], help="WorldConfig FIELD=value")
    parser.add_argument("--max-steps", type=int, default=None, help="override the budget")
    parser.add_argument("--json", default=None, help="write the summaries to this file")
    return parser.parse_args(argv)


def base_config(assignments: Sequence[str]) -> WorldConfig:
    types = {f.name: f.type for f in fields(WorldConfig)}
    values: dict[str, Any] = {}
    for item in assignments:
        name, _, raw = item.partition("=")
        if name not in types:
            raise SystemExit(f"Unknown WorldConfig field {name!r}")
        kind = types[name]
        values[name] = int(raw) if kind in (int, "int") else float(raw)
    return replace(WorldConfig(), **values)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    base = base_config(args.set)
    axes: list[tuple[str, list[float]]] = []
    for item in args.grid:
        name, _, raw = item.partition("=")
        if name not in KNOBS:
            raise SystemExit(f"Unknown knob {name!r}; choose from {', '.join(KNOBS)}")
        axes.append((name, [float(v) for v in raw.split(",") if v]))
    grid = [
        dict(zip([n for n, _ in axes], combo, strict=True))
        for combo in itertools.product(*[values for _, values in axes])
    ] or [{}]
    configs = [make_config(base, knobs) for knobs in grid]
    policies = tuple(p for p in args.policies.split(",") if p)
    seeds = range(args.start, args.start + args.seeds)
    units = [
        Unit(i, config, seed, policies, args.max_steps)
        for i, config in enumerate(configs)
        for seed in seeds
    ]
    max_steps = args.max_steps or FleetbaseSim.spec.max_steps
    started = time.time()
    runs: dict[int, dict[int, dict[str, dict[str, Any]]]] = {i: {} for i in range(len(configs))}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, seed, result in pool.map(run_unit, units, chunksize=1):
            runs[index][seed] = result
    summaries = [
        summarize(c, k, runs[i]) for i, (c, k) in enumerate(zip(configs, grid, strict=True))
    ]
    for summary in summaries:
        print_summary(summary, max_steps)
    print(f"\n{len(units)} worlds in {time.time() - started:.0f} s")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(
                {"max_steps": max_steps, "policies": policies, "summaries": summaries},
                handle,
                indent=2,
                default=str,
            )
    return 0 if all(s["pass"] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())

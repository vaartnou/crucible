"""Post-run Cala hindsight search over legal weekly controls and a certified surplus bound."""

import argparse
import asyncio
import copy
import heapq
import json
import math
import tomllib
from pathlib import Path
from typing import Any

import yaml
from score_cala_paired import BASELINES, TASK, replay, run_policy

from crucible.core.registry import get_task
from crucible.core.scoring import Score
from crucible.core.tools import ToolCall
from crucible.envs.supplygym.outcomes import OutcomeControls, grade_outcomes
from crucible.envs.supplygym.play import CALA_REGIME, Play
from crucible.envs.supplygym.world import EXPEDITE_COST, HOLD_COST, PRICE_SELL, STOCKOUT_PEN

type Controls = dict[str, Any]
type Plan = list[Controls]
LOCAL_POLICIES = ("reference", *BASELINES)


def surplus_cents(grade: Score) -> int:
    return round(100 * grade.breakdown["operating_surplus"])


def normalized_score(surplus: int, benchmark: int) -> float:
    """Score a completed year against the verified benchmark, in cents."""
    scale = max(1, abs(benchmark))
    return max(0.0, 1.0 - (benchmark - surplus) / scale)


def surplus_upper_bound(play: Play) -> int:
    """Bound surplus in cents by relaxing the weekly action and shipment capacity limits."""
    env: Any = play.env
    penalty = round(100 * STOCKOUT_PEN)
    price = round(100 * PRICE_SELL)
    holding = round(100 * HOLD_COST)
    expedite = round(100 * EXPEDITE_COST)
    # Each unit of demand can be lost, or bought at the cheapest available arrival
    # price plus holding. Allow unlimited purchases through any route that can ship
    # at least one unit. Also allow immediate onboarding and unlimited expediting.
    cost = [price + penalty] * env.horizon
    for day in range(env.horizon):
        for supplier in range(env.NS):
            health = float(env.tape.H[day, supplier])
            if math.floor(env.cap[supplier] * health) < 1:
                continue
            lead = int(env.lead[supplier])
            delay = math.ceil((1 - health) * lead * 0.5) if health < 0.8 else 0
            _, regular = env.regular_receipt(supplier, day + lead, delay)
            express = day + (lead + 1) // 2 + delay // 2
            unit_price = round(100 * float(env.price[supplier]))
            if regular < env.horizon:
                cost[regular] = min(cost[regular], unit_price)
            if express < env.horizon:
                cost[express] = min(cost[express], unit_price + expedite)
    for day in range(1, env.horizon):
        cost[day] = min(cost[day], cost[day - 1] + holding)

    demand_by_day = [int(demand) for demand in env.tape.demand[: env.horizon]]
    prepaid = [0] * env.horizon
    prepaid[0] = int(env.onhand)
    for order in env.orders:
        if order["arr"] < env.horizon:
            prepaid[order["arr"]] += order["qty"]

    # Match each prepaid unit to a distinct future demand unit. A release on
    # day a can save cost[t] - holding * (t - a) on day t >= a. For any two
    # arrivals, swapping their assigned eligible days preserves total savings.
    # Assigning later arrivals first preserves all earlier arrivals' options.
    opportunities: list[tuple[int, int]] = []
    opening_credit = 0
    for day in reversed(range(env.horizon)):
        if demand_by_day[day]:
            heapq.heappush(opportunities, (holding * day - cost[day], demand_by_day[day]))
        available = prepaid[day]
        while available and opportunities and holding * day > opportunities[0][0]:
            priority, demand = heapq.heappop(opportunities)
            units = min(available, demand)
            opening_credit += units * (holding * day - priority)
            available -= units
            if units < demand:
                heapq.heappush(opportunities, (priority, demand - units))
    return (
        sum(
            demand * (price - unit_cost)
            for demand, unit_cost in zip(demand_by_day, cost, strict=True)
        )
        + opening_credit
    )


def advance(play: Play, plan: Plan) -> bool:
    """Replay candidate weekly controls using the normal purchase and accounting rules."""
    for args in plan:
        if play.done or play.refusal(args) is not None:
            return False
        play.apply(args)
    return True


def checkpoints(initial: Play, plan: Plan) -> list[Play]:
    states = [initial]
    for args in plan:
        state = copy.copy(states[-1])
        env = states[-1].env
        state.env = copy.deepcopy(env, {id(env.world): env.world, id(env.tape): env.tape})
        state.log = states[-1].log.copy()
        if not advance(state, [args]):
            raise ValueError("Invalid controls in the verified starting plan")
        states.append(state)
    if not states[-1].done:
        raise ValueError("Starting plan must complete the Cala year")
    return states


def proposals(state: Play, args: Controls) -> list[Controls]:
    env: Any = state.env
    end = min(env.day + env.k, env.horizon)
    health = env.tape.H[env.day : end, : env.NS].mean(axis=0)
    weights = {
        node["id"]: max(0.1, float(health[index]) ** 2)
        for index, node in enumerate(env.world.nodes[: env.NS])
    }
    return [
        {**args, **change}
        for change in (
            {"order_multiplier": math.exp(-0.5), "safety_stock_days": 0.0},
            {"order_multiplier": 1.0, "safety_stock_days": 1.0},
            {"order_multiplier": math.exp(0.5), "safety_stock_days": 3.0},
            {"expedite_fraction": 0.0},
            {"expedite_fraction": 0.3},
            {"supplier_weights": weights},
            *({"replenishment": formula} for formula in BASELINES),
        )
    ]


def search(initial: Play, plan: Plan, sweeps: int, upper: int) -> tuple[Plan, int, int]:
    """Improve verified seeds by full-suffix replay after legal single-week edits."""
    plan = [OutcomeControls.model_validate(args).model_dump(exclude_none=True) for args in plan]
    states = checkpoints(initial, plan)
    best = surplus_cents(grade_outcomes(states[-1]))
    evaluated = 0
    for _ in range(sweeps):
        previous = best
        for week in reversed(range(len(plan))):
            if best == upper:
                return plan, best, evaluated
            # Reverse order keeps the prefix checkpoint valid as later weeks change.
            state = states[week]
            env = state.env
            chosen = plan[week]
            for args in proposals(state, chosen):
                if args == chosen:
                    continue
                trial = copy.copy(state)
                trial.env = copy.deepcopy(env, {id(env.world): env.world, id(env.tape): env.tape})
                trial.log = state.log.copy()
                if not advance(trial, [args, *plan[week + 1 :]]):
                    continue
                evaluated += 1
                surplus = surplus_cents(grade_outcomes(trial))
                if surplus > best:
                    best, plan[week] = surplus, args
        if best == previous:
            break
        states = checkpoints(initial, plan)
    return plan, best, evaluated


async def verify_plan(seed: int, salt: str, plan: Plan) -> Score:
    task = get_task(TASK)
    await task.reset(seed, salt=salt)
    for args in plan:
        result = await task.step(ToolCall(name="set_controls", args=args))
        if not result.ok:
            raise ValueError(f"Oracle plan was refused: {result.error}")
    grade = await task.grade()
    if not grade.flags["completed"] or len(plan) > task.spec.max_steps:
        raise ValueError("Oracle plan did not complete within the tool-call budget")
    return grade


def verified_trace(job: Path, scenario: str) -> Path | None:
    matches = sorted(job.glob(f"{scenario}__*/verifier/transcript.json"))
    if len(matches) > 1:
        raise ValueError(f"Multiple verified traces for {scenario} in {job}")
    if not matches:
        return None
    trace = matches[0]
    trial = json.loads((trace.parents[1] / "result.json").read_text(encoding="utf-8"))
    error = trial.get("exception_info")
    if error and error["exception_type"] != "AgentTimeoutError":
        return None
    return trace


async def evaluate(config: Path, jobs: dict[str, Path], sweeps: int) -> dict[str, Any]:
    metadata = tomllib.loads(await asyncio.to_thread(config.read_text, encoding="utf-8"))[
        "metadata"
    ]
    if metadata["task_ref"] != TASK:
        raise ValueError(f"Expected a {TASK} Harbor export")
    seed = int(metadata["seed"])
    compose = config.parent / "environment" / "docker-compose.yaml"
    compose_data = yaml.safe_load(await asyncio.to_thread(compose.read_text, encoding="utf-8"))
    env = compose_data["services"]["mcp-server"]["environment"]
    salt = env["SIDECAR_SALT"]
    if env["SIDECAR_TASK"] != TASK or int(env["SIDECAR_SEED"]) != seed or not salt:
        raise ValueError("Cala sidecar identity does not match task metadata")

    outcomes: dict[str, tuple[Score, Plan]] = {}
    missing = []
    for name, job in jobs.items():
        trace = await asyncio.to_thread(verified_trace, job, config.parent.name)
        if trace is None:
            missing.append(name)
            continue
        plan: Plan = []
        outcomes[name] = await replay(trace, seed, salt, controls=plan), plan
    if missing:
        return {"task": TASK, "seed": seed, "status": "missing_verified_runs", "missing": missing}
    for name in LOCAL_POLICIES:
        if name not in outcomes:
            outcomes[name] = await run_policy(name, seed, salt)

    completed = {
        name: surplus_cents(grade)
        for name, (grade, _) in outcomes.items()
        if grade.flags["completed"]
    }
    starting_method = max(completed, key=completed.__getitem__)
    initial = Play(seed, CALA_REGIME, salt)
    upper = surplus_upper_bound(initial)
    plan, lower, evaluated = search(initial, outcomes[starting_method][1], sweeps, upper)
    verified_lower = surplus_cents(await verify_plan(seed, salt, plan))
    if lower != verified_lower or not completed[starting_method] <= lower <= upper:
        raise ValueError("Cala hindsight bounds disagree with the verified simulator replay")
    methods = {}
    for name, (grade, _) in outcomes.items():
        value = surplus_cents(grade)
        finished = name in completed
        methods[name] = {
            "completed": finished,
            "operating_surplus": value / 100,
            "shortfall_to_best_verified": (verified_lower - value) / 100 if finished else None,
            "score": normalized_score(value, verified_lower) if finished else 0.0,
        }
    return {
        "task": TASK,
        "seed": seed,
        "status": "scored",
        "oracle": {
            "best_verified_surplus": verified_lower / 100,
            "score": 1.0,
            "upper_bound": upper / 100,
            "gap": (upper - verified_lower) / 100,
            "global_optimum_certified": verified_lower == upper,
            "starting_method": starting_method,
            "requested_sweeps": sweeps,
            "candidate_plans_evaluated": evaluated,
            "weekly_controls": plan,
        },
        "methods": methods,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-dir", type=Path, required=True, help="One Cala Harbor export folder"
    )
    parser.add_argument(
        "--job",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="Scheduled Harbor result. Repeat for each external model.",
    )
    parser.add_argument("--sweeps", type=int, default=1, help="Weekly search passes (default 1)")
    args = parser.parse_args()
    if args.sweeps < 0:
        parser.error("--sweeps must be nonnegative")
    jobs = {}
    for item in args.job:
        name, separator, path = item.partition("=")
        if not separator or not name or not path or name in jobs:
            parser.error("--job requires a unique NAME=PATH")
        jobs[name] = Path(path)
    report = asyncio.run(evaluate(args.task_dir / "task.toml", jobs, args.sweeps))
    print(json.dumps(report, indent=2, allow_nan=False))
    if report["status"] == "missing_verified_runs":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

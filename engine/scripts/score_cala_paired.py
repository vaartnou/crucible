"""Rescore a Cala Harbor job against four deterministic policies on the same worlds."""

import argparse
import asyncio
import json
import math
import tomllib
from pathlib import Path
from typing import Any

import yaml

from crucible.core.registry import get_policy, get_task
from crucible.core.scoring import Score
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import Footer, Header, StepRecord

TASK = "supplygym-cala@2"
BASELINES = ("ss", "base_stock", "fixed_lot", "fill_adjusted_base_stock")
BASE_WEIGHT = 0.6
WIN_BONUS = 0.1
LOSS_PENALTY = 0.1
TIE_TOLERANCE = 1e-9


def continuous_score(grade: Score) -> float:
    if not grade.flags["completed"]:
        return 0.0
    service = grade.breakdown["service"]
    surplus = grade.breakdown["surplus_ratio"]
    return service * max(0.0, surplus)


def compare(agent: Score, baselines: dict[str, Score]) -> dict[str, Any]:
    if set(baselines) != set(BASELINES):
        raise ValueError("All four baseline scores are required")
    base = continuous_score(agent)
    scores = {name: continuous_score(grade) for name, grade in baselines.items()}
    differences = {name: base - value for name, value in scores.items()}
    wins = sum(delta > TIE_TOLERANCE for delta in differences.values())
    losses = sum(delta < -TIE_TOLERANCE for delta in differences.values())
    paired_score = (BASE_WEIGHT - LOSS_PENALTY * losses) * base + WIN_BONUS * wins
    return {
        "score": paired_score if agent.flags["completed"] else 0.0,
        "completed": agent.flags["completed"],
        "continuous_score": base,
        "operating_surplus": agent.breakdown["operating_surplus"],
        "service": agent.breakdown["service"],
        "wins": wins,
        "losses": losses,
        "ties": len(BASELINES) - wins - losses,
        "baselines": {
            name: {
                "continuous_score": scores[name],
                "operating_surplus": grade.breakdown["operating_surplus"],
                "service": grade.breakdown["service"],
            }
            for name, grade in baselines.items()
        },
    }


async def replay(
    transcript: Path, seed: int, salt: str, *, controls: list[dict[str, Any]] | None = None
) -> Score:
    records = json.loads(await asyncio.to_thread(transcript.read_text, encoding="utf-8"))
    header = Header.model_validate(records[0])
    footer = Footer.model_validate(records[-1])
    task = get_task(TASK)
    if (header.task, header.seed, header.salted, header.scoring_hash) != (
        TASK,
        seed,
        True,
        task.scoring_hash,
    ):
        raise ValueError(f"Transcript does not match the Cala export: {transcript}")
    await task.reset(seed, salt=salt)
    steps = records[1:-1]
    if len(steps) != footer.steps or len(steps) > task.spec.max_steps:
        raise ValueError(f"Invalid transcript step count: {transcript}")
    for index, record in enumerate(steps):
        step = StepRecord.model_validate(record)
        if step.step != index or task.done() or await task.step(step.call) != step.result:
            raise ValueError(f"Cala replay mismatch at step {index}: {transcript}")
        if controls is not None and step.call.name == "set_controls" and step.result.ok:
            controls.append(dict(step.call.args))
    grade = await task.grade()
    if grade.breakdown != footer.score.breakdown or grade.total != footer.score.total:
        raise ValueError(f"Cala verifier grade does not match replay: {transcript}")
    return grade


async def run_baselines(seed: int, salt: str) -> dict[str, Score]:
    grades = {}
    for name in BASELINES:
        grade, _ = await run_policy(name, seed, salt)
        if not grade.flags["completed"]:
            raise ValueError(f"Cala baseline {name} did not finish seed {seed}")
        grades[name] = grade
    return grades


async def run_policy(name: str, seed: int, salt: str) -> tuple[Score, list[dict[str, Any]]]:
    task = get_task(TASK)
    observation = await task.reset(seed, salt=salt)
    policy = get_policy(name, TASK)
    await policy.reset(task.spec, task.tools(), task.instruction())
    controls = []
    for _ in range(task.spec.max_steps):
        if task.done():
            break
        call = await policy.act(observation)
        if call is None:
            break
        result = await task.step(call)
        if not result.ok:
            raise ValueError(f"Cala policy {name} made an invalid call on seed {seed}")
        if call.name == "set_controls":
            controls.append(dict(call.args))
        observation = Observation(text=result_text(result), last_result=result)
    return await task.grade(), controls


async def evaluate(dataset: Path, job: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    configs = await asyncio.to_thread(lambda: sorted(dataset.glob("*/task.toml")))
    for config in configs:
        metadata = tomllib.loads(await asyncio.to_thread(config.read_text, encoding="utf-8"))[
            "metadata"
        ]
        if metadata["task_ref"] != TASK:
            continue
        seed = int(metadata["seed"])
        row: dict[str, Any] = {"scenario": config.parent.name, "seed": seed, "score": None}
        rows.append(row)
        pattern = f"{config.parent.name}__*/verifier/transcript.json"
        traces = await asyncio.to_thread(lambda pattern=pattern: sorted(job.glob(pattern)))
        if not traces:
            row["status"] = "missing_verified_trial"
            continue
        if len(traces) != 1:
            raise ValueError(f"Expected one verified trial for {config.parent.name}")
        trace = traces[0]
        trial_result = json.loads((trace.parents[1] / "result.json").read_text(encoding="utf-8"))
        exception = trial_result.get("exception_info")
        if exception and exception["exception_type"] != "AgentTimeoutError":
            row["status"] = exception["exception_type"]
            continue
        compose = config.parent / "environment" / "docker-compose.yaml"
        environment = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["mcp-server"][
            "environment"
        ]
        salt = environment["SIDECAR_SALT"]
        if (
            not salt
            or environment["SIDECAR_TASK"] != TASK
            or int(environment["SIDECAR_SEED"]) != seed
        ):
            raise ValueError(f"Invalid Cala sidecar configuration: {config}")
        grade = await replay(trace, seed, salt)
        baselines = await run_baselines(seed, salt)
        row.update(compare(grade, baselines), status="verified", transcript=str(trace))
    if not rows:
        raise ValueError("The dataset has no scheduled Cala v2 scenarios")
    if len({row["seed"] for row in rows}) != len(rows):
        raise ValueError("Cala seeds must be unique within the dataset")
    pending = sum(row["score"] is None for row in rows)
    return {
        "rubric": {
            "base_weight": BASE_WEIGHT,
            "win_bonus": WIN_BONUS,
            "loss_penalty": LOSS_PENALTY,
            "tie_tolerance": TIE_TOLERANCE,
        },
        "scheduled_scenarios": len(rows),
        "unresolved_scenarios": pending,
        "valid_for_comparison": pending == 0,
        "overall_score": math.fsum(row["score"] or 0.0 for row in rows) / len(rows),
        "scenarios": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path, help="Original Harbor export")
    parser.add_argument(
        "--job", required=True, type=Path, help="Harbor job containing agent trials"
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(evaluate(args.dataset, args.job)), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

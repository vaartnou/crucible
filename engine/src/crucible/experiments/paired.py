"""Validate a three-world paired pilot without treating model repeats as worlds.

Consumes actual trial records. It never launches trials or substitutes synthetic
results for missing infrastructure. Failed attempts remain in the report.
"""

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

OPERATORS = ("reference", "greedy", "do_nothing", "stop_early", "agent")


class PilotSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    world_ids: tuple[str, str, str]
    reference_verified: bool = False
    model_settings: dict[str, JsonValue]
    rules: dict[str, JsonValue]
    analysis: str = "world-means-paired-t-interval-v1"


class Trial(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    candidate_sha256: str
    world_id: str
    initial_state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator: Literal["reference", "greedy", "do_nothing", "stop_early", "agent"]
    repeat: int = Field(ge=0, le=2)
    attempt: int = Field(default=0, ge=0)
    status: Literal["valid", "infra_error"]
    reward: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    utility: float | None = Field(default=None, allow_inf_nan=False)
    metrics: dict[str, float] = Field(default_factory=dict)
    transcript: str = ""
    error: str | None = None
    # Human-reviewable action -> consequence evidence, kept distinct from causal replay.
    attributions: tuple[str, ...] = ()
    causal_replays: tuple[str, ...] = ()


def analyze(spec: PilotSpec, trials: list[Trial]) -> dict[str, JsonValue]:
    """Return JSON-safe findings; incomplete or mismatched comparisons are inconclusive."""
    problems: list[str] = []
    if len(set(spec.world_ids)) != 3:
        problems.append("The pilot must contain three distinct fresh worlds")
    if not spec.reference_verified:
        problems.append("Reference reliability has not been verified")
    latest: dict[tuple[str, str, int], Trial] = {}
    attempts: set[tuple[str, str, int, int]] = set()
    for trial in trials:
        if (
            trial.experiment_id != spec.experiment_id
            or trial.candidate_sha256 != spec.candidate_sha256
        ):
            problems.append("Trial candidate or experiment identity does not match the freeze")
        if trial.world_id not in spec.world_ids:
            problems.append(f"Undeclared world: {trial.world_id}")
        if trial.operator != "agent" and trial.repeat != 0:
            problems.append("Scripted operators must have repeat=0")
        identity = (trial.world_id, trial.operator, trial.repeat, trial.attempt)
        if identity in attempts:
            problems.append(f"Duplicate attempt: {identity}")
        attempts.add(identity)
        key = identity[:3]
        if key not in latest or latest[key].attempt < trial.attempt:
            latest[key] = trial
    worlds: list[object] = []
    gaps: list[float] = []
    attributed = True
    for world in spec.world_ids:
        selected: dict[str, list[Trial]] = {}
        state_hashes: set[str] = set()
        complete = True
        for operator in OPERATORS:
            selected[operator] = []
            for repeat in range(3 if operator == "agent" else 1):
                trial = latest.get((world, operator, repeat))
                if (
                    trial is None
                    or trial.status != "valid"
                    or trial.reward is None
                    or trial.utility is None
                ):
                    problems.append(f"Missing valid trial: {world}/{operator}/{repeat}")
                    complete = False
                    continue
                if not trial.transcript:
                    problems.append(f"Missing transcript evidence: {world}/{operator}/{repeat}")
                if any(not math.isfinite(v) for v in trial.metrics.values()):
                    problems.append(f"Nonfinite outcome metrics: {world}/{operator}/{repeat}")
                selected[operator].append(trial)
                state_hashes.add(trial.initial_state_sha256)
        if len(state_hashes) != 1:
            problems.append(f"Initial application/world state differs within world {world}")
            complete = False
        if not complete:
            continue
        operators: dict[str, object] = {}
        for operator, rows in selected.items():
            rewards = [float(r.reward) for r in rows if r.reward is not None]
            utilities = [float(r.utility) for r in rows if r.utility is not None]
            operators[operator] = {
                "rewards": rewards,
                "utilities": utilities,
                "mean_reward": statistics.mean(rewards),
                "mean_utility": statistics.mean(utilities),
                "within_world_reward_sd": statistics.stdev(rewards) if len(rewards) > 1 else 0.0,
                "metrics_by_repeat": [dict(r.metrics) for r in rows],
                "attributions": [list(r.attributions) for r in rows],
                "causal_replays": [list(r.causal_replays) for r in rows],
            }
        reference = selected["reference"][0]
        agent = selected["agent"]
        assert reference.reward is not None
        gap = reference.reward - statistics.mean(
            float(r.reward) for r in agent if r.reward is not None
        )
        gaps.append(gap)
        attributed = attributed and any(r.attributions for r in agent)
        worlds.append({"world_id": world, "operators": operators, "paired_reward_gap": gap})
    summary: dict[str, object] = {"independent_worlds": len(gaps)}
    conclusion = "Evaluation inconclusive"
    if len(gaps) == 3 and not problems:
        mean = statistics.mean(gaps)
        standard_error = statistics.stdev(gaps) / math.sqrt(3)
        # Two-sided 95% Student t, df=2; independent units are WORLD means.
        interval = [mean - 4.3026527297 * standard_error, mean + 4.3026527297 * standard_error]
        summary.update(
            {
                "mean_paired_reward_gap": mean,
                "paired_95_percent_t_interval": interval,
                "reference_wins": sum(g > 1e-9 for g in gaps),
                "ties": sum(abs(g) <= 1e-9 for g in gaps),
                "reference_losses": sum(g < -1e-9 for g in gaps),
                "between_world_gap_sd": statistics.stdev(gaps),
            }
        )
        conclusion = (
            "Agent performs worse"
            if interval[0] > 0 and all(g > 0 for g in gaps) and attributed
            else "No demonstrated disadvantage"
        )
    result = {
        "experiment_id": spec.experiment_id,
        "conclusion": conclusion,
        "validity_problems": sorted(set(problems)),
        "summary": summary,
        "worlds": worlds,
        "failed_attempts": [t.model_dump(mode="json") for t in trials if t.status == "infra_error"],
        "attempts_total": len(trials),
        "fresh_world_validation": "Predeclare a fresh evaluation if the pilot justifies it",
    }

    return TypeAdapter(dict[str, JsonValue]).validate_python(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("trials", type=Path, help="JSON array of actual attempts")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = PilotSpec.model_validate_json(args.spec.read_text())
    trials = [Trial.model_validate(item) for item in json.loads(args.trials.read_text())]
    args.out.write_text(json.dumps(analyze(spec, trials), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()

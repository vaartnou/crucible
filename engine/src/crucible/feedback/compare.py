"""Compare a model screening batch with the paired public-rule baseline."""

import json
import math
from pathlib import Path
from typing import Any, cast


def compare(bots: Path, agent: Path) -> dict[str, Any]:
    bot: dict[str, Any] = json.loads((bots / "gates.json").read_text())
    manifest: dict[str, Any] = json.loads((agent / "manifest.json").read_text())
    rows: list[dict[str, Any]] = json.loads((agent / "summary.json").read_text())
    if bot["salt_sha256"] != manifest["salt_sha256"] or bot["task"] != manifest["task"]:
        raise ValueError("Batches must use the same task and world salt")
    bot_has_environment = "environment_sha256" in bot
    agent_has_environment = "environment_sha256" in manifest
    if bot_has_environment != agent_has_environment:
        raise ValueError("Both batches must record environment hashes, or both use legacy hashes")
    if bot_has_environment:
        if not bot.get("environment_sha256") or bot["environment_sha256"] != manifest.get(
            "environment_sha256"
        ):
            raise ValueError("Batches must use the same recorded environment source hash")
    elif not bot.get("simulator_sha256") or bot.get("simulator_sha256") != manifest.get(
        "simulator_sha256"
    ):
        raise ValueError("Batches must use the same recorded simulator source hash")
    seeds = bot["seeds"]
    if not seeds or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Bot batch must contain distinct integer seeds")
    if bot.get("errors") or bot.get("checks", {}).get("no_errors") is False:
        raise ValueError("Bot environment errors invalidate the paired baseline")
    values = bot["scores"]["graph_walk"]
    if len(values) != len(seeds):
        raise ValueError("Bot seeds and graph_walk scores must have equal lengths")
    baseline = dict(zip(seeds, values, strict=True))
    if len(rows) != 10 or len({r["seed"] for r in rows}) != 10:
        raise ValueError("The model gate requires exactly ten distinct seeds")
    declared = manifest.get("seeds")
    actual = [r["seed"] for r in rows]
    if not isinstance(declared, list):
        raise ValueError("Agent manifest seeds must match the ten result rows")
    declared = cast(list[Any], declared)
    if (
        any(type(seed) is not int for seed in declared + actual)
        or len(declared) != len(actual)
        or set(declared) != set(actual)
    ):
        raise ValueError("Agent manifest seeds must match the ten result rows")
    if any(seed not in baseline for seed in actual):
        raise ValueError("Every model seed must have a paired graph_walk score")
    rewards = [*values, *(r["reward"] for r in rows)]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in rewards
    ):
        raise ValueError("Rewards must be finite numbers between zero and one")
    if any(r.get("error") or r["stop_reason"] == "error" for r in rows):
        raise ValueError("Provider/environment errors do not count as model failures")
    deltas = [
        {
            "seed": r["seed"],
            "graph_walk": baseline[r["seed"]],
            "model": r["reward"],
            "gap": baseline[r["seed"]] - r["reward"],
        }
        for r in rows
    ]
    mean = sum(r["gap"] for r in deltas) / len(deltas)

    def meets_gap(value: float) -> bool:
        return value >= 0.20 or math.isclose(value, 0.20, rel_tol=0, abs_tol=1e-12)

    winning = sum(meets_gap(r["gap"]) for r in deltas)
    return {
        "model": manifest["model"],
        "environment_sha256": bot.get("environment_sha256"),
        "bot_simulator_sha256": bot.get("simulator_sha256"),
        "agent_simulator_sha256": manifest.get("simulator_sha256"),
        "mean_gap": mean,
        "seeds_with_gap_at_least_020": winning,
        "passed": meets_gap(mean) and winning >= 7,
        "paired_scores": deltas,
        "measurement": "direct API screening; not Harbor",
    }

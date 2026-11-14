"""Reproduce paired comparisons from saved CSV runs; never runs an agent.

Usage: python -m crucible.gap_proof --help
"""

import argparse
import csv
import math
import random
from collections.abc import Sequence
from itertools import combinations
from pathlib import Path
from statistics import fmean

from crucible.core.jobs import parse_seeds

Scores = dict[str, dict[int, dict[int, float]]]


def load_runs(paths: Sequence[Path], task: str, agents: Sequence[str], seeds: list[int]) -> Scores:
    """Read eval summary.csv files or equivalent task,agent,seed,repeat,score CSVs.

    No implicit intersection, duplicate overwrites, or failed-episode filtering.
    """
    scores: Scores = {agent: {} for agent in agents}
    for path in paths:
        if path.is_dir():
            path = path / "summary.csv"
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or [])
            agent_col = "agent" if "agent" in fields else "policy"
            score_col = "score" if "score" in fields else "total"
            required = {"task", agent_col, "seed", "repeat", score_col}
            if not required <= fields:
                raise ValueError(f"{path}: missing columns {sorted(required - fields)}")
            for line, row in enumerate(reader, 2):
                if row["task"] != task or row[agent_col] not in scores:
                    continue
                try:
                    seed, repeat = int(row["seed"]), int(row["repeat"])
                    if seed not in seeds:
                        continue
                    score = float(row[score_col])
                    if repeat < 0 or not math.isfinite(score):
                        raise ValueError("repeat must be nonnegative and score finite")
                    if row.get("stop_reason") == "error":
                        raise ValueError("episode ended with error; resolve it before reporting")
                    repeats = scores[row[agent_col]].setdefault(seed, {})
                    if repeat in repeats:
                        raise ValueError(f"duplicate ({row[agent_col]}, {seed}, {repeat})")
                    repeats[repeat] = score
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{path}:{line}: {exc}") from exc
    for seed in seeds:
        repeat_sets = [set(scores[a].get(seed, {})) for a in agents]
        if not repeat_sets[0] or any(r != repeat_sets[0] for r in repeat_sets):
            raise ValueError(f"seed {seed}: missing agent results or unequal repeat IDs")
    return scores


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided Binomial(n, .5) sign test, discarding exact ties."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    log_term = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    term = 1.0
    tail = term
    for j in range(k, 0, -1):
        term *= j / (n - j + 1)
        tail += term
    return min(1.0, 2 * math.exp(log_term - n * math.log(2)) * tail)


def bootstrap_ci(
    values: Sequence[float], *, samples: int, alpha: float, rng_seed: int
) -> tuple[float, float]:
    """Percentile CI for the mean, resampling paired seed-level values."""
    if not values or samples < 1 or not 0 < alpha < 1:
        raise ValueError("need values, positive bootstrap samples, and 0 < alpha < 1")
    rng = random.Random(rng_seed)
    means = sorted(fmean(rng.choices(values, k=len(values))) for _ in range(samples))

    def quantile(p: float) -> float:
        index = (len(means) - 1) * p
        lo = math.floor(index)
        hi = math.ceil(index)
        return means[lo] + (means[hi] - means[lo]) * (index - lo)

    return quantile(alpha / 2), quantile(1 - alpha / 2)


def report(
    scores: Scores,
    agents: list[str],
    seeds: list[int],
    *,
    hard_seeds: list[int],
    default: str | None,
    ceiling: str | None,
    samples: int,
    alpha: float,
    rng_seed: int,
) -> str:
    """Full Markdown table, including every seed and every requested agent pair."""
    means = {a: {s: fmean(scores[a][s].values()) for s in seeds} for a in scores}
    subsets = {"all": seeds}
    if hard_seeds:
        subsets["hard (exploratory)"] = hard_seeds
    lines = [
        f"Bootstrap: {samples} samples; RNG seed: {rng_seed}; alpha: {alpha:g}; "
        f"{100 * (1 - alpha):g}% pointwise percentile CIs.",
        "Higher scores win. Difference = A - B. Win rate = wins / all seeds (ties included).",
        "Sign test is two-sided, exact, with ties omitted; p-values are unadjusted.",
        "Repeats are averaged within each seed before analysis; seeds have equal weight.",
        "",
        "| subset | A | B | n seeds | mean A | mean B | mean diff | CI low | CI high "
        "| wins | ties | losses | win rate | sign p |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for subset, selected in subsets.items():
        for a, b in combinations(agents, 2):
            diffs = [means[a][s] - means[b][s] for s in selected]
            lo, hi = bootstrap_ci(diffs, samples=samples, alpha=alpha, rng_seed=rng_seed)
            wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
            ties = len(diffs) - wins - losses
            lines.append(
                f"| {subset} | {a} | {b} | {len(diffs)} | "
                f"{fmean(means[a][s] for s in selected):.8g} | "
                f"{fmean(means[b][s] for s in selected):.8g} | {fmean(diffs):.8g} | "
                f"{lo:.8g} | {hi:.8g} | {wins} | {ties} | {losses} | "
                f"{wins / len(diffs):.8g} | {sign_test(wins, losses):.8g} |"
            )
    if default is not None and ceiling is not None:
        lines.extend(
            [
                "",
                "Normalized gap diagnostic (not a reward): mean of per-seed "
                "(agent - default) / (hindsight ceiling - default).",
                "Nonpositive denominators are undefined and counted, never clamped.",
                "| subset | agent | defined | undefined | mean gap | CI low | CI high |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for subset, selected in subsets.items():
            for a in agents:
                gaps = [
                    (means[a][s] - means[default][s]) / (means[ceiling][s] - means[default][s])
                    for s in selected
                    if means[ceiling][s] > means[default][s]
                ]
                if gaps:
                    lo, hi = bootstrap_ci(gaps, samples=samples, alpha=alpha, rng_seed=rng_seed)
                    stats = f"{fmean(gaps):.8g} | {lo:.8g} | {hi:.8g}"
                else:
                    stats = "NA | NA | NA"
                lines.append(
                    f"| {subset} | {a} | {len(gaps)} | {len(selected) - len(gaps)} | {stats} |"
                )
    lines.extend(
        [
            "",
            "Per-seed paired scores (all requested seeds; repeat means):",
            "| seed | repeats | hard | A | B | score A | score B | diff |",
            "|---:|---:|---|---|---|---:|---:|---:|",
        ]
    )
    for s in seeds:
        for a, b in combinations(agents, 2):
            lines.append(
                f"| {s} | {len(scores[a][s])} | {'yes' if s in hard_seeds else 'no'} | "
                f"{a} | {b} | {means[a][s]:.8g} | {means[b][s]:.8g} | "
                f"{means[a][s] - means[b][s]:.8g} |"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, nargs="+", required=True, help="CSV files or run dirs")
    parser.add_argument("--task", required=True)
    parser.add_argument("--agents", required=True, help="Comma-separated names; A precedes B")
    parser.add_argument("--seeds", required=True, help="Full preregistered seed list, e.g. 0-24")
    parser.add_argument("--hard-seeds", help="Exploratory subset; always also prints full sample")
    parser.add_argument("--default", help="Saved default agent for normalized gap")
    parser.add_argument("--ceiling", help="Saved hindsight ceiling agent for normalized gap")
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=21)
    parser.add_argument("--alpha", type=float, default=0.05)
    args = parser.parse_args(argv)
    try:
        agents = [a.strip() for a in args.agents.split(",")]
        if len(agents) < 2 or not all(agents) or len(set(agents)) != len(agents):
            raise ValueError("--agents needs at least two distinct nonempty names")
        if any("|" in a or "\n" in a for a in agents):
            raise ValueError("agent names cannot contain Markdown table separators or newlines")
        seeds = parse_seeds(args.seeds)
        hard_seeds = parse_seeds(args.hard_seeds) if args.hard_seeds else []
        if not set(hard_seeds) <= set(seeds):
            raise ValueError("--hard-seeds must be a subset of --seeds")
        if (args.default is None) != (args.ceiling is None):
            raise ValueError("give both --default and --ceiling, or neither")
        if args.default is not None and args.default == args.ceiling:
            raise ValueError("--default and --ceiling must differ")
        if args.bootstrap_samples < 1 or not 0 < args.alpha < 1:
            raise ValueError("need positive --bootstrap-samples and 0 < --alpha < 1")
        required_agents = list(
            dict.fromkeys([*agents, *filter(None, [args.default, args.ceiling])])
        )
        scores = load_runs(args.runs, args.task, required_agents, seeds)
        print(f"Task: {args.task}; seeds: {args.seeds}; sources: {', '.join(map(str, args.runs))}")
        print(
            report(
                scores,
                agents,
                seeds,
                hard_seeds=hard_seeds,
                default=args.default,
                ceiling=args.ceiling,
                samples=args.bootstrap_samples,
                alpha=args.alpha,
                rng_seed=args.bootstrap_seed,
            )
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

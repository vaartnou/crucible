"""Paired evaluation on `supplygym@1` or `supplygym-fresh@1`: profit, delta vs default, 95% CI.

    uv run python -m crucible.envs.supplygym.evaluate --split heldout
    uv run python -m crucible.envs.supplygym.evaluate --task supplygym-fresh@1 --split fresh

The clipped 0-1 reward is what Harbor shows. Claims should use the raw paired deltas printed here.
Runs are unsalted, so they match the golden scores, not a salted Harbor task.
"""

import argparse
import asyncio
import random
import statistics
import tempfile
from pathlib import Path

from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.envs.supplygym.play import SPLITS


def bootstrap_ci(values: list[float], n: int = 4000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=sorted(SPLITS), default="dev")
    ap.add_argument("--n", type=int, default=0, help="first n seeds of the split (0 = all)")
    ap.add_argument(
        "--task",
        choices=["supplygym@1", "supplygym-fresh@1", "supplygym-cala@1"],
        default="supplygym@1",
    )
    ap.add_argument("--policies", default="ss,reference,stop_early,do_nothing,max_expedite")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    lo, hi = SPLITS[a.split]
    seeds = list(range(lo, hi))[: a.n or None]
    policies = a.policies.split(",")
    jobs = [
        Job(task=a.task, policy=p, seed=s, repeat=r)
        for p in policies
        for s in seeds
        for r in range(a.repeats)
    ]
    out = a.out or Path(tempfile.mkdtemp(prefix="sg-eval-"))
    results = asyncio.run(LocalExecutor(concurrency=a.concurrency).run(jobs, out))
    by: dict[str, dict[tuple[int, int], tuple[float, float, float]]] = {}
    for r in results:
        b = r.score.breakdown
        by.setdefault(r.job.policy, {})[(r.seed, r.job.repeat)] = (
            r.score.total,
            b["profit"],
            b["delta_vs_ss"],
        )
    print(f"task={a.task} split={a.split} n_seeds={len(seeds)} repeats={a.repeats}")
    print(f"{'policy':14s} {'reward':>7s} {'profit':>9s} {'vs ss':>8s} {'95% CI':>20s} {'win':>5s}")
    for p in policies:
        rows = list(by[p].values())
        d = [x[2] for x in rows]
        lo_ci, hi_ci = bootstrap_ci(d)
        win = sum(1 for x in d if x > 0) / len(d)
        print(
            f"{p:14s} {statistics.fmean(x[0] for x in rows):7.3f} "
            f"{statistics.fmean(x[1] for x in rows):9.0f} {statistics.fmean(d):+8.0f} "
            f"[{lo_ci:+8.0f},{hi_ci:+8.0f}] {win:5.2f}"
        )
    print(f"transcripts: {out}")


if __name__ == "__main__":
    main()

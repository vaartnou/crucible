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
from crucible.core.transcript import read_transcript
from crucible.envs.supplygym.grader import SCORING_HASH
from crucible.envs.supplygym.play import SPLITS


def bootstrap_ci(values: list[float], n: int = 4000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=sorted(SPLITS), default="dev")
    ap.add_argument("--n", type=int, default=0, help="first n seeds of the split (0 = all)")
    ap.add_argument("--task", choices=["supplygym@1", "supplygym-fresh@1"], default="supplygym@1")
    ap.add_argument("--policies", default="ss,reference,stop_early,do_nothing,max_expedite")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--tier", choices=["standard", "hard", "both"], default="standard")
    ap.add_argument(
        "--llm-transcripts", type=Path, help="Folder of graded, unsalted LLM JSONL episodes"
    )
    a = ap.parse_args()
    if a.n < 0 or a.repeats < 1 or a.concurrency < 1:
        ap.error("n must be nonnegative; repeats and concurrency must be positive")
    if a.tier != "standard" and a.split != "fresh":
        ap.error("hard evaluation requires --split fresh (seeds 30000+)")
    if a.tier != "standard":
        from crucible.envs.supplygym.hard import enable_hard_tier

        enable_hard_tier([p.strip() for p in a.policies.split(",")])
    tiers = ["standard", "hard"] if a.tier == "both" else [a.tier]
    out = a.out or Path(tempfile.mkdtemp(prefix="sg-eval-"))
    for tier in tiers:
        task = a.task if tier == "standard" else a.task.replace("@", "-hard@")
        report(a, task, tier, out / tier)


def report(a: argparse.Namespace, task: str, tier: str, out: Path) -> None:
    lo, hi = SPLITS[a.split]
    seeds = list(range(lo, hi))[: a.n or None]
    policies = [p.strip() for p in a.policies.split(",")]
    jobs = [
        Job(task=task, policy=p, seed=s, repeat=r)
        for p in policies
        for s in seeds
        for r in range(a.repeats)
    ]
    results = asyncio.run(LocalExecutor(concurrency=a.concurrency).run(jobs, out))
    by: dict[str, dict[tuple[int, int], tuple[float, float, float]]] = {}
    for r in results:
        if r.score.flags.get("env_error"):
            raise ValueError(f"evaluation failed: {r.transcript_path}")
        b = r.score.breakdown
        by.setdefault(r.job.policy, {})[(r.seed, r.job.repeat)] = (
            r.score.total,
            b["profit"],
            b["delta_vs_ss"],
        )
    if a.llm_transcripts:
        expected = {(s, r) for s in seeds for r in range(a.repeats)}
        for path in sorted(a.llm_transcripts.rglob("*.jsonl")):
            t = read_transcript(path)
            h, f = t.header, t.footer
            if h.task != task:
                continue
            key = (h.seed, h.repeat)
            if key not in expected:
                continue
            if h.salted or h.scoring_hash != SCORING_HASH or f.score.flags.get("env_error"):
                raise ValueError(f"incompatible LLM transcript: {path}")
            name = f"llm:{h.policy}"
            bucket = by.setdefault(name, {})
            if key in bucket:
                raise ValueError(f"duplicate LLM episode: {path}")
            b = f.score.breakdown
            bucket[key] = (f.score.total, b["profit"], b["delta_vs_ss"])
        for name, bucket in by.items():
            if name.startswith("llm:"):
                if set(bucket) != expected:
                    raise ValueError(f"{name}: LLM seeds/repeats do not match this evaluation")
                policies.append(name)
    print(f"tier={tier} task={task} split={a.split} n_seeds={len(seeds)} repeats={a.repeats}")
    if not any(p.startswith("llm:") for p in policies):
        print("LLM: not measured (supply --llm-transcripts to report paired results)")
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

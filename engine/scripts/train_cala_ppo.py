"""Cala public-observation actor pretraining followed by actual on-policy clipped PPO.
Run from engine: python scripts/train_cala_ppo.py --output-path runs/cala_ppo.npz
All fitting, heuristic tuning and checkpoint validation use seeds in 20000..21999.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import numpy as np

from crucible.envs.supplygym.cala_sim import CalaSupplyEnv
from crucible.envs.supplygym.cala_world import cala_world_identity, make_cala_world

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runs/cala_ppo.npz"


def availability_action(x):
    """Exact registered diagnostic shares, without teacher centering."""
    a = np.zeros(16)
    a[14:16] = -0.5
    avail = (np.clip(x[6:70:7], 0.1, 1) + np.clip(1 - (x[3:70:7] - 0.15) / 0.7, 0.1, 1)) / 2
    a[:10] = np.maximum(-1, np.log(avail) / 2)
    return a


def forward(w, x):
    h1 = np.tanh(x @ w["W1"].T + w["b1"])
    h2 = np.tanh(h1 @ w["W2"].T + w["b2"])
    return h2 @ w["Wa"].T + w["ba"], (x, h1, h2)


def backward(w, cache, da):
    x, h1, h2 = cache
    g = {"Wa": da.T @ h2, "ba": da.sum(axis=0)}
    d2 = da @ w["Wa"] * (1 - h2 * h2)
    g.update(W2=d2.T @ h1, b2=d2.sum(axis=0))
    d1 = d2 @ w["W2"] * (1 - h1 * h1)
    g.update(W1=d1.T @ x, b1=d1.sum(axis=0))
    return g


class Adam:
    def __init__(self, w):
        self.m = {k: np.zeros_like(v) for k, v in w.items()}
        self.v = {k: np.zeros_like(v) for k, v in w.items()}
        self.t = 0

    def step(self, w, g, lr):
        self.t += 1
        norm = np.sqrt(sum(np.square(v).sum() for v in g.values()))
        for k in w:
            z = g[k] * min(1, 1 / (norm + 1e-9))
            self.m[k] = 0.9 * self.m[k] + 0.1 * z
            self.v[k] = 0.999 * self.v[k] + 0.001 * z * z
            w[k] -= (
                lr
                * (self.m[k] / (1 - 0.9**self.t))
                / (np.sqrt(self.v[k] / (1 - 0.999**self.t)) + 1e-8)
            )


def episode(env, seed, policy, record=False, rng=None, sigma=None):
    x, _ = env.reset(seed=int(seed))
    rows = []
    done = False
    while not done:
        mu = policy(x)
        a = mu if rng is None else mu + rng.normal(size=16) * sigma
        profit_before = env.tot["profit"]
        nxt, _, _, done, _ = env.step(a)
        r = (env.tot["profit"] - profit_before) / 10500.0
        if record:
            rows.append((x.copy(), a.copy(), mu.copy(), r))
        x = nxt
    return env.tot["profit"], rows


def evaluate(env, seeds, policy):
    return np.array([episode(env, s, policy)[0] for s in seeds])


def train(args):
    started = time.monotonic()
    rng = np.random.default_rng(args.seed)
    env = CalaSupplyEnv(world=make_cala_world())
    # Disjoint training-only partitions, fixed before any checkpoint evaluation.
    # Select the highest mean validation profit after at least one PPO update.
    from cala_teacher import DEFAULT_TEACHER, training_partitions
    from cala_teacher import policy as public_teacher

    teacher_parameters = dict(DEFAULT_TEACHER)
    if args.teacher_config:
        teacher_parameters = json.loads(Path(args.teacher_config).read_text())
        teacher_parameters = teacher_parameters.get("teacher", teacher_parameters)

    def teacher_policy(x):
        return public_teacher(x, teacher_parameters)

    tuning, validation, distill_seeds, gradient_seeds = training_partitions()
    print("teacher", teacher_parameters, flush=True)
    xs = []
    ys = []
    for seed in distill_seeds:
        _, rows = episode(env, seed, teacher_policy, True)
        for x, _, mu, _ in rows:
            xs.append(x)
            ys.append(mu)
    x = np.array(xs)
    y = np.array(ys)
    w = {
        "W1": rng.normal(0, 0.07, (64, 112)),
        "b1": np.zeros(64),
        "W2": rng.normal(0, 0.1, (64, 64)),
        "b2": np.zeros(64),
        "Wa": rng.normal(0, 0.01, (16, 64)),
        "ba": y.mean(0),
    }
    opt = Adam(w)
    for _epoch in range(args.distill_epochs):
        for ids in np.array_split(rng.permutation(len(x)), 16):
            pred, cache = forward(w, x[ids])
            opt.step(w, backward(w, cache, (pred - y[ids]) * 2 / (len(ids) * 16)), 0.002)
    print("distillation mse", float(np.mean((forward(w, x)[0] - y) ** 2)), flush=True)
    baselines = {
        "default": evaluate(env, validation, lambda x: np.zeros(16)),
        "availability": evaluate(env, validation, availability_action),
        "teacher": evaluate(env, validation, teacher_policy),
    }
    with np.load(ROOT / "src/crucible/envs/supplygym/policies/data/supplygym_ppo.npz") as data:
        ow = {k: data[k] for k in ("W1", "b1", "W2", "b2", "Wa", "ba")}
    baselines["old_ppo"] = evaluate(env, validation, lambda x: np.clip(forward(ow, x)[0], -1, 1))
    scores = evaluate(env, validation, lambda x: np.clip(forward(w, x)[0], -1, 1))
    best = -np.inf
    best_w = None
    best_iteration = 0
    best_transitions = 0
    history = [{"iteration": 0, "validation_profit": float(scores.mean())}]
    print(
        "baselines",
        {k: float(v.mean()) for k, v in baselines.items()},
        "initial",
        scores.mean(),
        flush=True,
    )
    sigma = np.array([0.1] * 12 + [0.12, 0.12, 0.06, 0.04])
    opt = Adam(w)
    transitions = 0
    for iteration in range(1, args.iterations + 1):
        xx = []
        aa = []
        mm = []
        monte_carlo_returns = []
        rewards = []
        episode_lengths = []
        profits = []
        seeds = rng.choice(gradient_seeds, size=args.episodes, replace=False)
        for seed in seeds:
            profit, rows = episode(env, int(seed), lambda z: forward(w, z)[0], True, rng, sigma)
            profits.append(profit)
            ex = np.array([r[0] for r in rows])
            ea = np.array([r[1] for r in rows])
            em = np.array([r[2] for r in rows])
            er = np.array([r[3] for r in rows])
            rewards.extend(er)
            episode_lengths.append(len(er))
            discounted = np.zeros(len(er))
            running = 0.0
            for t in range(len(er) - 1, -1, -1):
                running = er[t] + 0.995 * running
                discounted[t] = running
            monte_carlo_returns.extend(discounted)
            xx.extend(ex)
            aa.extend(ea)
            mm.extend(em)
        xx = np.array(xx)
        aa = np.array(aa)
        mm = np.array(mm)
        transitions += len(xx)
        features = np.column_stack((xx, np.ones(len(xx))))
        # Fit the predictable time/terminal-credit profile before computing GAE.
        # This uses rewards from completed on-policy trajectories only.
        ret = np.array(monte_carlo_returns)
        value = np.linalg.solve(features.T @ features + np.eye(113), features.T @ ret)
        vals = features @ value
        adv = np.zeros(len(xx))
        offset = 0
        for length in episode_lengths:
            gae = 0.0
            for t in range(offset + length - 1, offset - 1, -1):
                nextv = vals[t + 1] if t + 1 < offset + length else 0.0
                gae = rewards[t] + 0.995 * nextv - vals[t] + 0.995 * 0.95 * gae
                adv[t] = gae
            offset += length
        adv = (adv - adv.mean()) / (adv.std() + 1e-8)
        # Gaussian density is evaluated before action clipping. Clipping belongs
        # to the environment, so these are the exact sampled-action ratios.
        oldlog = -0.5 * np.sum(((aa - mm) / sigma) ** 2, axis=1)
        clipfracs = []
        kls = []
        for _epoch in range(4):
            for ids in np.array_split(rng.permutation(len(xx)), max(1, len(xx) // 256)):
                mu, cache = forward(w, xx[ids])
                log = -0.5 * np.sum(((aa[ids] - mu) / sigma) ** 2, axis=1)
                ratio = np.exp(np.clip(log - oldlog[ids], -20, 20))
                active = ((adv[ids] >= 0) & (ratio <= 1.2)) | ((adv[ids] < 0) & (ratio >= 0.8))
                coeff = -adv[ids] * ratio * active / len(ids)
                da = coeff[:, None] * (aa[ids] - mu) / (sigma * sigma)
                opt.step(w, backward(w, cache, da), args.learning_rate)
                clipfracs.extend(abs(ratio - 1) > 0.2)
                kls.extend(oldlog[ids] - log)
            if np.mean(kls) > 0.025:
                break
        scores = evaluate(env, validation, lambda z: np.clip(forward(w, z)[0], -1, 1))
        row = {
            "iteration": iteration,
            "train_profit": float(np.mean(profits)),
            "validation_profit": float(scores.mean()),
            "clip_fraction": float(np.mean(clipfracs)),
            "approx_kl": float(np.mean(kls)),
        }
        history.append(row)
        print(json.dumps(row), flush=True)
        if scores.mean() > best:
            best = float(scores.mean())
            best_w = {k: v.copy() for k, v in w.items()}
            best_iteration = iteration
            best_transitions = transitions
    if best <= history[0]["validation_profit"]:
        raise RuntimeError(
            "No PPO checkpoint improved initialization on train-validation; no export"
        )
    final = evaluate(env, validation, lambda z: np.clip(forward(best_w, z)[0], -1, 1))
    metrics = {
        k: {"mean_profit": float(v.mean()), "cala_ppo_minus_baseline": float((final - v).mean())}
        for k, v in baselines.items()
    }
    metadata = {
        "algorithm": "Gaussian on-policy PPO clipped ratio; NumPy analytic backprop + Adam",
        "architecture": [112, 64, 64, 16],
        "seed": args.seed,
        "train_seed_range_half_open": [20000, 22000],
        "distillation_seeds": distill_seeds,
        "ppo_seed_partition": {
            "definition": "20000..21999 excluding both stratified banks and distillation seeds",
            "count": len(gradient_seeds),
            "sha256_json": hashlib.sha256(json.dumps(gradient_seeds).encode()).hexdigest(),
        },
        "selection_rule": "highest deterministic mean validation profit among PPO iterations 1..N",
        "ppo_parameters": {
            "gamma": 0.995,
            "gae_lambda": 0.95,
            "clip_epsilon": 0.2,
            "max_epochs": 4,
            "target_kl": 0.025,
            "sigma": sigma.tolist(),
        },
        "reward": "full step profit delta including approval fees and terminal credit, /10500",
        "teacher_tuning_seeds": tuning,
        "teacher_selection_provenance": {
            "pilot_seed_range_half_open": [21744, 21808],
            "pilot_candidates": 55,
            "stratified_tuning_candidates": 18,
            "selection_finalists": 3,
            "note": "Frozen parameters included; searches need not be repeated",
        },
        "checkpoint_validation_seeds": validation,
        "heldout_used": False,
        "teacher": teacher_parameters,
        "selected_iteration": best_iteration,
        "ppo_transitions": transitions,
        "checkpoint_ppo_transitions": best_transitions,
        "history": history,
        "validation_comparisons": metrics,
        "validation_profit": final.tolist(),
        "world": cala_world_identity(),
        "elapsed_seconds": time.monotonic() - started,
        "arguments": {
            k: v for k, v in vars(args).items() if k not in ("output_path", "teacher_config")
        },
    }
    output = Path(args.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, **best_w)
    metadata["model_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".training.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    print("RESULT", json.dumps(metadata), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--episodes", type=int, default=48)
    parser.add_argument("--distill-epochs", type=int, default=240)
    parser.add_argument("--learning-rate", type=float, default=0.00004)
    parser.add_argument("--seed", type=int, default=7126)
    parser.add_argument("--output-path", default=str(OUTPUT))
    parser.add_argument("--teacher-config")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive: an actual PPO update is required")
    if args.episodes < 1 or args.distill_epochs < 0:
        parser.error("--episodes must be positive and --distill-epochs nonnegative")
    train(args)

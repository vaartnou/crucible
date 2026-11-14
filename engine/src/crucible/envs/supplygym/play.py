# pyright: basic
"""One SupplyGym episode as the task, the grader replays and the tests all play it."""

import copy
from dataclasses import dataclass
from typing import Any

import numpy as np

from crucible.envs.supplygym.sim import ACT_DIM, SupplyEnv
from crucible.envs.supplygym.state import (
    alternative_error,
    build_status,
    status_controls,
    to_action,
)

# regime -> simulator parameters. 0 (`supplygym`) is the calibration the reports were written on;
# 1 (`supplygym-fresh`) is the fresh regime: another supplier graph, wider price spread and 1.5x
# the disruption rate.
REGIMES: dict[int, dict[str, float | int]] = {
    0: {"world_seed": 7, "rate_scale": 1.0, "price_lo": 9.6, "price_hi": 10.4},
    1: {"world_seed": 11, "rate_scale": 1.5, "price_lo": 9.0, "price_hi": 11.0},
}

# Seed ranges, [lo, hi). Training seeds are for the external trainer only; the others are used for
# evaluation. A test asserts they do not overlap.
SPLITS: dict[str, tuple[int, int]] = {
    "train": (20000, 22000),
    "dev": (1000, 1030),
    "heldout": (10000, 10050),
    "fresh": (30000, 30050),
}


@dataclass
class Week:
    """What happened during one decision step (public)."""

    profit: float
    lost_units: int
    expedite_cost: float
    switching_fee: float


class Play:
    """A simulator, its public signal log and the controls API."""

    def __init__(
        self,
        seed: int,
        regime: int = 0,
        salt: str = "",
        signals: str = "full",
        *,
        demand_shocks: bool = False,
    ) -> None:
        if regime not in REGIMES:
            raise ValueError(f"unknown regime {regime}; known: {sorted(REGIMES)}")
        self.seed, self.regime = seed, regime
        self.env = SupplyEnv(
            signals=signals,
            salt=salt,
            demand_shocks=demand_shocks,
            **REGIMES[regime],  # type: ignore[arg-type]
        )
        self.env.reset(seed=seed)
        self.log: list[dict[str, Any]] = []
        self.weeks = 0
        self.finished = False
        self.ledger: dict[str, Any] | None = None

    @property
    def done(self) -> bool:
        return self.finished or self.env.day >= self.env.horizon

    def status(self) -> dict[str, Any]:
        return build_status(self.env, self.log, self.ledger)

    def refusal(self, args: dict[str, Any]) -> str | None:
        """Why this call would be refused (it then changes nothing), else `None`."""
        name = args.get("approve_alternative")
        return alternative_error(self.env, name) if name else None

    def apply(self, args: dict[str, Any]) -> Week:
        """Run one 7-day step under the new control. Caller has checked `refusal`."""
        env = self.env
        keys = (
            "profit",
            "lost",
            "expedite",
            "switching",
            "holding",
            "stockout",
            "purchase",
            "revenue",
        )
        t0 = {k: env.tot[k] for k in keys}
        before = status_controls(env)
        a = to_action(env, args)
        # what the default controls would have earned this week, from the same state and the same
        # realised demand: only the week that has just been played, so nothing about the future
        alt = copy.copy(env)
        alt.__dict__ = {k: copy.deepcopy(v) for k, v in env.__dict__.items() if k != "tape"}
        alt.tape = env.tape
        alt.step(np.zeros(ACT_DIM))
        _, _, _, _, info = env.step(a)
        for item in info["news"] + info["rumors"] + info["updates"]:
            self.log.append({k: v for k, v in item.items() if k not in ("node", "severity")})
        self.weeks += 1
        after = status_controls(env)
        self.ledger = {
            "week": self.weeks,
            "revenue": round(float(env.tot["revenue"] - t0["revenue"]), 2),
            "supplier_cost": round(float(env.tot["purchase"] - t0["purchase"]), 2),
            "expedite": round(float(env.tot["expedite"] - t0["expedite"]), 2),
            "holding": round(float(env.tot["holding"] - t0["holding"]), 2),
            "lost_sales": round(float(env.tot["stockout"] - t0["stockout"]), 2),
            "switching": round(float(env.tot["switching"] - t0["switching"]), 2),
            "profit": round(float(env.tot["profit"] - t0["profit"]), 2),
            "delta_vs_default": round(float(env.tot["profit"] - alt.tot["profit"]), 2),
            "previous_controls": before,
            "controls_changed": sum(1 for k in after if after[k] != before[k]),
        }
        return Week(
            profit=env.tot["profit"] - t0["profit"],
            lost_units=int(env.tot["lost"] - t0["lost"]),
            expedite_cost=env.tot["expedite"] - t0["expedite"],
            switching_fee=env.tot["switching"] - t0["switching"],
        )

    def run_default(self) -> None:
        """Play the remaining weeks on the default (s,S) controls."""
        zero = np.zeros(ACT_DIM)
        while self.env.day < self.env.horizon:
            self.env.step(zero)

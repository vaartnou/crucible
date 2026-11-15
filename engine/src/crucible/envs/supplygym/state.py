# pyright: basic
"""What the agent may see (`status`) and how tool arguments become a standing control.

Nothing here reads the tape. `status` is built from the same public quantities the original
SupplyGym observation used; values are rounded to 6 decimals so that a policy fed from this view
and a policy replayed in-process see identical numbers.
"""

import math
from typing import Any

import numpy as np

from crucible.envs.supplygym.sim import ACT_DIM, N_DIRECT, REGIONS, SupplyEnv

SIGNAL_DAYS = 21
ALT_IDS = ("A01", "A02")
_COST_KEYS = ("purchase", "holding", "stockout", "expedite", "switching")
_R = 6


def _r(x: float) -> float:
    return round(float(x), _R)


def supplier_id(env: SupplyEnv, i: int) -> str:
    return str(env.world.nodes[i]["id"])


def status_controls(env: SupplyEnv) -> dict[str, Any]:
    """Standing controls as the agent sees them, after clipping to what the simulator applies."""
    c = env.ctrl
    return {
        "supplier_weights": {
            supplier_id(env, i): _r(math.exp(4.0 * float(c[i]))) for i in range(env.NS)
        },
        "safety_stock_days": _r(3.0 * max(0.0, float(c[13]))),
        "order_multiplier": _r(math.exp(0.5 * float(c[12]))),
        "expedite_fraction": _r(0.3 * max(0.0, float(c[14]))),
        **({"replenishment": env.replenishment} if env.replenishment != "ss" else {}),
    }


def build_status(
    env: SupplyEnv, signals: list[dict[str, Any]], ledger: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Public state at the current decision point."""
    d = env.day
    act = set(env.active())
    sups: list[dict[str, Any]] = []
    for i in range(env.NS):
        node = env.world.nodes[i]
        pend = [o for o in env.orders if o["sup"] == i]
        pending = sum(o["ordered"] if d < o["rev"] else o["qty"] for o in pend)
        nxt = min((env.reported_arrival(o) - d for o in pend), default=None)
        if i in act:
            state, left = "active", 0
        elif i in env.approved:
            state, left = "onboarding", int(env.approved[i] - d)
        else:
            state, left = ("not_approved" if node["kind"] == "alt" else "inactive"), 0
        sups.append(
            {
                "id": node["id"],
                "region": node["region"],
                "kind": node["kind"],
                "state": state,
                "onboarding_days_left": left,
                "lead_days": int(env.lead[i]),
                "capacity": int(env.cap[i]),
                "price": float(node["price"]),
                "risk": _r(env.risk[i]) if env.use_ops else 0.0,
                "late_orders": sum(1 for o in pend if o["nom"] < d) if env.use_ops else 0,
                "fill_ratio": _r(env.fill[i]) if env.use_ops else 1.0,
                "pending_units": _r(pending),
                "days_to_next_arrival": nxt,
            }
        )
    regions: dict[str, dict[str, float]] = {}
    for r, name in enumerate(REGIONS):
        low = sum(
            1 for x in env.rum_active if env.reg_idx[x["region"]] == r and x["credibility"] == "low"
        )
        med = sum(
            1
            for x in env.rum_active
            if env.reg_idx[x["region"]] == r and x["credibility"] == "medium"
        )
        regions[name] = {
            "news_intensity": _r(math.tanh(env.news_ewma[r])),
            "days_since_news": _r(min(60, d - env.last_news[r])),
            "rumors_low": low,
            "rumors_medium": med,
        }
    s_lvl, _ = env.levels(list(act), 0)
    operating_surplus = env.tot["revenue"] - sum(env.tot[k] for k in _COST_KEYS)
    out = {
        "day": d,
        "weeks_left": max(0, math.ceil((env.horizon - d) / env.k)),
        "onhand": _r(env.onhand),
        "inventory_position": _r(env.onhand + env._pipeline()),
        "base_stock_level": _r(s_lvl),
        "suppliers": sups,
        "regions": regions,
        "signals": [s for s in signals if d - s["day"] <= SIGNAL_DAYS],
        "controls": status_controls(env),
        "profit_so_far": _r(env.tot["profit"]),
        "operating_surplus_so_far": _r(operating_surplus),
    }
    if ledger is not None:
        out["last_week"] = ledger
    return out


def next_alternative(env: SupplyEnv) -> str | None:
    """Id of the alternative that `approve_alternative` would approve now, if any."""
    for i in range(N_DIRECT, env.NS):
        if i not in env.approved:
            return supplier_id(env, i)
    return None


def alternative_error(env: SupplyEnv, name: str) -> str | None:
    """Reason the approval is refused, or `None` if `name` is the next approvable alternative."""
    ids = {supplier_id(env, i): i for i in range(N_DIRECT, env.NS)}
    if name not in ids:
        return f"approve_alternative: unknown alternative {name!r}. Known: {', '.join(ids)}"
    i = ids[name]
    if i in env.approved:
        left = max(0, env.approved[i] - env.day)
        what = "active" if left == 0 else f"onboarding ({left} days left)"
        return f"approve_alternative: {name} is already approved and {what}. No fee was charged."
    nxt = next_alternative(env)
    if nxt != name:
        return f"approve_alternative: {nxt} must be approved before {name}."
    return None


def to_action(env: SupplyEnv, args: dict[str, Any]) -> np.ndarray:
    """New standing control: the previous one with the given fields replaced."""
    a = env.ctrl.copy()
    a[15] = -1.0
    a[16] = -1.0
    ids = {supplier_id(env, i): i for i in range(env.NS)}
    for sid, v in (args.get("supplier_weights") or {}).items():
        a[ids[sid]] = float(np.clip(math.log(v) / 4.0, -1.0, 1.0))
    if args.get("safety_stock_days") is not None:
        a[13] = float(np.clip(args["safety_stock_days"] / 3.0, 0.0, 1.0))
    if args.get("order_multiplier") is not None:
        a[12] = float(np.clip(2.0 * math.log(args["order_multiplier"]), -1.0, 1.0))
    if args.get("expedite_fraction") is not None:
        a[14] = float(np.clip(args["expedite_fraction"] / 0.3, 0.0, 1.0))
    if args.get("approve_alternative"):
        a[15] = 1.0
    assert a.size == ACT_DIM
    return a

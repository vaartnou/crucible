"""Typed view of `supplygym@1` tool results, for policies.

Only what a tool result shows: no import from `crucible.tasks`. `to_vector` rebuilds the 112
numbers the original SupplyGym PPO observation used, from the public status view alone.
"""

import math
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, JsonValue

from crucible.core.tools import ToolCall, ToolResult

REGIONS = ("NA", "EU", "APAC-N", "APAC-S", "LATAM")
DEMAND = 100.0


class SupplierView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    region: str
    kind: str
    state: str
    onboarding_days_left: int
    lead_days: int
    capacity: int
    price: float
    risk: float
    late_orders: int
    fill_ratio: float
    pending_units: float
    days_to_next_arrival: int | None


class RegionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    news_intensity: float
    days_since_news: float
    rumors_low: int
    rumors_medium: int


class StatusView(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: int
    weeks_left: int
    onhand: float
    inventory_position: float
    base_stock_level: float
    suppliers: list[SupplierView]
    regions: dict[str, RegionView]
    signals: list[dict[str, JsonValue]]
    controls: dict[str, JsonValue]
    profit_so_far: float
    operating_surplus_so_far: float | None = None
    last_week: dict[str, JsonValue] | None = None


def parse_status(result: ToolResult) -> StatusView:
    """Status from a `get_status` or `set_controls` result."""
    if not result.ok or not isinstance(result.content, dict):
        raise ValueError(f"not a status result: {result.error or result.content!r}")
    body = result.content.get("status", result.content)
    return StatusView.model_validate(body)


def episode_over(result: ToolResult) -> bool:
    c = result.content
    return isinstance(c, dict) and c.get("episode_over") is True


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, args={k: v for k, v in args.items() if v is not None})


def to_vector(v: StatusView) -> np.ndarray:
    """The original 112-dimensional observation, rebuilt from the status view."""
    f: list[float] = []
    for s in v.suppliers:
        on = 1.0 if s.state == "active" else 0.0
        units = s.pending_units / (DEMAND * max(1, s.lead_days) / 3.0)
        nxt = (s.days_to_next_arrival if s.days_to_next_arrival is not None else 30) / 30.0
        f += [
            on,
            units * on,
            nxt * on,
            s.risk * on,
            s.late_orders / 5.0 * on,
            s.lead_days / 30.0,
            s.fill_ratio * on,
        ]
    for name in REGIONS:
        r = v.regions[name]
        f += [
            r.news_intensity,
            min(60.0, r.days_since_news) / 30.0,
            min(r.rumors_low, 3) / 3.0,
            min(r.rumors_medium, 3) / 3.0,
        ]
    d = v.day
    active_alts = sum(1 for s in v.suppliers if s.kind == "alt" and s.state == "active")
    ss = float(v.controls["safety_stock_days"])  # type: ignore[arg-type]
    mult = float(v.controls["order_multiplier"])  # type: ignore[arg-type]
    f += [
        v.onhand / (7 * DEMAND),
        v.inventory_position / v.base_stock_level,
        math.sin(2 * math.pi * d / 365),
        math.cos(2 * math.pi * d / 365),
        d / 365.0,
        active_alts / 2.0,
        ss / 3.0,
        2.0 * math.log(mult),
    ]
    return np.clip(np.array(f, dtype=np.float32), -5, 5)

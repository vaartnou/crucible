# pyright: basic
"""`supplygym@1` task: tools, clock and instruction text.

The tape is hidden. Every tool result is built by `state.build_status` and `Week`, never from the
tape. A refused call changes nothing: not the clock, not the controls, not the fee.
"""

import math
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, Field, JsonValue

from crucible.core.registry import register_task
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskChecks, TaskSpec
from crucible.core.tools import (
    Observation,
    ToolCall,
    ToolResult,
    ToolSpec,
    tool_map,
    validate_call,
)
from crucible.envs.supplygym.grader import SCORING_HASH, grade_play
from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.sim import (
    ACK_DAYS,
    DEMAND_MEAN,
    EXPEDITE_COST,
    HOLD_COST,
    HORIZON,
    ONBOARD_DAYS,
    PRICE_SELL,
    STOCKOUT_PEN,
    SWITCH_COST,
)

__all__ = ["SupplyGymTask"]

MAX_STEPS = 120
_SUPPLIERS = [f"S{k:02d}" for k in range(1, 11)] + ["A01", "A02"]

# exp(+-4): the whole accepted range is representable, nothing is clamped silently
Weight = Annotated[float, Field(ge=math.exp(-4.0), le=math.exp(4.0))]


class NoArgs(BaseModel):
    """Arguments of a tool that takes none."""


class ControlsArgs(BaseModel):
    """Arguments of `set_controls`. Omitted fields keep their current value."""

    supplier_weights: dict[str, Weight] | None = Field(
        default=None,
        description="Per-supplier multiplier on its default order share (1 = default, 0.1 = cut "
        "about 90%, 2 = double). Ids S01..S10, A01, A02. Only the ids you list change.",
    )
    safety_stock_days: float | None = Field(
        default=None, ge=0, le=3, description="Extra safety stock in days of demand (0 to 3)."
    )
    order_multiplier: float | None = Field(
        default=None,
        ge=math.exp(-0.5),
        le=math.exp(0.5),
        description="Scales every order (0.607 to 1.649).",
    )
    expedite_fraction: float | None = Field(
        default=None,
        ge=0,
        le=0.3,
        description=f"Share of each order shipped at half lead time for +${EXPEDITE_COST:g}/unit.",
    )
    approve_alternative: str | None = Field(
        default=None,
        description="'A01', then later 'A02': approve the next alternative supplier (one-off fee, "
        f"{ONBOARD_DAYS} days of onboarding before it can ship).",
    )


_TOOLS = [
    ToolSpec(
        name="get_briefing",
        description="Static facts: suppliers (region, lead time, capacity, price) and economics.",
        kind="read",
        args_model=NoArgs,
    ),
    ToolSpec(
        name="get_status",
        description="Current inventory, per-supplier health signals, regional news and rumors, and "
        "your current controls. The same view is returned after every set_controls.",
        kind="read",
        args_model=NoArgs,
    ),
    ToolSpec(
        name="set_controls",
        description="Set the standing controls for the next 7 days and advance the simulation by "
        "one week. Fields you omit keep their current value (the start is the default rule).",
        kind="action",
        args_model=ControlsArgs,
    ),
    ToolSpec(
        name="finish",
        description="Stop now. The remaining weeks run on the default rule, which scores 0.",
        kind="end",
        args_model=NoArgs,
    ),
]
_TOOL_MAP = tool_map(_TOOLS)


def _fail(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message)


@register_task
class SupplyGymTask(Task):
    """Run one year of inbound supply for one product, deciding once a week."""

    spec: ClassVar[TaskSpec] = TaskSpec(
        id="supplygym",
        version=1,
        max_steps=MAX_STEPS,
        title="Crucible: supply-chain control",
        description="Weekly control of one product's inbound supply for a year under hidden "
        "supplier disruptions.",
        keywords=("supply-chain",),
        timeout_sec=3600.0,
    )
    scoring_hash: ClassVar[str] = SCORING_HASH
    # `ss` is the reward anchor and `max_expedite` a cost exploit; both are pinned in the golden
    # scores. The reference does not solve every seed and `stop_early` (reference for 4 weeks)
    # often matches it, a known cold-start issue (README, Known limits), so those checks are off.
    checks: ClassVar[TaskChecks] = TaskChecks(
        exploit_policies=("ss", "stop_early", "max_expedite"),
        reference_solves=False,
        exploits_below_reference=False,
    )
    # Simulator regime (`play.REGIMES`). Each regime is its own task, not a `difficulty`.
    regime: ClassVar[int] = 0

    def __init__(self, *, demand_shocks: bool = False) -> None:
        self.demand_shocks = demand_shocks
        self._play: Play | None = None

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        # `difficulty` is unused: the regime is fixed per task (see `SupplyGymFreshTask`).
        # No salt (offline eval, golden scores) keeps the plain seed: `effective_seed(seed, "")`.
        self._play = Play(seed, self.regime, salt or "", demand_shocks=self.demand_shocks)
        return Observation(text=self.instruction(), data={"day": 0})

    def tools(self) -> list[ToolSpec]:
        return list(_TOOLS)

    def done(self) -> bool:
        return self._play is not None and self._play.done

    def _briefing(self, play: Play) -> dict[str, JsonValue]:
        env = play.env
        sups: list[JsonValue] = [
            {
                "id": s["id"],
                "region": s["region"],
                "kind": s["kind"],
                "lead_days": s["lead"],
                "capacity_per_day": s["cap"],
                "price": s["price"],
            }
            for s in env.world.public()
        ]
        return {
            "horizon_days": HORIZON,
            "decision_interval_days": env.k,
            "sell_price": PRICE_SELL,
            "demand_mean_per_day": DEMAND_MEAN,
            "holding_cost_per_unit_day": HOLD_COST,
            "lost_sale_penalty_per_unit": STOCKOUT_PEN,
            "expedite_cost_per_unit": EXPEDITE_COST,
            "alternative_switching_fee": SWITCH_COST,
            "alternative_onboarding_days": ONBOARD_DAYS,
            "regions": ["NA", "EU", "APAC-N", "APAC-S", "LATAM"],
            "suppliers": sups,
        }

    async def step(self, call: ToolCall) -> ToolResult:
        play = self._play
        if play is None:
            return _fail("Episode not started: call reset first")
        if play.done:
            return _fail("Episode is over")
        parsed = validate_call(_TOOL_MAP, call)
        if isinstance(parsed, ToolResult):
            return parsed
        if isinstance(parsed, ControlsArgs):
            args: dict[str, Any] = parsed.model_dump(exclude_none=True)
            unknown = [s for s in args.get("supplier_weights", {}) if s not in _SUPPLIERS]
            if unknown:
                return _fail(
                    f"set_controls: unknown supplier id(s) {', '.join(unknown)}. "
                    f"Known: {', '.join(_SUPPLIERS)}"
                )
            why = play.refusal(args)
            if why is not None:
                return _fail(why)
            week = play.apply(args)
            return ToolResult(
                ok=True,
                content={
                    "last_week": {
                        "profit": round(week.profit, 2),
                        "lost_units": week.lost_units,
                        "expedite_cost": round(week.expedite_cost, 2),
                        "switching_fee": round(week.switching_fee, 2),
                    },
                    "status": play.status(),
                    "episode_over": play.done,
                },
            )
        if call.name == "get_briefing":
            return ToolResult(ok=True, content=self._briefing(play))
        if call.name == "get_status":
            return ToolResult(ok=True, content=play.status())  # type: ignore[arg-type]
        play.finished = True
        return ToolResult(ok=True, content="finished; remaining weeks run on the default rule")

    async def grade(self) -> Score:
        if self._play is None:
            raise ValueError("Episode not started: call reset first")
        return grade_play(self._play)

    def instruction(self) -> str:
        return f"""You manage the inbound supply of one product for a year ({HORIZON} days). \
You decide once a week (about 53 decisions) and the simulation then runs 7 days under your \
controls. Demand averages {DEMAND_MEAN:g} units a day and you sell at ${PRICE_SELL:g}. Unmet \
demand is lost and costs the margin plus ${STOCKOUT_PEN:g}; holding costs ${HOLD_COST:g} per unit \
per day.

You buy from 10 direct suppliers (S01..S10, in 5 regions) and may approve up to 2 pricier \
alternatives (A01, then A02) for a one-off fee of ${SWITCH_COST:g} and {ONBOARD_DAYS} days of \
onboarding. Disruptions (strikes, port closures, fires, sanctions, bankruptcies) hit suppliers \
at random and spread to the suppliers that depend on them. You never see them directly. You \
see noisy signals: news (about half the headlines are false alarms; a headline's region or type \
can be wrong), rumors (low or medium credibility; they later turn real or fizzle), a weekly \
risk score per supplier, late orders and a fill ratio (share of an order a supplier actually \
shipped, visible about {ACK_DAYS} days after ordering).

Orders are placed automatically every day from your standing controls. The default controls \
(all fields omitted) are a classic base-stock rule that sizes orders from demand and lead \
times and splits them across suppliers in proportion to capacity. Goods usually arrive just in \
time, so low on-hand at decision time is normal and not an emergency by itself.

Tools: get_briefing (static facts), get_status (current view), set_controls (apply controls \
and advance one week; omitted fields keep their value; refused calls change nothing), finish. \
You have {MAX_STEPS} tool calls. Stopping early, or never calling set_controls, leaves the \
remaining weeks on the default rule.

Scoring (0 to 1): your year's profit is compared with the default rule on the same year \
(0 = no better, which is also what stopping early or doing nothing earns) and with a strong \
reference agent that sees the same information as you (1 = as good as the reference). Costs \
count: expedite is ${EXPEDITE_COST:g} per unit; an alternative costs the fee plus a higher \
unit price.""" + (
            "\n\nDemand shocks are enabled: demand can surge or dip for 2-4 weeks. "
            "Status includes observed_demand (day and units for the last 28 completed days), "
            "including unmet demand. Supplier prices stay fixed."
            if self.demand_shocks
            else ""
        )


@register_task
class SupplyGymFreshTask(SupplyGymTask):
    """The same task in the fresh regime: the reference and default rule were not tuned on it."""

    spec: ClassVar[TaskSpec] = SupplyGymTask.spec.model_copy(
        update={
            "id": "supplygym-fresh",
            "title": "Crucible: supply-chain control (fresh regime)",
            "description": "Weekly control of one product's inbound supply for a year under "
            "hidden supplier disruptions, on a supplier graph, price spread and disruption rate "
            "the reference was not tuned on.",
        }
    )
    regime: ClassVar[int] = 1

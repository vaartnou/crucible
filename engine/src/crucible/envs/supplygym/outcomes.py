"""Version 2 outcome rubrics, computed exclusively from the authoritative ledger."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any, ClassVar, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crucible.core.registry import register_task
from crucible.core.scoring import Score, load_scoring_config
from crucible.core.tools import ToolCall, ToolResult, ToolSpec, tool_map, validate_call
from crucible.envs.supplygym.cala_task import SupplyGymCalaTask
from crucible.envs.supplygym.cala_world import cala_world_identity
from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.task import ControlsArgs, SupplyGymFreshTask, SupplyGymTask
from crucible.envs.supplygym.world import PRICE_SELL


class OutcomeRubric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    full_service: float = Field(ge=0, le=1)
    full_surplus_ratio: float
    partial_service: float = Field(ge=0, le=1)
    partial_surplus_ratio: float
    partial_credit: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def ordered(self) -> OutcomeRubric:
        if self.full_service < self.partial_service:
            raise ValueError("Full service threshold must be at least the partial threshold")
        if self.full_surplus_ratio < self.partial_surplus_ratio:
            raise ValueError("Full surplus threshold must be at least the partial threshold")
        return self


RUBRIC, RUBRIC_HASH = load_scoring_config(
    Path(__file__).with_name("outcome_scoring.yaml"), OutcomeRubric
)
COST_KEYS = ("purchase", "holding", "stockout", "expedite", "switching")
CALA_V3_MAX_STEPS = 200


def grade_outcomes(play: Play, rubric: OutcomeRubric = RUBRIC) -> Score:
    """Reading the ledger never advances the clock or completes an abandoned episode."""
    env = play.env
    ledger = env.tot
    complete = env.day == env.horizon and play.weeks == math.ceil(env.horizon / env.k)
    required = ("demand", "sales", "lost", "revenue", *COST_KEYS)
    valid = all(math.isfinite(ledger[k]) and ledger[k] >= 0 for k in required)
    valid = valid and ledger["sales"] + ledger["lost"] == ledger["demand"]
    valid = valid and math.isclose(ledger["revenue"], PRICE_SELL * ledger["sales"])
    if not valid:
        raise ValueError("Authoritative accounting failed verification")
    demand = ledger["demand"]
    service = ledger["sales"] / demand if demand else 0.0
    surplus = ledger["revenue"] - sum(ledger[k] for k in COST_KEYS)
    potential_revenue = PRICE_SELL * demand
    surplus_ratio = surplus / potential_revenue if demand else 0.0
    eligible = complete and demand > 0
    partial = eligible and service >= rubric.partial_service
    partial = partial and surplus_ratio >= rubric.partial_surplus_ratio
    full = eligible and service >= rubric.full_service
    full = full and surplus_ratio >= rubric.full_surplus_ratio
    total = 1.0 if full else rubric.partial_credit if partial else 0.0
    return Score(
        total=total,
        breakdown={
            "service": service,
            "operating_surplus": surplus,
            "surplus_ratio": surplus_ratio,
            "potential_revenue": potential_revenue,
            "demand": demand,
            "sales": ledger["sales"],
            "lost_units": ledger["lost"],
            "revenue": ledger["revenue"],
            "days_completed": float(env.day),
            "weeks_played": float(play.weeks),
            **{key: ledger[key] for key in COST_KEYS},
        },
        flags={"completed": complete, "full_credit": full, "partial_threshold_met": partial},
    )


class OutcomeControls(ControlsArgs):
    replenishment: Literal["ss", "base_stock", "fixed_lot", "fill_adjusted_base_stock"] | None = (
        Field(
            default=None,
            description="Standing daily replenishment formula. ss is the default. Base-stock "
            "targets the reorder point, fixed_lot orders one day of demand at that point, "
            "and fill_adjusted_base_stock discounts unacknowledged orders by supplier fill.",
        )
    )


class OutcomeTask(SupplyGymTask):
    """Shared version 2 behavior for all three SupplyGym worlds."""

    scoring_hash: ClassVar[str] = RUBRIC_HASH

    def audit_state(self) -> dict[str, Any] | None:
        """Trusted verifier access. This method is never exposed as an agent tool."""
        play = self._play
        if play is None:
            return None
        env: Any = play.env
        return {
            "seed": play.seed,
            "regime": play.regime,
            "day": env.day,
            "horizon": env.horizon,
            "decision_interval": env.k,
            "weeks": play.weeks,
            "finished": play.finished,
            "onhand": env.onhand,
            "orders": env.orders,
            "approved": env.approved,
            "controls": env.ctrl.tolist(),
            "replenishment": env.replenishment,
            "ledger": dict(env.tot),
        }

    def tools(self) -> list[ToolSpec]:
        overrides = {
            "finish": {"description": "End the episode. An unfinished year scores zero."},
            "set_controls": {"args_model": OutcomeControls},
        }
        return [tool.model_copy(update=overrides.get(tool.name, {})) for tool in super().tools()]

    def _validate_call(self, call: ToolCall) -> BaseModel | ToolResult:
        return validate_call(tool_map(self.tools()), call)

    async def step(self, call: ToolCall) -> ToolResult:
        result = await super().step(call)
        if call.name == "finish" and result.ok:
            return result.model_copy(
                update={"content": "Episode ended. An unfinished year scores zero."}
            )
        return result

    async def grade(self) -> Score:
        if self._play is None:
            raise ValueError("Episode not started")
        return grade_outcomes(self._play)

    def instruction(self) -> str:
        return (
            super()
            .instruction()
            .replace(
                "Stopping early, or never calling set_controls, leaves the remaining weeks "
                "on the default rule.",
                "Complete all 365 days through 53 successful weekly decisions. "
                "Stopping early or exhausting the budget with an unfinished year earns zero.",
            )
        )

    def _scoring_instruction(self) -> str:
        return (
            "Complete the year to earn credit. "
            f"Full credit requires service >= {RUBRIC.full_service:.0%} "
            f"and operating surplus / potential revenue >= {RUBRIC.full_surplus_ratio:.0%}. "
            f"Partial credit ({RUBRIC.partial_credit:g}) requires "
            f"service >= {RUBRIC.partial_service:.0%} "
            f"and surplus ratio >= {RUBRIC.partial_surplus_ratio:.0%}. All other outcomes earn 0. "
            "Service is units sold / demand. Operating surplus is sales revenue minus purchase, "
            "holding, lost-sale penalties, expedite and switching costs incurred during this year. "
            "Potential revenue is selling price times total demand. Opening stock and freight are "
            "prepaid. Terminal inventory earns no rubric credit. The verifier uses its own ledger."
        )


@register_task
class SupplyGymOutcomes(OutcomeTask):
    spec = SupplyGymTask.spec.model_copy(update={"version": 2})


@register_task
class SupplyGymFreshOutcomes(OutcomeTask, SupplyGymFreshTask):
    spec = SupplyGymFreshTask.spec.model_copy(update={"version": 2})
    regime = 1


class CalaSpectatorSnapshot(TypedDict):
    week: int
    end_day: int
    operating_surplus: float
    observation: dict[str, Any]


@register_task
class SupplyGymCalaOutcomes(OutcomeTask, SupplyGymCalaTask):
    spec = SupplyGymCalaTask.spec.model_copy(update={"version": 2})
    regime = 2
    scoring_hash = hashlib.sha256(
        json.dumps({"rubric": RUBRIC_HASH, "world": cala_world_identity()}, sort_keys=True).encode()
    ).hexdigest()

    def spectator_snapshot(self) -> CalaSpectatorSnapshot:
        """Read public observations and unrounded accounting for the trusted host."""
        play = self._play
        if play is None:
            raise ValueError("Episode not started")
        return {
            "week": play.weeks,
            "end_day": play.env.day,
            "operating_surplus": grade_outcomes(play).breakdown["operating_surplus"],
            "observation": play.status(),
        }


@register_task
class SupplyGymCalaUpperBound(SupplyGymCalaOutcomes):
    """Cala controls with a fixed, scenario-specific optimistic surplus denominator."""

    spec = SupplyGymCalaTask.spec.model_copy(
        update={"version": 3, "max_steps": CALA_V3_MAX_STEPS}
    )
    scoring_hash = hashlib.sha256(
        json.dumps(
            {"method": "cala-surplus-upper-bound-v1", "world": cala_world_identity()},
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def tools(self) -> list[ToolSpec]:
        return [tool for tool in super().tools() if tool.name != "finish"]

    def instruction(self) -> str:
        return super().instruction().replace(", finish.", ".")

    async def reset(self, seed: int, difficulty: int = 0, *, salt: str | None = None):
        from crucible.envs.supplygym.cala_upper_bound import surplus_upper_bound

        observation = await super().reset(seed, difficulty, salt=salt)
        assert self._play is not None
        self._upper_bound_cents = surplus_upper_bound(self._play)
        return observation

    async def grade(self) -> Score:
        if self._play is None:
            raise ValueError("Episode not started")
        accounting = grade_outcomes(self._play)
        bound = self._upper_bound_cents / 100
        total = 0.0
        if accounting.flags["completed"] and bound > 0:
            total = max(0.0, min(1.0, accounting.breakdown["operating_surplus"] / bound))
        return Score(
            total=total,
            breakdown={**accounting.breakdown, "surplus_upper_bound": bound},
            flags={"completed": accounting.flags["completed"]},
        )

    def _scoring_instruction(self) -> str:
        return (
            "Complete all 365 days to earn credit; an unfinished year scores zero. "
            "Your score is operating surplus divided by a positive theoretical upper bound "
            "for this year, clipped to [0, 1]. The bound is calculated before your first "
            "action from the scenario's hidden demand and disruptions, allowing optimistic "
            "purchases and shipments. Operating surplus is sales revenue minus purchase, "
            "holding, lost-sale penalties, expedite and switching costs incurred during "
            "the year. Opening stock and freight are prepaid; terminal inventory receives "
            "no credit. The verifier uses its own ledger."
        )

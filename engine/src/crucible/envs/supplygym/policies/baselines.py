"""Policies for `supplygym@1` and `supplygym-fresh@1` that play through the tools only.

`ss` plays the default rule and so scores exactly 0; `stop_early` stops after a few weeks; the
`*_every_week` policies are crude strategies that must not beat honest play.
"""

import json
from collections.abc import Callable
from typing import Any

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec
from crucible.envs.supplygym.policies.reference import CALA_REFERENCE, Decider, make_decider
from crucible.envs.supplygym.policies.views import call, parse_status

SUPPLIERS = [f"S{k:02d}" for k in range(1, 11)]
# The legacy regimes retain their checkpoint; Cala has its own frozen reference.
TASK_IDS = ("supplygym", "supplygym-fresh", "supplygym-cala")


def _register[T](name: str) -> Callable[[T], T]:
    """Register a policy for every supplygym task id."""

    def decorator(cls: T) -> T:
        for task_id in TASK_IDS:
            register_policy(name, task_id)(cls)  # pyright: ignore[reportArgumentType]
        return cls

    return decorator


class _Loop:
    """Reads the status once, then plays `decide(status)` until the episode is over."""

    name = "supplygym"
    weeks_limit: int | None = None

    def __init__(self) -> None:
        self._weeks = 0
        self._started = False
        self._decider: Decider | None = None
        self._reference = "ppo"
        self._has_finish = True

    def _make(self) -> Decider:
        raise NotImplementedError

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._reference = CALA_REFERENCE if task_spec.id == "supplygym-cala" else "ppo"
        if task_spec.version >= 2:
            self._reference = "availability"
        self._has_finish = any(t.name == "finish" for t in tools)
        self._weeks, self._started, self._decider = 0, False, self._make()

    async def act(self, obs: Observation) -> ToolCall | None:
        if not self._started:
            self._started = True
            return call("get_status")
        r = obs.last_result
        if r is None or not r.ok:
            return None  # a refused call ends the policy; the grader then plays the default rule
        if isinstance(r.content, dict) and r.content.get("episode_over"):
            return None
        if self.weeks_limit is not None and self._weeks >= self.weeks_limit:
            return call("finish") if self._has_finish else None
        assert self._decider is not None
        status = json.loads(json.dumps(parse_status(r).model_dump(mode="json")))
        self._weeks += 1
        return call("set_controls", **self._decider.decide(status))


@_register("ss")
class DefaultEveryWeek(_Loop):
    """Calls set_controls with no changes every week."""

    name = "ss"

    def _make(self) -> Decider:
        return make_decider("ss")


@_register("stop_early")
class StopEarly(_Loop):
    """Plays the reference for 4 weeks, then calls finish."""

    name = "stop_early"
    weeks_limit = 4

    def _make(self) -> Decider:
        return make_decider(self._reference)


class _Fixed:
    def __init__(self, args: dict[str, Any]) -> None:
        self.args = args

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        return dict(self.args)


class _ReplenishmentFormula(_Loop):
    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        if task_spec.version < 2:
            raise ValueError("Replenishment formulas require a SupplyGym version 2 task")
        await super().reset(task_spec, tools, instruction)

    def _make(self) -> Decider:
        return _Fixed({"replenishment": self.name})


@_register("base_stock")
class BaseStock(_ReplenishmentFormula):
    name = "base_stock"


@_register("fixed_lot")
class FixedLot(_ReplenishmentFormula):
    name = "fixed_lot"


@_register("fill_adjusted_base_stock")
class FillAdjustedBaseStock(_ReplenishmentFormula):
    name = "fill_adjusted_base_stock"


@_register("max_expedite")
class MaxExpedite(_Loop):
    name = "max_expedite"

    def _make(self) -> Decider:
        return _Fixed({"expedite_fraction": 0.3, "safety_stock_days": 3.0})


@_register("reference")
class Reference(_Loop):
    """The reference (PPO) policy played over the tools."""

    name = "reference"

    def _make(self) -> Decider:
        return make_decider(self._reference)


@register_policy("transfer_ppo", "supplygym-cala")
class TransferPpo(_Loop):
    """The original checkpoint evaluated on the same Cala tape for transfer comparison."""

    name = "transfer_ppo"

    def _make(self) -> Decider:
        return make_decider("ppo")


@register_policy("availability", "supplygym-cala")
class Availability(_Loop):
    """Diagnostic reallocation using acknowledged fill and the public risk score."""

    name = "availability"

    def _make(self) -> Decider:
        return make_decider("availability")

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        return make_decider("availability").decide(status)

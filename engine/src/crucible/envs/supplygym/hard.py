"""Opt-in evaluation registration, separate from the environment entry point."""

from functools import partial
from typing import ClassVar

from crucible.core.registry import (
    get_policy,
    list_policies,
    list_tasks,
    register_policy,
    register_task,
)
from crucible.core.task import TaskSpec
from crucible.envs.supplygym.task import SupplyGymFreshTask, SupplyGymTask


class HardTask(SupplyGymTask):
    hard: ClassVar[bool] = True
    spec: ClassVar[TaskSpec] = SupplyGymTask.spec.model_copy(
        update={"id": "supplygym-hard", "title": "SupplyGym: hard evaluation"}
    )

    def instruction(self) -> str:
        return super().instruction() + (
            "\n\nHard evaluation: disruptions may overlap. Weekly risk scores use health "
            "from 7 or 14 days earlier (plus the weekly reporting delay) and may be stale. "
            "Extra false headlines appear during that delay. Cross-check risk against "
            "late orders and fill ratios, which retain their normal reporting times."
        )


class HardFreshTask(HardTask):
    regime: ClassVar[int] = 1
    spec: ClassVar[TaskSpec] = SupplyGymFreshTask.spec.model_copy(
        update={"id": "supplygym-fresh-hard", "title": "SupplyGym: fresh hard evaluation"}
    )


def enable_hard_tier(policies: list[str]) -> None:
    """Enable distinct eval task IDs and reuse the selected policies without retraining.

    Call explicitly in an evaluation process, including an LLM evaluation harness.
    Hard task resets reject seeds below 30000, including training and dev banks.
    """
    for cls, base in ((HardTask, "supplygym@1"), (HardFreshTask, "supplygym-fresh@1")):
        if cls.spec.ref not in list_tasks():
            register_task(cls)
        for name in policies:
            if name not in list_policies(cls.spec.ref):
                register_policy(name, cls.spec.id)(partial(get_policy, name, base))

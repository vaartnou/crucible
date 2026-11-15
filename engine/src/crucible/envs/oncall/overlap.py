"""Overlapping release regressions, using the same physical model and public evidence."""

import random
from dataclasses import replace
from typing import ClassVar

from crucible.core.registry import register_task
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolSpec
from crucible.envs.oncall.model import State, solve
from crucible.envs.oncall.task import TOOLS, IncidentResolveArgs, OncallTask
from crucible.envs.oncall.world import Build, Incident, World, make_world


def make_overlap_world(seed: int, salt: str | None = None, difficulty: int = 0) -> World:
    world = make_world(seed, salt, difficulty)
    rng = random.Random(f"overlap-v1:{world.fingerprint}")
    # A different owner's recent release develops a regression while the first incident
    # is being investigated. Its public deployment history remains an ordinary version diff.
    candidates = [
        change
        for change in world.changes
        if change.kind == "deploy"
        and -2 <= change.minute <= 2
        and change.service not in (world.culprit, "edge-gateway", "session-cache")
    ]
    change = rng.choice(candidates)
    fault = "partial_rollout" if world.fault == "memory_leak" else "memory_leak"
    build = Build(
        change.service,
        int(change.after["version"]),
        bad_pods=1,
        pod_error=0.6 if fault == "partial_rollout" else 0,
        leak_mib_min=44 if fault == "memory_leak" else 0,
    )
    return replace(
        world,
        builds=(*world.builds, build),
        incidents=(
            Incident(world.culprit, world.fault, world.change_id),
            Incident(change.service, fault, change.id),
        ),
    )


@register_task
class OverlapTask(OncallTask):
    spec: ClassVar[TaskSpec] = TaskSpec(
        id="oncall",
        version=2,
        max_steps=80,
        title="On-call gym: overlapping incidents",
        description="Restore service through overlapping release regressions and verify recovery.",
        keywords=("on-call", "sre", "overlapping-incidents"),
    )

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        self._world = make_overlap_world(seed, salt, difficulty)
        self._state = State()
        for minute in range(-30, 6):
            pods, edges = solve(self._world, self._state, minute)
            self._state.history.append(pods)
            self._state.edge_history.append(edges)
        return Observation(text=self.instruction(), data={"minute": 5})

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="resolve",
                description="Close with diagnoses. Recovery is tested for thirty minutes.",
                kind="end",
                args_model=IncidentResolveArgs,
            )
            if tool.name == "resolve"
            else tool
            for tool in TOOLS
        ]

    def instruction(self) -> str:
        return (
            super()
            .instruction()
            .replace(
                "Close with resolve(report={service, fault_kind, change_id, remediation}).",
                "Overlapping regressions may affect this system. Inspect residual impact after "
                "each repair and verify sustained recovery across the system. Close with "
                "resolve(reports=[{service, fault_kind, change_id, remediation}, ...]). "
                "Report each independent cause once; duplicate and speculative reports reduce "
                "diagnosis credit.",
            )
        )

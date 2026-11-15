"""Public-tool reference for the external backend infrastructure proof."""

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec


@register_policy("reference", "backend_probe")
class Reference:
    name = "reference"

    def __init__(self) -> None:
        self._index = 0
        self._calls = [
            ToolCall(name="dispatch"),
            ToolCall(name="advance", args={"minutes": 10}),
            ToolCall(name="inspect"),
            ToolCall(name="finish"),
        ]

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._index = 0

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is not None and not obs.last_result.ok:
            return None
        if self._index >= len(self._calls):
            return None
        call = self._calls[self._index]
        self._index += 1
        return call

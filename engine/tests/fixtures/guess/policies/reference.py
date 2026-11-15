"""Binary-search reference for `guess`. Uses only tool results, never task internals."""

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec


@register_policy("reference", "guess")
class BinarySearch:
    """Halves the range on every answer. Solves 1..100 in at most 7 guesses."""

    name = "reference"

    def __init__(self) -> None:
        self._low = 1
        self._high = 100
        self._last = 0

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._low, self._high, self._last = 1, 100, 0

    async def act(self, obs: Observation) -> ToolCall | None:
        result = obs.last_result
        if result is not None and result.ok:
            if result.content == "higher":
                self._low = self._last + 1
            elif result.content == "lower":
                self._high = self._last - 1
        self._last = (self._low + self._high) // 2
        return ToolCall(name="guess", args={"n": self._last})


__all__ = ["BinarySearch"]

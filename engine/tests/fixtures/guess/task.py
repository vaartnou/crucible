"""`guess@1`: find a hidden number in 1..100 with higher/lower feedback."""

from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, Field

from crucible.core.registry import register_task
from crucible.core.rng import RngStreams
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskSpec
from crucible.core.tools import (
    Observation,
    ToolCall,
    ToolResult,
    ToolSpec,
    tool_map,
    validate_call,
)

LOW, HIGH = 1, 100


class GuessArgs(BaseModel):
    """Arguments of the `guess` tool."""

    n: int = Field(ge=LOW, le=HIGH, description="Your guess, from 1 to 100.")


class NoArgs(BaseModel):
    """Arguments of a tool that takes none."""


_TOOLS = [
    ToolSpec(
        name="guess",
        description="Guess the hidden number. Returns 'higher', 'lower' or 'correct'.",
        kind="action",
        args_model=GuessArgs,
    ),
    ToolSpec(
        name="give_up",
        description="End the episode without solving it.",
        kind="end",
        args_model=NoArgs,
    ),
]


@dataclass(frozen=True)
class _Hidden:
    """Private world state. Never put in a `ToolResult` or `Observation`."""

    target: int


@register_task
class GuessNumber(Task):
    """Guess a hidden number; fewer guesses score higher."""

    spec: ClassVar[TaskSpec] = TaskSpec(
        id="guess",
        version=1,
        max_steps=10,
        title="Guess the number",
        description=(
            "Find a hidden whole number from 1 to 100 with higher/lower feedback. "
            "Fewer guesses score higher."
        ),
        keywords=("number-guessing", "search"),
    )

    def __init__(self) -> None:
        self._hidden: _Hidden | None = None
        self._guesses = 0
        self._solved = False
        self._gave_up = False

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        self._hidden = _Hidden(target=RngStreams(seed, salt).exogenous.randint(LOW, HIGH))
        self._guesses = 0
        self._solved = False
        self._gave_up = False
        return Observation(text=self.instruction())

    def tools(self) -> list[ToolSpec]:
        return list(_TOOLS)

    async def step(self, call: ToolCall) -> ToolResult:
        if self._hidden is None:
            return ToolResult(ok=False, error="Episode not started: call reset first")
        if self.done():
            return ToolResult(ok=False, error="Episode is over")
        parsed = validate_call(tool_map(_TOOLS), call)
        if isinstance(parsed, ToolResult):
            return parsed
        if isinstance(parsed, GuessArgs):
            self._guesses += 1
            if parsed.n == self._hidden.target:
                self._solved = True
                return ToolResult(ok=True, content="correct")
            return ToolResult(
                ok=True, content="higher" if parsed.n < self._hidden.target else "lower"
            )
        self._gave_up = True
        return ToolResult(ok=True, content="gave up")

    def done(self) -> bool:
        return self._solved or self._gave_up

    def hidden_strings(self) -> frozenset[str]:
        return frozenset() if self._hidden is None else frozenset({str(self._hidden.target)})

    def solved(self, score: Score) -> bool:
        return score.flags.get("solved", False)

    async def grade(self) -> Score:
        total = 1 - (self._guesses - 1) / self.spec.max_steps if self._solved else 0.0
        return Score(
            total=total,
            breakdown={"guesses": float(self._guesses)},
            flags={"solved": self._solved},
        )

    def instruction(self) -> str:
        return (
            f"Guess the hidden whole number from {LOW} to {HIGH} with the `guess` tool. "
            "Fewer guesses score higher. Call `give_up` to stop."
        )

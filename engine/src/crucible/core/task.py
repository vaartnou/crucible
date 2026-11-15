"""Task definition: the spec and the abstract environment."""

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from crucible.core.scoring import Score
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec


class TaskSpec(BaseModel):
    """Static identity of a task, plus the metadata exports show (title, description, keywords)."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: int
    max_steps: int
    title: str = Field(min_length=1, pattern=r"^[^\r\n]+$")
    description: str = Field(min_length=1)
    keywords: tuple[str, ...] = ()
    # Harbor resources. `None` keeps the export's defaults.
    cpus: int | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    timeout_sec: float | None = Field(default=None, gt=0)
    docker_image: str | None = Field(default=None, min_length=1, pattern=r"^[^\s\"\\]+$")

    @field_validator("keywords")
    @classmethod
    def _keywords_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not k.strip() for k in value):
            raise ValueError("keywords must not be blank")
        return value

    @property
    def ref(self) -> str:
        """Versioned reference, `"id@version"`."""
        return f"{self.id}@{self.version}"


class TaskChecks(BaseModel):
    """Which generic checks (`tests/generic/`) run for a task, and how.

    Every check is on by default; turn one off only when it cannot apply to the task.
    """

    model_config = ConfigDict(frozen=True)

    seeds: tuple[int, ...] = tuple(range(20))
    exploit_policies: tuple[str, ...] = ()
    determinism: bool = True
    do_nothing_zero: bool = True
    reward_bounds: bool = True
    no_hidden_state: bool = True
    reference_solves: bool = True
    exploits_below_reference: bool = True
    oracle_perfect: bool = True


class Task(ABC):
    """One environment. A fresh instance is made per episode."""

    spec: ClassVar[TaskSpec]
    scoring_hash: ClassVar[str] = "none"
    checks: ClassVar[TaskChecks] = TaskChecks()

    @abstractmethod
    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        """Start an episode and return the first observation. `salt` mixes into the world RNG."""

    @abstractmethod
    def tools(self) -> list[ToolSpec]:
        """Tools a policy may call."""

    @abstractmethod
    async def step(self, call: ToolCall) -> ToolResult:
        """Run one tool call."""

    @abstractmethod
    def done(self) -> bool:
        """True when the episode is over."""

    @abstractmethod
    async def grade(self) -> Score:
        """Score the finished episode."""

    @abstractmethod
    def instruction(self) -> str:
        """Instruction text shown to the policy."""

    async def aclose(self) -> None:
        """Release resources owned by this episode, including partially initialized resources.

        Implementations must tolerate repeated calls and must not stop shared services.
        Grading applies any published continuation rules before this hook runs.
        """
        return None

    def grading_evidence(self) -> dict[str, JsonValue]:
        """Frozen private evidence available only to the grading harness after grade()."""
        return {}

    def hidden_strings(self) -> frozenset[str]:
        """Words from the hidden state that no successful tool result may contain. After reset.

        The generic no-hidden-state check matches them as whole words.
        """
        return frozenset()

    def solved(self, score: Score) -> bool:
        """True when a finished episode counts as solved. Override for a stricter rule."""
        return score.total > 0


__all__ = ["Task", "TaskChecks", "TaskSpec"]

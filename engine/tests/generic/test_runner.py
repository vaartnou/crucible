"""The generic checks' own helpers catch what they are meant to catch."""

import asyncio
from pathlib import Path
from typing import ClassVar

from crucible.core import registry
from crucible.core.registry import register_task
from crucible.core.scoring import Score
from crucible.core.task import TaskSpec
from crucible.core.tools import ToolCall, ToolResult
from tests.core.helpers import ScriptedPolicy
from tests.fixtures.guess.task import GuessNumber
from tests.generic.runner import Fuzzer, comparable, leaks, run


class LeakyGuess(GuessNumber):
    """Says the target in every answer."""

    spec: ClassVar[TaskSpec] = GuessNumber.spec.model_copy(update={"id": "leaky"})

    async def step(self, call: ToolCall) -> ToolResult:
        result = await super().step(call)
        if not result.ok or self._hidden is None:
            return result
        return ToolResult(ok=True, content={"answer": result.content, "t": self._hidden.target})


def test_leak_is_found_unless_the_call_said_it(tmp_path: Path) -> None:
    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        register_task(LeakyGuess)
        episode = asyncio.run(
            run(
                "leaky@1",
                ScriptedPolicy([ToolCall(name="guess", args={"n": 1})]),
                0,
                tmp_path / "a",
            )
        )
    (target,) = episode.hidden
    assert leaks(episode) == ([] if target == "1" else [(0, target)])


def test_honest_guess_does_not_leak(tmp_path: Path) -> None:
    episode = asyncio.run(run("guess@1", Fuzzer(3), 3, tmp_path / "g.jsonl"))
    assert episode.hidden and not leaks(episode)
    assert isinstance(episode.result.score, Score)


def test_fuzzer_is_seeded(tmp_path: Path) -> None:
    a = asyncio.run(run("guess@1", Fuzzer(5), 5, tmp_path / "a.jsonl"))
    b = asyncio.run(run("guess@1", Fuzzer(5), 5, tmp_path / "b.jsonl"))
    c = asyncio.run(run("guess@1", Fuzzer(6), 5, tmp_path / "c.jsonl"))
    assert comparable(a.records) == comparable(b.records)
    assert comparable(a.records) != comparable(c.records)
    assert a.result.steps > 0

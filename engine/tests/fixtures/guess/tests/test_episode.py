from pathlib import Path

import pytest

from crucible.core.registry import get_policy, get_task
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec
from crucible.core.transcript import read_transcript
from crucible.testing import ScriptedPolicy, play


def guess_call(n: int) -> ToolCall:
    return ToolCall(name="guess", args={"n": n})


async def test_reference_solves_seeds_0_to_49(tmp_path: Path) -> None:
    for seed in range(50):
        policy = get_policy("reference", "guess@1")
        result = await play(policy, seed, tmp_path / f"{seed}.jsonl")
        assert result.score.flags["solved"] is True, seed
        assert result.score.flags["budget_exhausted"] is False
        assert result.stop_reason == "task_done"
        assert result.steps <= 7
        assert result.score.total == pytest.approx(1 - (result.steps - 1) / 10)
        assert result.score.breakdown == {"guesses": float(result.steps)}


async def test_do_nothing_scores_zero(tmp_path: Path) -> None:
    result = await play(get_policy("do_nothing", "guess@1"), 0, tmp_path / "a.jsonl")
    assert result.score.total == 0
    assert result.score.flags == {"solved": False, "budget_exhausted": False}
    assert result.stop_reason == "policy_stop"
    assert result.steps == 0


async def test_budget_exhausted_sets_flag(tmp_path: Path) -> None:
    policy = ScriptedPolicy([guess_call(0)] * 20)  # invalid every time, so never solved
    result = await play(policy, 0, tmp_path / "a.jsonl")
    assert result.stop_reason == "budget_exhausted"
    assert result.steps == 10
    assert result.score.flags["budget_exhausted"] is True
    assert result.score.total == 0


async def test_solved_on_last_step_is_task_done(tmp_path: Path) -> None:
    from crucible.core.rng import RngStreams

    target = RngStreams(4).exogenous.randint(1, 100)
    wrong = 1 if target != 1 else 2
    calls = [guess_call(wrong)] * 9 + [guess_call(target)]
    result = await play(ScriptedPolicy(calls), 4, tmp_path / "a.jsonl")
    assert result.stop_reason == "task_done"
    assert result.score.flags["budget_exhausted"] is False
    assert result.score.total == pytest.approx(0.1)


async def test_bad_tool_call_does_not_change_state() -> None:
    task = get_task("guess@1")
    await task.reset(0)
    for bad in [
        ToolCall(name="guess", args={"n": 0}),
        ToolCall(name="guess", args={"n": 101}),
        ToolCall(name="guess", args={"n": "x"}),
        ToolCall(name="guess", args={}),
        ToolCall(name="nope"),
    ]:
        result = await task.step(bad)
        assert result.ok is False
        assert result.error
    assert not task.done()
    assert (await task.grade()).breakdown == {"guesses": 0.0}
    assert (await task.step(guess_call(50))).ok is True
    assert (await task.grade()).breakdown == {"guesses": 1.0}


async def test_step_after_done_is_an_error() -> None:
    task = get_task("guess@1")
    await task.reset(0)
    assert (await task.step(ToolCall(name="give_up"))).ok is True
    assert task.done()
    result = await task.step(guess_call(50))
    assert result.ok is False
    assert (await task.grade()).total == 0


async def test_give_up_ends_episode(tmp_path: Path) -> None:
    policy = ScriptedPolicy([ToolCall(name="give_up")])
    result = await play(policy, 0, tmp_path / "a.jsonl")
    assert result.stop_reason == "task_done"
    assert result.score.total == 0
    assert result.steps == 1


async def test_policy_exception_is_env_error(tmp_path: Path) -> None:
    class Boom:
        name = "boom"

        async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
            return None

        async def act(self, obs: Observation) -> ToolCall | None:
            raise RuntimeError("policy blew up")

    path = tmp_path / "a.jsonl"
    result = await play(Boom(), 0, path)
    assert result.stop_reason == "error"
    assert result.score.total == 0
    assert result.score.flags["env_error"] is True
    transcript = read_transcript(path)
    assert transcript.footer.stop_reason == "error"
    assert transcript.footer.error == "RuntimeError"
    assert not path.with_name("a.jsonl.tmp").exists()

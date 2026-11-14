"""Public-tool operators played against the in-memory task: ladder, exploits and budget."""

import statistics
from pathlib import Path

import pytest

from crucible.core.episode import run_episode
from crucible.core.jobs import Job
from crucible.core.policy import Policy
from crucible.core.registry import get_policy
from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.core.transcript import TranscriptWriter
from crucible.envs.fleetbase_dispatch.policies import (
    EXPLOITS,
    GreedyOperator,
    ReferenceOperator,
    StopEarlyOperator,
)
from crucible.envs.fleetbase_dispatch.policies.reference import RESERVE
from crucible.envs.fleetbase_dispatch.task import MAX_STEPS, FleetbaseSim
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World

PUBLIC = {
    "list_events",
    "list_orders",
    "list_vehicles",
    "create_order",
    "assign_order",
    "unassign_order",
    "dispatch_order",
    "set_vehicle_route",
    "advance",
    "finish",
}
SEEDS = range(10)


class WorldSim(FleetbaseSim):
    def __init__(self, world: World) -> None:
        super().__init__()
        self._world = world

    def _make_world(self, seed: int, salt: str | None) -> World:
        return self._world


async def play(task: FleetbaseSim, policy: Policy, seed: int = 0) -> list[tuple[ToolCall, bool]]:
    observation = await task.reset(seed)
    await policy.reset(task.spec, task.tools(), task.instruction())
    calls: list[tuple[ToolCall, bool]] = []
    for _ in range(task.spec.max_steps):
        if task.done():
            break
        call = await policy.act(observation)
        if call is None:
            break
        result = await task.step(call)
        calls.append((call, result.ok))
        observation = Observation(last_result=result)
    return calls


async def reward(name: str, seed: int) -> float:
    task = FleetbaseSim()
    await play(task, get_policy(name, "fleetbase_sim@1"), seed)
    return (await task.grade()).total


@pytest.mark.parametrize("seed", SEEDS)
async def test_reference_completes_within_budget_without_rejections(seed: int) -> None:
    task = FleetbaseSim()
    calls = await play(task, ReferenceOperator(), seed)
    assert task.done()
    assert len(calls) <= task.spec.max_steps - RESERVE
    assert all(ok for _, ok in calls)
    assert {call.name for call, _ in calls} <= PUBLIC
    score = await task.grade()
    assert 0.5 < score.total < 1 and score.breakdown["deliveries"] > 0


async def test_budget_is_about_60_percent_of_the_reference_use() -> None:
    used: list[int] = []
    for seed in SEEDS:
        calls = await play(FleetbaseSim(), ReferenceOperator(), seed)
        used.append(len(calls))
    share = statistics.fmean(used) / MAX_STEPS
    assert 0.5 <= share <= 0.7, share
    assert max(used) < MAX_STEPS - RESERVE


class SmallBudgetSim(FleetbaseSim):
    spec = FleetbaseSim.spec.model_copy(update={"max_steps": 60})


async def test_reference_respects_a_small_budget() -> None:
    task = SmallBudgetSim()
    calls = await play(task, ReferenceOperator(), 3)
    # With the reserve left it advances to the horizon instead of running out.
    assert len(calls) < 60 and task.done()
    assert calls[-1][0].name in {"advance", "finish"}
    assert all(ok for _, ok in calls)
    assert (await task.grade()).total > 0


async def test_replay_reproduces_calls_and_score() -> None:
    a, b = FleetbaseSim(), FleetbaseSim()
    calls_a = await play(a, ReferenceOperator(), 7)
    calls_b = await play(b, ReferenceOperator(), 7)
    assert calls_a == calls_b
    assert await a.grade() == await b.grade()


async def test_greedy_takes_the_nearest_order_and_reference_the_most_profitable() -> None:
    world = World(
        (
            Request("near", (1, 0), (2, 0), 1, 0, 50, 5),
            Request("far", (5, 0), (6, 0), 1, 0, 7, 100),
        ),
        (Vehicle("v", "d", 1, (0, 0), 0.2, 100),),
        horizon=60,
        area=10,
    )

    def created(calls: list[tuple[ToolCall, bool]]) -> list[object]:
        return [c.args["request_id"] for c, _ in calls if c.name == "create_order"]

    reference_task, greedy_task = WorldSim(world), WorldSim(world)
    reference_calls = await play(reference_task, ReferenceOperator())
    assert created(await play(greedy_task, GreedyOperator()))[0] == "near"
    assert "far" in created(reference_calls)
    reference_score = await reference_task.grade()
    greedy_score = await greedy_task.grade()
    assert reference_score.breakdown["deliveries"] >= 1
    assert reference_score.total > greedy_score.total


async def test_a_rejected_workflow_call_drops_that_order() -> None:
    world = World(
        (Request("r1", (1, 0), (2, 0), 1, 0, 50, 20),),
        (Vehicle("v", "d", 2, (0, 0), 0.2, 100),),
        horizon=60,
        area=10,
    )
    task = WorldSim(world)
    policy = ReferenceOperator()
    observation = await task.reset(0)
    await policy.reset(task.spec, task.tools(), task.instruction())
    call = await policy.act(observation)
    while call is not None and call.name != "create_order":
        observation = Observation(last_result=await task.step(call))
        call = await policy.act(observation)
    assert call is not None and call.args == {"request_id": "r1"}
    # The tool reported a failure: the operator does not continue with this order.
    rejected = Observation(last_result=ToolResult(ok=False, error="timeout"))
    following = await policy.act(rejected)
    assert following is not None and following.args.get("request_id") != "r1"


async def test_stop_early_dispatches_one_order_then_finishes() -> None:
    task = FleetbaseSim()
    calls = await play(task, StopEarlyOperator())
    names = [call.name for call, _ in calls]
    assert names.count("dispatch_order") == 1 and names[-1] == "finish"
    assert names[names.index("dispatch_order") + 1] == "finish"
    score = await task.grade()
    assert score.breakdown["deliveries"] + score.breakdown["late"] == 1


async def test_ladder_order_on_dev_seeds() -> None:
    means: dict[str, float] = {}
    for name in ("fifo", "greedy_no_replan", "greedy_nearest", "reference"):
        means[name] = statistics.fmean([await reward(name, seed) for seed in SEEDS])
    assert means["fifo"] < means["greedy_nearest"]
    assert means["greedy_no_replan"] < means["greedy_nearest"]
    assert means["reference"] - means["greedy_nearest"] >= 0.25


async def test_greedy_alias_is_greedy_nearest() -> None:
    assert await reward("greedy", 2) == await reward("greedy_nearest", 2)


@pytest.mark.parametrize("name", EXPLOITS)
async def test_exploit_runs_cleanly_and_scores_below_greedy(name: str, tmp_path: Path) -> None:
    for seed in range(3):
        task = FleetbaseSim()
        job = Job(task="fleetbase_sim@1", policy=name, seed=seed)
        writer = TranscriptWriter(tmp_path / f"{name}-{seed}.jsonl")
        result = await run_episode(
            task, get_policy(name, "fleetbase_sim@1"), seed, job=job, writer=writer
        )
        assert result.stop_reason != "error", result
        assert result.score.total < await reward("greedy_nearest", seed), (name, seed)


async def test_hindsight_is_rarely_beaten_by_the_reference() -> None:
    beaten = 0
    for seed in range(20):
        task = FleetbaseSim()
        await play(task, ReferenceOperator(), seed)
        score = await task.grade()
        assert score.breakdown["u_hindsight"] > 0
        beaten += score.breakdown["utility"] >= score.breakdown["u_hindsight"]
    # 0 of 50 dev seeds in the frozen sweep; allow one in twenty here.
    assert beaten <= 1

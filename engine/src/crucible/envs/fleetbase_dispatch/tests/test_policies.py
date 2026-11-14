"""Public-tool operators played against the in-memory task."""

import pytest

from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.envs.fleetbase_dispatch.policies import (
    GreedyOperator,
    Operator,
    ReferenceOperator,
    StopEarlyOperator,
)
from crucible.envs.fleetbase_dispatch.task import FleetbaseSim
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World

PUBLIC = {
    "list_orders",
    "list_vehicles",
    "create_order",
    "assign_order",
    "dispatch_order",
    "advance",
    "finish",
}


class WorldSim(FleetbaseSim):
    def __init__(self, world: World) -> None:
        super().__init__()
        self._world = world

    def _make_world(self, seed: int, salt: str | None) -> World:
        return self._world


async def play(task: FleetbaseSim, policy: Operator, seed: int = 0) -> list[tuple[ToolCall, bool]]:
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


@pytest.mark.parametrize("seed", range(10))
async def test_reference_completes_within_budget_without_rejections(seed: int) -> None:
    task = FleetbaseSim()
    calls = await play(task, ReferenceOperator(), seed)
    assert task.done()
    assert len(calls) < task.spec.max_steps
    assert all(ok for _, ok in calls)
    assert {call.name for call, _ in calls} <= PUBLIC
    score = await task.grade()
    assert score.total > 0 and score.breakdown["deliveries"] > 0


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
        (Vehicle("v", "d", 2, (0, 0), 0.2, 100),),
        horizon=60,
        area=10,
    )

    def first_create(calls: list[tuple[ToolCall, bool]]) -> object:
        return next(c.args["request_id"] for c, _ in calls if c.name == "create_order")

    reference_task, greedy_task = WorldSim(world), WorldSim(world)
    assert first_create(await play(reference_task, ReferenceOperator())) == "far"
    assert first_create(await play(greedy_task, GreedyOperator())) == "near"
    assert (await reference_task.grade()).total > (await greedy_task.grade()).total


async def test_stop_early_dispatches_one_order_then_finishes() -> None:
    task = FleetbaseSim()
    calls = await play(task, StopEarlyOperator())
    names = [call.name for call, _ in calls]
    assert names.count("dispatch_order") == 1 and names[-1] == "finish"
    assert names[names.index("dispatch_order") + 1] == "finish"
    score = await task.grade()
    assert score.breakdown["deliveries"] + score.breakdown["late"] == 1


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
    assert following is not None and following.name == "advance"

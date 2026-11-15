"""Public-tool policy checks using a workflow fixture, not an application proof."""

from collections.abc import Mapping

import pytest
from pydantic import JsonValue

from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.envs.fleetbase_dispatch.policies import (
    GreedyOperator,
    ReferenceOperator,
    StopEarlyOperator,
)
from crucible.envs.fleetbase_dispatch.sim import WorldSimulator
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World, make_world

SPEC = TaskSpec(id="fleetbase_dispatch", version=1, max_steps=160, title="Test", description="Test")


class PublicWorkflow:
    """Adapter-free policy fixture with only public call results."""

    def __init__(self, world: World) -> None:
        self.sim = WorldSimulator(world)
        self.created: set[str] = set()
        self.committed: dict[str, dict[str, JsonValue]] = {}
        self.calls: list[ToolCall] = []
        self.finished = False

    def observation(self) -> dict[str, JsonValue]:
        data = self.sim.observation()
        requests = data["requests"]
        assert isinstance(requests, list)
        for item in requests:
            assert isinstance(item, dict)
            if item["id"] in self.created:
                item["native_id"] = f"order_{item['id']}"
        return data

    def step(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        request_id = call.args.get("request_id")
        if call.name == "create_order":
            assert isinstance(request_id, str)
            self.created.add(request_id)
        elif call.name == "assign_order":
            assert isinstance(request_id, str) and request_id in self.created
            self.committed[request_id] = {
                "vehicle_id": call.args["vehicle_id"],
                "dispatched": False,
            }
        elif call.name == "dispatch_order":
            assert isinstance(request_id, str) and request_id in self.committed
            self.committed[request_id]["dispatched"] = True
        elif call.name == "advance":
            minutes = call.args["minutes"]
            assert isinstance(minutes, (int, float))
            self.sim.advance(minutes)
        elif call.name == "finish":
            self.sim.finish()
            self.finished = True
        else:
            assert call.name == "observe"
        snapshot: Mapping[str, Mapping[str, JsonValue]] = self.committed
        self.sim.reconcile(snapshot)
        return ToolResult(ok=True, content=self.observation())


async def play(
    world: World, policy: ReferenceOperator | GreedyOperator | StopEarlyOperator
) -> PublicWorkflow:
    fixture = PublicWorkflow(world)
    await policy.reset(SPEC, [], "public instructions")
    observation = Observation(data=fixture.observation())
    for _ in range(SPEC.max_steps):
        call = await policy.act(observation)
        if call is None:
            break
        result = fixture.step(call)
        observation = Observation(last_result=result)
        if fixture.finished:
            break
    assert fixture.finished, fixture.calls
    return fixture


@pytest.mark.parametrize("seed", range(10))
async def test_reference_reliable_complete_workflow(seed: int) -> None:
    result = await play(make_world(seed), ReferenceOperator())
    assert result.sim.economics()["reward"] > 0.5
    assert len(result.calls) <= 100
    assert set(call.name for call in result.calls) <= {
        "observe",
        "create_order",
        "assign_order",
        "dispatch_order",
        "advance",
        "finish",
    }
    assert not any(event.kind == "rejected" for event in result.sim.events)


async def test_replay_policy_reproduces_semantic_events_and_economics() -> None:
    world = make_world(71, "private-world")
    a = await play(world, ReferenceOperator())
    b = await play(world, ReferenceOperator())
    assert a.calls == b.calls
    assert a.sim.events == b.sim.events
    assert a.sim.economics() == b.sim.economics()


async def test_greedy_uses_same_workflow_but_distinct_selection() -> None:
    # One vehicle: nearest-first loses a high-value request; reference plans both.
    world = World(
        (
            Request("low", (0.5, 0), (-1, 0), 1, 0, 20, 5),
            Request("high", (2, 0), (3, 0), 1, 0, 4, 100),
        ),
        (Vehicle("v", "d", 2, (0, 0)),),
        horizon=20,
    )
    reference = await play(world, ReferenceOperator())
    greedy = await play(world, GreedyOperator())
    assert reference.sim.economics()["reward"] > greedy.sim.economics()["reward"]
    assert reference.calls[0].args["request_id"] == "high"
    assert greedy.calls[0].args["request_id"] == "low"


async def test_reference_rejects_terminal_pickup_only_feasibility() -> None:
    world = World(
        (Request("impossible", (1, 0), (100, 0), 1, 0, 5, 20),),
        (Vehicle("v", "d", 2, (0, 0)),),
        horizon=20,
    )
    result = await play(world, ReferenceOperator())
    assert not result.created
    assert result.sim.economics()["distance"] == 0


async def test_partial_workflow_recovery_reads_back_then_continues() -> None:
    policy = ReferenceOperator()
    await policy.reset(SPEC, [], "")
    fixture = PublicWorkflow(make_world(0))
    call = await policy.act(Observation(data=fixture.observation()))
    assert call is not None and call.name == "create_order"
    # The application committed creation but the tool reported an ambiguous timeout.
    fixture.step(call)
    recovery = await policy.act(Observation(last_result=ToolResult(ok=False, error="timeout")))
    assert recovery is not None and recovery == ToolCall(name="observe")
    read_back = fixture.step(recovery)
    next_call = await policy.act(Observation(last_result=read_back))
    assert next_call is not None and next_call.name == "assign_order"


async def test_stop_early_dispatches_exactly_one_order_then_continues() -> None:
    result = await play(make_world(0), StopEarlyOperator())
    assert len(result.created) == 1
    assert [call.name for call in result.calls] == [
        "create_order",
        "assign_order",
        "dispatch_order",
        "finish",
    ]
    assert result.sim.economics()["deliveries"] == 1
    assert result.sim.economics()["unserved"] == 11

"""Tools v2 and the task trust boundary, on the in-memory backend."""

import json
import re
from typing import cast

import pytest
from pydantic import JsonValue

from crucible.backends import BackendError
from crucible.core.tools import ToolCall, ToolResult
from crucible.envs.fleetbase_dispatch.memory_adapter import InMemoryAdapter
from crucible.envs.fleetbase_dispatch.task import (
    BACKEND_ENV,
    MAX_STEPS,
    PAGE_SIZE,
    FleetbaseDispatch,
    FleetbaseSim,
)
from crucible.envs.fleetbase_dispatch.world import (
    Disruption,
    Request,
    Vehicle,
    World,
    WorldConfig,
    make_world,
)

TOOLS = {
    "list_orders",
    "get_order",
    "create_order",
    "update_order",
    "cancel_order",
    "assign_order",
    "unassign_order",
    "dispatch_order",
    "set_vehicle_route",
    "list_vehicles",
    "get_vehicle",
    "reposition_vehicle",
    "list_events",
    "get_forecast",
    "estimate_route",
    "advance",
    "finish",
}


class WorldSim(FleetbaseSim):
    """`fleetbase_sim@1` on a hand-made world."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self._world = world

    def _make_world(self, seed: int, salt: str | None) -> World:
        return self._world


def tiny_world(*requests: Request, disruptions: tuple[Disruption, ...] = ()) -> World:
    return World(
        requests=requests
        or (
            Request("r1", (3, 0), (6, 0), 2, 0, 50, 20),
            Request("r2", (4, 0), (5, 0), 2, 0, 50, 30),
            Request("r3", (1, 1), (2, 2), 1, 60, 120, 10),
        ),
        vehicles=(
            Vehicle("v1", "d1", 3, (0, 0), 0.2, 200),
            Vehicle("v2", "d2", 6, (10, 0), 0.4, 30),
        ),
        disruptions=disruptions,
        horizon=100,
        area=20,
    )


async def started(world: World | None = None) -> WorldSim:
    task = WorldSim(world or tiny_world())
    await task.reset(0)
    return task


async def ok(task: FleetbaseSim, name: str, **args: JsonValue) -> dict[str, JsonValue]:
    result = await task.step(ToolCall(name=name, args=args))
    assert result.ok, result.error
    assert isinstance(result.content, dict)
    return result.content


async def fails(task: FleetbaseSim, name: str, **args: JsonValue) -> str:
    result = await task.step(ToolCall(name=name, args=args))
    assert not result.ok, result.content
    return result.error or ""


async def dispatch(task: FleetbaseSim, request_id: str, vehicle_id: str = "v1") -> None:
    await ok(task, "create_order", request_id=request_id)
    await ok(task, "assign_order", request_id=request_id, vehicle_id=vehicle_id)
    await ok(task, "dispatch_order", request_id=request_id)


def test_tools_and_budget() -> None:
    task = FleetbaseSim()
    assert {t.name for t in task.tools()} == TOOLS
    assert FleetbaseSim.spec.max_steps == FleetbaseDispatch.spec.max_steps == MAX_STEPS == 530
    text = task.instruction()
    assert "530 tool calls" in text and "observe" not in text
    assert "U_hindsight" in text


async def test_reset_observation_is_minimal() -> None:
    task = FleetbaseSim()
    observation = await task.reset(0)
    assert set(observation.data) == {"now", "horizon", "released_orders", "vehicles", "budget"}
    assert observation.data["budget"] == MAX_STEPS and observation.data["vehicles"] == 6
    released = observation.data["released_orders"]
    assert isinstance(released, int) and 0 < released < len(make_world(0).requests)


async def test_actions_return_only_the_touched_record() -> None:
    task = await started()
    record = await ok(task, "create_order", request_id="r1")
    assert record["id"] == "r1" and record["status"] == "created"
    assert record["native_id"] == "order_0001" and "orders" not in record
    record = await ok(task, "assign_order", request_id="r1", vehicle_id="v1")
    assert record["status"] == "assigned" and record["vehicle_id"] == "v1"
    record = await ok(task, "dispatch_order", request_id="r1")
    assert record["status"] == "dispatched" and record["dispatched"] is True
    vehicle = await ok(task, "get_vehicle", vehicle_id="v1")
    assert vehicle["route"] == [
        {"request_id": "r1", "action": "pickup"},
        {"request_id": "r1", "action": "dropoff"},
    ]
    clock = await ok(task, "advance", minutes=3)
    assert clock == {"now": 3, "latest_event_seq": 3, "calls_remaining": MAX_STEPS - 5}
    record = await ok(task, "get_order", request_id="r1")
    assert record["status"] == "picked_up" and record["picked_up_at"] == 3
    assert (await ok(task, "finish")) == {"finished": True, "now": 3}
    assert task.done()
    assert await fails(task, "get_order", request_id="r1") == "Episode is over"


async def test_list_pagination_and_status_filter() -> None:
    requests = tuple(
        Request(f"r{i:02d}", (1, float(i % 5)), (2, 2), 1, 0, 90, 10) for i in range(23)
    )
    task = await started(tiny_world(*requests))
    first = await ok(task, "list_orders")
    orders = first["orders"]
    assert isinstance(orders, list) and len(orders) == PAGE_SIZE == 10
    assert (first["page"], first["pages"], first["total"]) == (1, 3, 23)
    last = await ok(task, "list_orders", page=3)
    assert isinstance(last["orders"], list) and len(last["orders"]) == 3
    beyond = await ok(task, "list_orders", page=4)
    assert beyond["orders"] == []
    await ok(task, "create_order", request_id="r05")
    await ok(task, "create_order", request_id="r17")
    created = await ok(task, "list_orders", status="created")
    created_orders = created["orders"]
    assert isinstance(created_orders, list)
    assert [o["id"] for o in created_orders if isinstance(o, dict)] == ["r05", "r17"]
    requested = await ok(task, "list_orders", status="requested")
    assert requested["total"] == 21
    vehicles = await ok(task, "list_vehicles")
    assert (vehicles["total"], vehicles["pages"]) == (2, 1)
    await fails(task, "list_orders", status="nonsense")
    await fails(task, "list_orders", page=0)


async def test_unreleased_requests_look_unknown() -> None:
    task = await started()
    unreleased = await fails(task, "get_order", request_id="r3")
    assert unreleased == await fails(task, "get_order", request_id="nope")
    assert await fails(task, "create_order", request_id="r3") == unreleased
    await ok(task, "advance", minutes=60)
    assert (await ok(task, "get_order", request_id="r3"))["status"] == "requested"


async def test_silent_changes_appear_only_on_the_order_record() -> None:
    world = tiny_world(
        disruptions=(
            Disruption("disruption-01", "cancellation", 1, request_id="r1", silent=True),
            Disruption(
                "disruption-02", "dropoff_change", 1, request_id="r2", point=(7, 7), silent=True
            ),
            Disruption("disruption-03", "breakdown", 2, vehicle_id="v2", duration=10),
        )
    )
    task = await started(world)
    await dispatch(task, "r1")
    await ok(task, "advance", minutes=5)
    events = await ok(task, "list_events")
    listed = events["events"]
    assert isinstance(listed, list)
    kinds = [e["kind"] for e in listed if isinstance(e, dict)]
    assert kinds == ["released", "released", "breakdown"]
    assert "disruption-01" not in json.dumps(events)
    cancelled = await ok(task, "get_order", request_id="r1")
    assert cancelled["status"] == "cancelled" and cancelled["cancelled_by"] == "customer"
    changed = await ok(task, "get_order", request_id="r2")
    assert changed["dropoff"] == [7, 7]
    vehicle = await ok(task, "get_vehicle", vehicle_id="v1")
    assert vehicle["route"] == []
    score = await task.grade()
    assert score.breakdown["cancellation_fees"] == 0
    assert "disruption-01" in task.hidden_strings() and "disruption-02" in task.hidden_strings()
    assert "disruption-03" not in task.hidden_strings()


async def test_agent_cancellation_after_dispatch_costs_a_fee() -> None:
    task = await started()
    await dispatch(task, "r1")
    await ok(task, "create_order", request_id="r2")
    record = await ok(task, "cancel_order", request_id="r1")
    assert record["status"] == "cancelled" and record["cancelled_by"] == "agent"
    await ok(task, "cancel_order", request_id="r2")
    await fails(task, "assign_order", request_id="r1", vehicle_id="v1")
    score = await task.grade()
    assert score.breakdown["cancellation_fees"] == WorldConfig().cancellation_fee
    assert score.breakdown["agent_cancelled"] == 2 and score.total == 0


async def test_update_order_changes_only_bookkeeping() -> None:
    task = await started()
    await fails(task, "update_order", request_id="r1", note="x")  # not created yet
    await dispatch(task, "r1")
    await fails(task, "update_order", request_id="r1")
    await fails(task, "update_order", request_id="r1", priority="urgent")
    await fails(task, "update_order", request_id="r1", status="delivered")
    record = await ok(task, "update_order", request_id="r1", note="fragile", priority="high")
    assert (record["note"], record["priority"], record["status"]) == (
        "fragile",
        "high",
        "dispatched",
    )
    baseline = await started()
    await dispatch(baseline, "r1")
    assert (await task.grade()).total == (await baseline.grade()).total > 0


async def test_pickup_is_irreversible_through_every_tool() -> None:
    task = await started()
    await dispatch(task, "r1")
    await ok(task, "advance", minutes=3)
    for name in ("cancel_order", "unassign_order"):
        assert "irreversible" in await fails(task, name, request_id="r1")
    assert "irreversible" in await fails(task, "assign_order", request_id="r1", vehicle_id="v2")
    await ok(task, "update_order", request_id="r1", note="on board")


async def test_routes_capacity_and_estimates() -> None:
    task = await started()
    await dispatch(task, "r1")
    await ok(task, "create_order", request_id="r2")
    await ok(task, "assign_order", request_id="r2", vehicle_id="v1")
    pooled: list[JsonValue] = [
        {"request_id": "r1", "action": "pickup"},
        {"request_id": "r2", "action": "pickup"},
        {"request_id": "r2", "action": "dropoff"},
        {"request_id": "r1", "action": "dropoff"},
    ]
    # Loads 2 + 2 exceed capacity 3 at the second pickup.
    assert "Capacity exceeded" in await fails(
        task, "set_vehicle_route", vehicle_id="v1", stops=pooled
    )
    estimate = await ok(task, "estimate_route", vehicle_id="v1", stops=pooled)
    assert estimate["feasible"] is False
    assert "exactly the remaining stops" in await fails(
        task, "set_vehicle_route", vehicle_id="v1", stops=pooled[:2]
    )
    sequential: list[JsonValue] = [pooled[1], pooled[2], pooled[0], pooled[3]]
    vehicle = await ok(task, "set_vehicle_route", vehicle_id="v1", stops=sequential)
    assert vehicle["id"] == "v1" and vehicle["assigned_orders"] == ["r2"]
    # The undispatched r2 is not executed yet.
    assert vehicle["route"] == [pooled[0], pooled[3]]
    await ok(task, "dispatch_order", request_id="r2")
    estimate = await ok(task, "estimate_route", vehicle_id="v1", stops=sequential)
    assert estimate["feasible"] is True and estimate["end_time"] == 10
    task_after = await ok(task, "advance", minutes=100)
    assert task_after["now"] == 100
    score = await task.grade()
    assert score.breakdown["deliveries"] == 2


async def test_assign_is_rejected_when_the_load_exceeds_capacity() -> None:
    task = await started(tiny_world(Request("big", (1, 0), (2, 0), 4, 0, 50, 20)))
    await ok(task, "create_order", request_id="big")
    assert "capacity" in await fails(task, "assign_order", request_id="big", vehicle_id="v1")
    await ok(task, "assign_order", request_id="big", vehicle_id="v2")


async def test_vehicles_shift_end_reposition_and_forecast() -> None:
    task = await started()
    await ok(task, "advance", minutes=31)
    vehicle = await ok(task, "get_vehicle", vehicle_id="v2")
    assert vehicle["status"] == "off_shift" and vehicle["shift_end"] == 30
    await ok(task, "create_order", request_id="r1")
    assert "shift" in await fails(task, "assign_order", request_id="r1", vehicle_id="v2")
    assert "outside" in await fails(task, "reposition_vehicle", vehicle_id="v1", point=[50, 0])
    moved = await ok(task, "reposition_vehicle", vehicle_id="v1", point=[0, 4])
    assert moved["reposition_target"] == [0, 4]
    await ok(task, "advance", minutes=5)
    assert (await ok(task, "get_vehicle", vehicle_id="v1"))["position"] == [0, 4]
    generated = FleetbaseSim()
    await generated.reset(0)
    forecast = await ok(generated, "get_forecast")
    wave = forecast["next_wave"]
    assert isinstance(wave, dict) and wave["starts_at"] == 40
    zones = wave["zones"]
    assert isinstance(zones, list) and len(zones) == 9
    await ok(generated, "advance", minutes=201)
    assert (await ok(generated, "get_forecast"))["next_wave"] is None


async def test_events_page_with_since() -> None:
    requests = tuple(Request(f"r{i:02d}", (1, 0), (2, 0), 1, 0, 90, 10) for i in range(12))
    task = await started(tiny_world(*requests))
    first = await ok(task, "list_events")
    events = first["events"]
    assert isinstance(events, list) and len(events) == 10
    assert first["next_since"] == 10 and first["more"] is True
    second = await ok(task, "list_events", since=10)
    assert second["more"] is False
    seqs = [e["seq"] for e in cast(list[dict[str, JsonValue]], second["events"])]
    assert seqs == [11, 12]


async def test_rejected_calls_count_and_cost_nothing() -> None:
    task = await started()
    for call in (
        ToolCall(name="no_such_tool"),
        ToolCall(name="advance", args={"minutes": -1}),
        ToolCall(name="dispatch_order", args={"request_id": "r1"}),
    ):
        result: ToolResult = await task.step(call)
        assert not result.ok
    clock = await ok(task, "advance", minutes=1)
    assert clock["calls_remaining"] == MAX_STEPS - 4
    score = await task.grade()
    assert score.breakdown["rejected_operations"] == 3 and score.total == 0


async def test_native_completed_status_cannot_earn_value() -> None:
    task = await started()
    await ok(task, "create_order", request_id="r1")
    adapter = cast(InMemoryAdapter, task._adapter)  # pyright: ignore[reportPrivateUsage]
    order = adapter._orders["r1"]  # pyright: ignore[reportPrivateUsage]
    order["status"] = "completed"
    score = await task.grade()
    assert score.total == 0 and score.breakdown["deliveries"] == 0


async def test_grade_continues_dispatched_work_and_evidence_is_frozen() -> None:
    task = await started()
    await dispatch(task, "r1")
    await ok(task, "finish")
    score = await task.grade()
    assert score.breakdown["deliveries"] == 1
    # The hindsight planner serves r1 and r2 in this world, so it earns more.
    hindsight = score.breakdown["u_hindsight"]
    assert hindsight > 20 - 6 * 0.2
    assert score.total == pytest.approx((20 - 6 * 0.2) / hindsight)
    assert await task.grade() == score
    evidence = task.grading_evidence()
    evidence["events"] = []
    assert task.grading_evidence()["events"] != []
    await task.aclose()
    await task.aclose()


async def test_hidden_strings_cover_future_requests_and_unrevealed_disruptions() -> None:
    task = FleetbaseSim()
    await task.reset(0)
    listing = await ok(task, "list_orders")
    await ok(task, "finish")
    world = make_world(0)
    hidden = task.hidden_strings()
    future = [r for r in world.requests if r.release > 0]
    assert future and all(r.id in hidden for r in future)
    assert all(r.id not in hidden for r in world.requests if r.release == 0)
    assert all(d.id in hidden for d in world.disruptions if d.silent or d.time > 0)
    said = json.dumps(listing)
    for word in hidden:
        assert not re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", said)


def test_sim_task_is_offline_with_generic_checks() -> None:
    sim = FleetbaseSim()
    assert not sim.external_backend and sim.deployment_bundle() is None
    assert sim.instruction() == FleetbaseDispatch().instruction()
    assert [t.model_dump() for t in sim.tools()] == [
        t.model_dump() for t in FleetbaseDispatch().tools()
    ]
    checks = FleetbaseSim.checks
    assert checks.determinism and checks.do_nothing_zero and checks.reward_bounds
    assert checks.no_hidden_state and checks.reference_solves and checks.exploits_below_reference
    assert FleetbaseDispatch.external_backend


async def test_dispatch_backend_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FLEETBASE_URL", "FLEETBASE_TOKEN", "FLEETBASE_DEPLOYMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    with pytest.raises(BackendError):
        await FleetbaseDispatch().reset(0)
    monkeypatch.setenv(BACKEND_ENV, "postgres")
    with pytest.raises(BackendError):
        await FleetbaseDispatch().reset(0)
    monkeypatch.setenv(BACKEND_ENV, "memory")
    task = FleetbaseDispatch()
    observation = await task.reset(0)
    assert observation.data == (await FleetbaseSim().reset(0)).data
    with pytest.raises(RuntimeError):
        await task.reset(0)
    await task.aclose()

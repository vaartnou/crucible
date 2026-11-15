"""Task trust boundary checks; clean deployment proofs are recorded separately."""

from typing import cast

import pytest
from pydantic import JsonValue

from crucible.core.tools import ToolCall
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent, WorldSimulator
from crucible.envs.fleetbase_dispatch.task import FleetbaseDispatch
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World


class FakeAdapter:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, JsonValue]] = {}
        self.events: list[PhysicalEvent] = []
        self.closes = 0

    async def create_order(self, request: Request) -> dict[str, JsonValue]:
        self.records.setdefault(request.id, {"id": "native-1", "status": "completed"})
        return self.records[request.id]

    async def assign_order(self, request_id: str, vehicle_id: str) -> dict[str, JsonValue]:
        self.records[request_id]["vehicle_id"] = vehicle_id
        return self.records[request_id]

    async def dispatch_order(self, request_id: str) -> dict[str, JsonValue]:
        self.records[request_id]["dispatched"] = True
        return self.records[request_id]

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        return self.records

    async def snapshot(self) -> dict[str, JsonValue]:
        return {"orders": {key: dict(value) for key, value in self.records.items()}}

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        self.events.extend(events)

    async def cleanup(self) -> None:
        self.closes += 1


def prepared() -> tuple[FleetbaseDispatch, FakeAdapter]:
    task = FleetbaseDispatch()
    adapter = FakeAdapter()
    world = World(
        requests=(Request("request-1", (1, 0), (3, 0), 1, 0, 10, 100),),
        vehicles=(Vehicle("vehicle-1", "driver-1", 2, (0, 0)),),
    )
    task._sim = WorldSimulator(world)  # pyright: ignore[reportPrivateUsage]
    task._adapter = cast(FleetbaseAdapter, cast(object, adapter))  # pyright: ignore[reportPrivateUsage]
    return task, adapter


async def test_native_completed_status_cannot_earn_value() -> None:
    task, _ = prepared()
    await task.step(ToolCall(name="create_order", args={"request_id": "request-1"}))
    score = await task.grade()
    assert score.total == 0
    assert score.breakdown["deliveries"] == 0


async def test_native_workflow_physical_grade_and_evidence_are_frozen() -> None:
    task, adapter = prepared()
    for call in (
        ToolCall(name="create_order", args={"request_id": "request-1"}),
        ToolCall(name="assign_order", args={"request_id": "request-1", "vehicle_id": "vehicle-1"}),
        ToolCall(name="dispatch_order", args={"request_id": "request-1"}),
    ):
        assert (await task.step(call)).ok
    score = await task.grade()
    assert score.total == pytest.approx(0.994)
    assert score.breakdown["utility"] == 99.4
    events = len(adapter.events)
    assert await task.grade() == score
    assert len(adapter.events) == events
    evidence = task.grading_evidence()
    evidence["events"] = []
    assert task.grading_evidence()["events"] != []
    assert not (await task.step(ToolCall(name="observe"))).ok
    await task.aclose()
    await task.aclose()
    assert adapter.closes == 1


async def test_pickup_prevents_reassignment_and_unreleased_request_cannot_create() -> None:
    task, _ = prepared()
    for call in (
        ToolCall(name="create_order", args={"request_id": "request-1"}),
        ToolCall(name="assign_order", args={"request_id": "request-1", "vehicle_id": "vehicle-1"}),
        ToolCall(name="dispatch_order", args={"request_id": "request-1"}),
        ToolCall(name="advance", args={"minutes": 1}),
    ):
        assert (await task.step(call)).ok
    assert not (
        await task.step(
            ToolCall(
                name="assign_order", args={"request_id": "request-1", "vehicle_id": "vehicle-2"}
            )
        )
    ).ok
    assert not (await task.step(ToolCall(name="create_order", args={"request_id": "unknown"}))).ok
    assert not (await task.step(ToolCall(name="advance", args={"minutes": -1}))).ok

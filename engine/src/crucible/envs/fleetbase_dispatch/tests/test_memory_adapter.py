"""InMemoryAdapter: native-compatible shapes, idempotency and every rejection case."""

import pytest
from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.adapter_protocol import (
    DispatchAdapter,
    FleetbaseOperationError,
)
from crucible.envs.fleetbase_dispatch.memory_adapter import InMemoryAdapter
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

VEHICLES = (
    Vehicle("vehicle-1", "driver-1", 2, (0, 0)),
    Vehicle("vehicle-2", "driver-2", 4, (6, 0)),
)
REQUEST = Request("request-01", (1, 0), (3, 0), 1, 0, 30, 50)
SECOND = Request("request-02", (2, 0), (4, 0), 1, 0, 30, 50)


def commit(
    vehicle_id: str | None,
    dispatched: bool,
    *,
    cancelled: bool = False,
    ranks: tuple[int, int] | None = None,
) -> dict[str, JsonValue]:
    return {
        "vehicle_id": vehicle_id,
        "dispatched": dispatched,
        "cancelled": cancelled,
        "pickup_rank": ranks[0] if ranks else None,
        "dropoff_rank": ranks[1] if ranks else None,
    }


# Keys the native adapter's snapshot() reads back for each order.
NATIVE_ORDER_KEYS = {
    "id",
    "internal_id",
    "status",
    "dispatched",
    "started",
    "driver_assigned",
    "vehicle_assigned",
    "meta",
    "payload",
}


def test_backends_satisfy_protocol() -> None:
    # Checked statically by pyright: both classes structurally match the protocol.
    backends: tuple[type[DispatchAdapter], ...] = (FleetbaseAdapter, InMemoryAdapter)
    assert len(backends) == 2


async def adapter(*, created: bool = True) -> InMemoryAdapter:
    result = InMemoryAdapter()
    await result.prepare(VEHICLES)
    if created:
        await result.create_order(REQUEST)
    return result


async def test_create_is_idempotent_like_native() -> None:
    memory = await adapter()
    first = await memory.create_order(REQUEST)
    second = await memory.create_order(REQUEST)
    assert first == second
    snapshot = await memory.snapshot()
    orders = snapshot["orders"]
    assert isinstance(orders, dict) and list(orders) == ["request-01"]
    assert snapshot["duplicate_orders"] == 0


async def test_shapes_match_native_contract() -> None:
    memory = await adapter()
    assert await memory.committed() == {"request-01": commit(None, False)}
    await memory.assign_order("request-01", "vehicle-2")
    await memory.dispatch_order("request-01")
    assert await memory.committed() == {"request-01": commit("vehicle-2", True, ranks=(0, 1))}
    snapshot = await memory.snapshot()
    assert set(snapshot) == {"orders", "vehicles", "drivers", "duplicate_orders"}
    orders = snapshot["orders"]
    assert isinstance(orders, dict)
    record = orders["request-01"]
    assert isinstance(record, dict) and set(record) == NATIVE_ORDER_KEYS
    assert record["status"] == "dispatched"
    assert record["internal_id"] == "crucible-memory-request-01"
    assert snapshot["vehicles"] == {"vehicle-1": "vehicle_0001", "vehicle-2": "vehicle_0002"}


async def test_ids_are_deterministic_and_results_are_copies() -> None:
    a, b = await adapter(), await adapter()
    assert await a.snapshot() == await b.snapshot()
    record = await a.create_order(REQUEST)
    record["status"] = "tampered"
    snapshot = await a.snapshot()
    orders = snapshot["orders"]
    assert isinstance(orders, dict) and orders["request-01"] != record


@pytest.mark.parametrize("operation", ["assign", "dispatch", "cancel", "unassign", "update"])
async def test_unknown_order_is_rejected(operation: str) -> None:
    memory = await adapter(created=False)
    with pytest.raises(FleetbaseOperationError):
        if operation == "assign":
            await memory.assign_order("request-01", "vehicle-1")
        elif operation == "dispatch":
            await memory.dispatch_order("request-01")
        elif operation == "cancel":
            await memory.cancel_order("request-01")
        elif operation == "unassign":
            await memory.unassign_order("request-01")
        else:
            await memory.update_order("request-01", {"note": "x"})


async def test_unknown_vehicle_is_rejected() -> None:
    memory = await adapter()
    with pytest.raises(FleetbaseOperationError):
        await memory.assign_order("request-01", "vehicle-9")


async def test_dispatch_before_assign_is_rejected_and_repeat_dispatch_is_a_no_op() -> None:
    memory = await adapter()
    with pytest.raises(FleetbaseOperationError):
        await memory.dispatch_order("request-01")
    await memory.assign_order("request-01", "vehicle-1")
    first = await memory.dispatch_order("request-01")
    assert first == await memory.dispatch_order("request-01")


async def test_change_after_pickup_is_rejected() -> None:
    memory = await adapter()
    await memory.assign_order("request-01", "vehicle-1")
    await memory.dispatch_order("request-01")
    # Reassignment before pickup is legal.
    await memory.assign_order("request-01", "vehicle-2")
    await memory.assign_order("request-01", "vehicle-1")
    await memory.sync_events([PhysicalEvent("picked_up", 1.0, "request-01", "vehicle-1")])
    with pytest.raises(FleetbaseOperationError):
        await memory.assign_order("request-01", "vehicle-2")
    with pytest.raises(FleetbaseOperationError):
        await memory.cancel_order("request-01")
    with pytest.raises(FleetbaseOperationError):
        await memory.unassign_order("request-01")
    # Committed state is unchanged, so the native adapter sends no mutation either.
    record = await memory.assign_order("request-01", "vehicle-1")
    assert record["status"] == "enroute" and record["started"] is True
    await memory.sync_events([PhysicalEvent("delivered", 3.0, "request-01", "vehicle-1")])
    snapshot = await memory.snapshot()
    orders = snapshot["orders"]
    assert isinstance(orders, dict)
    order = orders["request-01"]
    assert isinstance(order, dict) and order["status"] == "completed"


async def test_progress_for_an_uncreated_order_is_rejected() -> None:
    memory = await adapter(created=False)
    with pytest.raises(FleetbaseOperationError):
        await memory.sync_events([PhysicalEvent("picked_up", 1.0, "request-01", "vehicle-1")])


async def test_cancelled_order_is_rejected() -> None:
    memory = await adapter()
    await memory.assign_order("request-01", "vehicle-1")
    await memory.cancel_order("request-01")
    with pytest.raises(FleetbaseOperationError):
        await memory.assign_order("request-01", "vehicle-2")
    with pytest.raises(FleetbaseOperationError):
        await memory.assign_order("request-01", "vehicle-1")
    with pytest.raises(FleetbaseOperationError):
        await memory.dispatch_order("request-01")
    # Create stays idempotent and cannot revive the order.
    record = await memory.create_order(REQUEST)
    assert record["status"] == "canceled"
    assert await memory.committed() == {"request-01": commit(None, False, cancelled=True)}


async def test_cleanup_is_repeatable() -> None:
    memory = await adapter()
    await memory.cleanup()
    await memory.cleanup()
    assert await memory.committed() == {}


async def test_many_orders_per_vehicle_append_route_ranks() -> None:
    memory = await adapter()
    await memory.create_order(SECOND)
    await memory.assign_order("request-01", "vehicle-1")
    await memory.assign_order("request-02", "vehicle-1")
    await memory.dispatch_order("request-01")
    await memory.dispatch_order("request-02")
    assert await memory.committed() == {
        "request-01": commit("vehicle-1", True, ranks=(0, 1)),
        "request-02": commit("vehicle-1", True, ranks=(2, 3)),
    }
    # Moving an order to another vehicle appends it there.
    await memory.assign_order("request-01", "vehicle-2")
    committed = await memory.committed()
    assert committed["request-01"] == commit("vehicle-2", True, ranks=(0, 1))


async def test_set_route_rewrites_ranks_in_order_meta() -> None:
    memory = await adapter()
    await memory.create_order(SECOND)
    await memory.assign_order("request-01", "vehicle-1")
    await memory.assign_order("request-02", "vehicle-1")
    stops = [
        ("request-02", "pickup"),
        ("request-01", "pickup"),
        ("request-01", "dropoff"),
        ("request-02", "dropoff"),
    ]
    await memory.set_route("vehicle-1", stops)
    committed = await memory.committed()
    assert committed["request-01"] == commit("vehicle-1", False, ranks=(1, 2))
    assert committed["request-02"] == commit("vehicle-1", False, ranks=(0, 3))
    snapshot = await memory.snapshot()
    orders = snapshot["orders"]
    assert isinstance(orders, dict)
    order = orders["request-02"]
    assert isinstance(order, dict) and isinstance(order["meta"], dict)
    assert order["meta"]["crucible_route"] == {"pickup": 0, "dropoff": 3}
    with pytest.raises(FleetbaseOperationError):
        await memory.set_route("vehicle-2", stops)
    with pytest.raises(FleetbaseOperationError):
        await memory.set_route("vehicle-9", [])


async def test_unassign_and_update() -> None:
    memory = await adapter()
    await memory.assign_order("request-01", "vehicle-1")
    await memory.dispatch_order("request-01")
    record = await memory.unassign_order("request-01")
    assert record["status"] == "created" and record["dispatched"] is False
    assert await memory.committed() == {"request-01": commit(None, False)}
    record = await memory.update_order("request-01", {"note": "call ahead", "priority": "high"})
    meta = record["meta"]
    assert isinstance(meta, dict) and meta["note"] == "call ahead" and meta["priority"] == "high"
    with pytest.raises(FleetbaseOperationError):
        await memory.update_order("request-01", {"status": "completed"})


async def test_world_changes_sync_to_order_records() -> None:
    memory = await adapter()
    await memory.create_order(SECOND)
    await memory.assign_order("request-01", "vehicle-1")
    await memory.dispatch_order("request-01")
    await memory.sync_events(
        [
            PhysicalEvent("unassigned", 1.0, "request-01", "vehicle-1"),
            PhysicalEvent("dropoff_changed", 2.0, "request-02", data={"dropoff": [5, 5]}),
            PhysicalEvent("customer_cancelled", 3.0, "request-02", public=False),
            # Changes to an uncreated order are skipped.
            PhysicalEvent("customer_cancelled", 3.0, "request-09", public=False),
        ]
    )
    committed = await memory.committed()
    assert committed["request-01"] == commit(None, False)
    assert committed["request-02"] == commit(None, False, cancelled=True)
    snapshot = await memory.snapshot()
    orders = snapshot["orders"]
    assert isinstance(orders, dict)
    order = orders["request-02"]
    assert isinstance(order, dict) and order["status"] == "canceled"
    assert isinstance(order["meta"], dict) and order["meta"]["dropoff"] == [5, 5]

"""Backend parity: one recorded tool trace gives equal physics on every backend.

The trace is the reference policy on seed 0 plus deliberate rejected calls and the
v2 operations (update, unassign, route, cancel). It is replayed against:

- the in-memory backend (`fleetbase_sim@1` and `fleetbase_dispatch@1` with
  `FLEETBASE_BACKEND=memory`);
- `FleetbaseAdapter` over an offline fake of the audited native HTTP contract, and
- a real native installation (`FLEETBASE_URL`, `FLEETBASE_TOKEN`,
  `FLEETBASE_DEPLOYMENT_ID` and `FLEETBASE_EXCLUSIVE=1`).

The fake proves the adapter's logic against the documented contract, including
read-back after injected post-commit failures and complete cleanup. It does not prove
that the live API behaves as documented: the v2 endpoints are assumptions until the
live test passes (docs/fleetbase-api-audit.md, "v2 operations").

Physical events, economics and rejected-call counts must be equal.
"""

import json
import os
from typing import Any

import httpx
import pytest
from pydantic import JsonValue

from crucible.backends import BackendEpisode, ResourceLease
from crucible.core.tools import Observation, ToolCall
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.adapter_protocol import (
    DispatchAdapter,
    FleetbaseOperationError,
)
from crucible.envs.fleetbase_dispatch.memory_adapter import InMemoryAdapter
from crucible.envs.fleetbase_dispatch.policies import ReferenceOperator
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.task import (
    BACKEND_ENV,
    DispatchTaskBase,
    FleetbaseDispatch,
    FleetbaseSim,
)
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

SEED = 0
NATIVE_ENV = ("FLEETBASE_URL", "FLEETBASE_TOKEN", "FLEETBASE_DEPLOYMENT_ID")
# Rejected: dispatch of an uncreated order, unreleased request, unknown vehicle, dispatch
# before assignment. The creates commit and later replay idempotently. Then the v2
# operations: a note, assign + unassign, a dispatch that is cancelled (fee), and a
# route rewrite.
PRELUDE = (
    ToolCall(name="dispatch_order", args={"request_id": "request-01"}),
    ToolCall(name="create_order", args={"request_id": "request-40"}),
    ToolCall(name="create_order", args={"request_id": "request-01"}),
    ToolCall(name="create_order", args={"request_id": "request-01"}),
    ToolCall(name="assign_order", args={"request_id": "request-01", "vehicle_id": "vehicle-9"}),
    ToolCall(name="dispatch_order", args={"request_id": "request-01"}),
    ToolCall(name="update_order", args={"request_id": "request-01", "note": "parity"}),
    ToolCall(name="assign_order", args={"request_id": "request-01", "vehicle_id": "vehicle-1"}),
    ToolCall(name="unassign_order", args={"request_id": "request-01"}),
    ToolCall(name="create_order", args={"request_id": "request-02"}),
    ToolCall(name="assign_order", args={"request_id": "request-02", "vehicle_id": "vehicle-2"}),
    ToolCall(name="dispatch_order", args={"request_id": "request-02"}),
    ToolCall(name="cancel_order", args={"request_id": "request-02"}),
    ToolCall(name="create_order", args={"request_id": "request-03"}),
    ToolCall(name="create_order", args={"request_id": "request-04"}),
    ToolCall(name="assign_order", args={"request_id": "request-03", "vehicle_id": "vehicle-6"}),
    ToolCall(name="assign_order", args={"request_id": "request-04", "vehicle_id": "vehicle-6"}),
    ToolCall(
        name="set_vehicle_route",
        args={
            "vehicle_id": "vehicle-6",
            "stops": [
                {"request_id": "request-04", "action": "pickup"},
                {"request_id": "request-04", "action": "dropoff"},
                {"request_id": "request-03", "action": "pickup"},
                {"request_id": "request-03", "action": "dropoff"},
            ],
        },
    ),
    ToolCall(name="dispatch_order", args={"request_id": "request-03"}),
    ToolCall(name="dispatch_order", args={"request_id": "request-04"}),
)


class FakeFleetbase:
    """In-process HTTP fake of the native contract in docs/fleetbase-api-audit.md.

    It models the audited v1 endpoints and the documented, live-unverified v2
    assumptions: ``PUT /v1/orders/:id`` sets only the keys it receives (``driver`` /
    ``vehicle`` null clears the assignment, ``meta`` replaces the whole object, and
    ``dispatched`` cannot be reset), ``DELETE /v1/orders/:id/cancel`` sets status
    ``canceled``, and ``PUT /v1/{places,vehicles}/:id`` updates the given attributes.

    With ``fail_every`` set, every n-th mutation other than a resource delete commits
    and then answers HTTP 500, like the audited notification failure, so each operation
    must settle its outcome by read-back.
    """

    def __init__(self, fail_every: int | None = None) -> None:
        self.flow = {"started": "enroute", "enroute": "completed"}
        self.records: dict[str, dict[str, dict[str, JsonValue]]] = {}
        self.count = 0
        self.fail_every = fail_every
        self.mutations = 0
        self.injected = 0
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = self._handle(request)
        self.calls.append(f"{request.method} {request.url.path} {response.status_code}")
        is_delete = request.method == "DELETE" and request.url.path.count("/") == 3
        if request.method != "GET" and response.is_success and not is_delete:
            self.mutations += 1
            if self.fail_every and self.mutations % self.fail_every == 0:
                self.injected += 1
                return httpx.Response(500, json={"error": "notification transport"})
        return response

    def _handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.removeprefix("/v1/").split("/")
        resource, native = parts[0], parts[1] if len(parts) > 1 else None
        action = parts[2] if len(parts) > 2 else None
        table = self.records.setdefault(resource, {})
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if native is None:
            if request.method == "GET":
                offset = int(request.url.params.get("offset", "0"))
                limit = int(request.url.params.get("limit", "100"))
                return httpx.Response(200, json=list(table.values())[offset : offset + limit])
            self.count += 1
            record: dict[str, JsonValue] = {"id": f"{resource}_{self.count}", **body}
            if resource == "orders":
                record |= {
                    "status": "created",
                    "dispatched": False,
                    "started": False,
                    "driver_assigned": None,
                    "vehicle_assigned": None,
                    "payload": {
                        "pickup": {"id": body["pickup"]},
                        "dropoff": {"id": body["dropoff"]},
                    },
                }
            table[str(record["id"])] = record
            return httpx.Response(201, json=record)
        record = table.get(native, {})
        if not record:
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "GET" and action is None:
            return httpx.Response(200, json=record)
        if request.method == "DELETE" and action is None:
            del table[native]
            return httpx.Response(200, json={"deleted": True})
        if resource != "orders":
            if request.method != "PUT" or action is not None:
                return httpx.Response(405, json={"error": "method not allowed"})
            record |= body
            return httpx.Response(200, json=record)
        if request.method == "PUT" and action is None:
            for key in ("driver", "vehicle"):
                if key in body:
                    value = body[key]
                    record[key + "_assigned"] = None if value is None else {"id": value}
            if "meta" in body:
                record["meta"] = body["meta"]
        elif request.method == "DELETE" and action == "cancel":
            record["status"] = "canceled"
        elif action == "dispatch":
            if record["driver_assigned"] is None or record["dispatched"] is True:
                return httpx.Response(400, json={"error": "cannot dispatch"})
            record |= {"dispatched": True, "status": "dispatched"}
        elif action == "start":
            if record["dispatched"] is not True or record["driver_assigned"] is None:
                return httpx.Response(400, json={"error": "not dispatched"})
            record |= {"started": True, "status": "started"}
        elif action == "next-activity":
            return httpx.Response(200, json=[{"code": self.flow[str(record["status"])]}])
        elif action == "update-activity":
            record["status"] = body["activity"]["code"]
        else:
            return httpx.Response(405, json={"error": "method not allowed"})
        return httpx.Response(200, json=record)


class FakeNativeDispatch(FleetbaseDispatch):
    """`fleetbase_dispatch@1` with the native adapter talking to `FakeFleetbase`."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__()
        self._client = client

    async def _open_adapter(self, seed: int, salt: str | None) -> DispatchAdapter:
        async def release() -> None:
            return None

        self._episode_id = "fake-native"
        lease = ResourceLease(self._episode_id, "exclusive", "exclusive", release)
        return FleetbaseAdapter(BackendEpisode(lease, self._client))


async def record_trace() -> list[ToolCall]:
    """Reference policy on the memory backend, plus deliberate rejections."""
    task = FleetbaseSim()
    observation = await task.reset(SEED)
    for call in PRELUDE:
        await task.step(call)
    policy = ReferenceOperator()
    await policy.reset(task.spec, task.tools(), task.instruction())
    calls = list(PRELUDE)
    injected = False
    for _ in range(task.spec.max_steps):
        if task.done():
            break
        call = await policy.act(observation)
        if call is None:
            break
        result = await task.step(call)
        calls.append(call)
        observation = Observation(last_result=result)
        if injected or call.name != "advance" or task.done():
            continue
        probe = ToolCall(name="list_orders", args={"status": "picked_up"})
        listing = await task.step(probe)
        calls.append(probe)
        content = listing.content
        assert listing.ok and isinstance(content, dict) and isinstance(content["orders"], list)
        picked = [r for r in content["orders"] if isinstance(r, dict)]
        if picked:
            # Cargo cannot move after pickup.
            other = "vehicle-1" if picked[0]["vehicle_id"] != "vehicle-1" else "vehicle-2"
            bad = ToolCall(
                name="assign_order", args={"request_id": picked[0]["id"], "vehicle_id": other}
            )
            assert not (await task.step(bad)).ok
            calls.append(bad)
            injected = True
    assert injected
    await task.aclose()
    return calls


async def replay(task: DispatchTaskBase, calls: list[ToolCall]) -> dict[str, JsonValue]:
    try:
        await task.reset(SEED)
        for call in calls:
            await task.step(call)
        await task.grade()
        evidence = task.grading_evidence()
        return {key: evidence[key] for key in ("events", "economics", "rejected_operations")}
    finally:
        await task.aclose()


@pytest.fixture
async def trace() -> list[ToolCall]:
    return await record_trace()


@pytest.fixture
async def expected(trace: list[ToolCall]) -> dict[str, JsonValue]:
    result = await replay(FleetbaseSim(), trace)
    economics = result["economics"]
    assert isinstance(economics, dict)
    assert economics["deliveries"] and economics["reward"]
    assert economics["cancellation_fees"]
    assert result["rejected_operations"] == 5
    assert economics["duplicate_orders"] == 0
    return result


async def test_memory_replay_is_deterministic(
    trace: list[ToolCall], expected: dict[str, JsonValue]
) -> None:
    assert await replay(FleetbaseSim(), trace) == expected


async def test_dispatch_memory_backend_matches_sim(
    trace: list[ToolCall], expected: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "memory")
    assert await replay(FleetbaseDispatch(), trace) == expected


def fake_task(fake: FakeFleetbase) -> tuple[FakeNativeDispatch, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="http://fleetbase.invalid", transport=httpx.MockTransport(fake.handle)
    )
    return FakeNativeDispatch(client), client


def trace_kinds(result: dict[str, JsonValue]) -> set[str]:
    events = result["events"]
    assert isinstance(events, list)
    return {str(event["kind"]) for event in events if isinstance(event, dict)}


@pytest.mark.parametrize("fail_every", [None, 7])
async def test_native_adapter_on_fake_contract_matches_memory(
    trace: list[ToolCall], expected: dict[str, JsonValue], fail_every: int | None
) -> None:
    # The reference trace exercises every v2 operation and world change.
    names = {call.name for call in trace}
    assert {"cancel_order", "unassign_order", "update_order", "set_vehicle_route"} <= names
    assert {
        "customer_cancelled",
        "dropoff_changed",
        "unassigned",
        "breakdown",
        "repaired",
        "shift_ended",
        "agent_cancelled",
    } <= trace_kinds(expected)
    fake = FakeFleetbase(fail_every)
    task, client = fake_task(fake)
    async with client:
        assert await replay(task, trace) == expected
    assert not any(fake.records.values()), "native cleanup leaves no owned records"
    calls = " ".join(fake.calls)
    for endpoint in ("/cancel 200", "PUT /v1/places/", "PUT /v1/vehicles/", "/dispatch 200"):
        assert endpoint in calls
    if fail_every:
        assert fake.injected > 10


async def test_native_world_changes_mirror_into_records(trace: list[ToolCall]) -> None:
    fake = FakeFleetbase()
    task, client = fake_task(fake)
    async with client:
        await task.reset(SEED)
        for call in trace:
            await task.step(call)
        await task.grade()
        orders = fake.records["orders"].values()
        metas = [order["meta"] for order in orders if isinstance(order["meta"], dict)]
        assert sum(meta.get("cancelled_by") == "customer" for meta in metas) >= 1
        assert any(isinstance(meta.get("dropoff"), list) for meta in metas)
        assert sum(order["status"] == "canceled" for order in orders) >= 2
        statuses = [vehicle["status"] for vehicle in fake.records["vehicles"].values()]
        assert "inactive" in statuses
        await task.aclose()
    assert not any(fake.records.values())


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in NATIVE_ENV)
    or os.environ.get("FLEETBASE_EXCLUSIVE") != "1",
    reason="needs an exclusive native Fleetbase stack (FLEETBASE_URL etc.)",
)
async def test_live_native_matches_memory(
    trace: list[ToolCall], expected: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "native")
    assert await replay(FleetbaseDispatch(), trace) == expected


VEHICLES = (
    Vehicle("vehicle-1", "driver-1", 2, (0, 0)),
    Vehicle("vehicle-2", "driver-2", 4, (6, 0)),
)
REQUESTS = tuple(Request(f"r{i}", (i, 0), (i + 2, 1), 1, 0, 30, 50) for i in range(1, 5))


def event(kind: str, request_id: str = "", vehicle_id: str | None = None) -> PhysicalEvent:
    data: dict[str, JsonValue] = {"dropoff": [7.0, 3.5]} if kind == "dropoff_changed" else {}
    return PhysicalEvent(kind, 1.0, request_id, vehicle_id, data=data)


# Adapter calls, world changes and deliberate rejections, applied to both backends.
SCRIPT: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("dispatch_order", ("r1",)),  # uncreated
    *(("create_order", (request,)) for request in REQUESTS),
    ("dispatch_order", ("r1",)),  # unassigned
    ("assign_order", ("r1", "vehicle-9")),  # unknown vehicle
    ("assign_order", ("r1", "vehicle-1")),
    ("assign_order", ("r2", "vehicle-1")),
    ("dispatch_order", ("r1",)),
    ("dispatch_order", ("r1",)),  # repeat: no-op
    ("sync_events", ([event("breakdown", vehicle_id="vehicle-1"), event("unassigned", "r1")],)),
    ("dispatch_order", ("r1",)),  # unassigned by breakdown
    ("assign_order", ("r1", "vehicle-2")),
    ("dispatch_order", ("r1",)),  # natively dispatched earlier: clears the marker
    ("unassign_order", ("r2",)),
    ("assign_order", ("r2", "vehicle-2")),
    ("assign_order", ("r3", "vehicle-2")),
    ("set_route", ("vehicle-2", [("r3", "pickup"), ("r1", "pickup"), ("r3", "dropoff")])),
    ("set_route", ("vehicle-1", [("r3", "pickup")])),  # not this vehicle
    ("set_route", ("vehicle-2", [("r3", "teleport")])),  # unknown action
    ("update_order", ("r3", {"note": "fragile", "priority": "high"})),
    ("update_order", ("r3", {"status": "completed"})),  # not editable
    ("sync_events", ([event("repaired", vehicle_id="vehicle-1"), event("dropoff_changed", "r3")],)),
    ("dispatch_order", ("r3",)),
    ("sync_events", ([event("picked_up", "r1", "vehicle-2")],)),
    ("assign_order", ("r1", "vehicle-1")),  # after pickup
    ("unassign_order", ("r1",)),  # after pickup
    ("cancel_order", ("r1",)),  # after pickup
    ("update_order", ("r1", {"note": "on board"})),  # allowed after pickup
    ("cancel_order", ("r2",)),
    ("cancel_order", ("r2",)),  # repeat: no-op
    ("update_order", ("r2", {"note": "x"})),  # cancelled
    ("dispatch_order", ("r2",)),  # cancelled
    ("sync_events", ([event("customer_cancelled", "r3"), event("customer_cancelled", "r9")],)),
    ("assign_order", ("r4", "vehicle-2")),
    ("sync_events", ([event("shift_ended", vehicle_id="vehicle-2")],)),
    ("sync_events", ([event("delivered", "r1", "vehicle-2")],)),
    ("sync_events", ([event("delivered", "r1", "vehicle-2")],)),  # repeat: no-op
)


async def run_script(adapter: DispatchAdapter) -> list[JsonValue]:
    """Each step's outcome (rejected or not) and the committed state after it."""
    outcomes: list[JsonValue] = []
    await adapter.prepare(VEHICLES)
    for name, args in SCRIPT:
        try:
            await getattr(adapter, name)(*args)
            rejected = False
        except FleetbaseOperationError:
            rejected = True
        snapshot = await adapter.snapshot()
        orders = snapshot["orders"]
        assert isinstance(orders, dict)
        dispatched: dict[str, JsonValue] = {
            key: order.get("dispatched") for key, order in orders.items() if isinstance(order, dict)
        }
        outcomes.append([name, rejected, dict(await adapter.committed()), dispatched])
    await adapter.cleanup()
    return outcomes


@pytest.mark.parametrize("fail_every", [None, 3])
async def test_native_adapter_operations_match_memory_step_by_step(fail_every: int | None) -> None:
    expected = await run_script(InMemoryAdapter())
    assert sum(bool(step[1]) for step in expected if isinstance(step, list)) == 12
    fake = FakeFleetbase(fail_every)
    async with httpx.AsyncClient(
        base_url="http://fleetbase.invalid", transport=httpx.MockTransport(fake.handle)
    ) as client:
        lease = ResourceLease("episode", "exclusive", "exclusive", release_lease)
        native = FleetbaseAdapter(BackendEpisode(lease, client))
        assert await run_script(native) == expected
    assert not any(fake.records.values())
    if fail_every:
        assert fake.injected > 5


async def release_lease() -> None:
    return None

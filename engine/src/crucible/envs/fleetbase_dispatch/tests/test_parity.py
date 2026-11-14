"""Backend parity: one recorded tool trace gives equal physics on every backend.

The trace is the reference policy on seed 0 plus deliberate rejected calls. It is
replayed against:

- the in-memory backend (`fleetbase_sim@1` and `fleetbase_dispatch@1` with
  `FLEETBASE_BACKEND=memory`);
- `FleetbaseAdapter` over an offline fake of the audited native HTTP contract;
- a real native installation, only when `FLEETBASE_URL`, `FLEETBASE_TOKEN`,
  `FLEETBASE_DEPLOYMENT_ID` and `FLEETBASE_EXCLUSIVE=1` are set.

Physical events, economics and rejected-call counts must be equal.
"""

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import JsonValue

from crucible.backends import BackendEpisode, ResourceLease
from crucible.core.tools import Observation, ToolCall
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.adapter_protocol import DispatchAdapter
from crucible.envs.fleetbase_dispatch.policies import ReferenceOperator
from crucible.envs.fleetbase_dispatch.task import (
    BACKEND_ENV,
    DispatchTaskBase,
    FleetbaseDispatch,
    FleetbaseSim,
)

SEED = 0
NATIVE_ENV = ("FLEETBASE_URL", "FLEETBASE_TOKEN", "FLEETBASE_DEPLOYMENT_ID")
# Rejected before any state exists: unknown order, unreleased request, unknown vehicle,
# dispatch before assignment. The create in between commits and later replays idempotently.
PRELUDE = (
    ToolCall(name="dispatch_order", args={"request_id": "request-01"}),
    ToolCall(name="create_order", args={"request_id": "request-07"}),
    ToolCall(name="create_order", args={"request_id": "request-01"}),
    ToolCall(name="create_order", args={"request_id": "request-01"}),
    ToolCall(name="assign_order", args={"request_id": "request-01", "vehicle_id": "vehicle-9"}),
    ToolCall(name="dispatch_order", args={"request_id": "request-01"}),
)


class FakeFleetbase:
    """In-process HTTP fake of the audited native order contract (docs/fleetbase-api-audit.md)."""

    def __init__(self) -> None:
        self.flow = {"started": "enroute", "enroute": "completed"}
        self.records: dict[str, dict[str, dict[str, JsonValue]]] = {}
        self.count = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
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
        if request.method == "DELETE":
            del table[native]
            return httpx.Response(200, json={"deleted": True})
        if request.method == "PUT":
            record["driver_assigned"] = {"id": body["driver"]}
            record["vehicle_assigned"] = {"id": body["vehicle"]}
        elif action == "dispatch":
            if record["driver_assigned"] is None or record["dispatched"] is True:
                return httpx.Response(400, json={"error": "cannot dispatch"})
            record |= {"dispatched": True, "status": "dispatched"}
        elif action == "start":
            if record["dispatched"] is not True:
                return httpx.Response(400, json={"error": "not dispatched"})
            record |= {"started": True, "status": "started"}
        elif action == "next-activity":
            return httpx.Response(200, json=[{"code": self.flow[str(record["status"])]}])
        elif action == "update-activity":
            record["status"] = body["activity"]["code"]
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
        content = result.content
        if injected or not result.ok or not isinstance(content, dict):
            continue
        requests = content["requests"]
        assert isinstance(requests, list)
        picked = [r for r in requests if isinstance(r, dict) and r["status"] == "picked_up"]
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


@pytest.fixture
async def fake_client() -> AsyncIterator[tuple[httpx.AsyncClient, FakeFleetbase]]:
    fake = FakeFleetbase()
    async with httpx.AsyncClient(
        base_url="http://fleetbase.invalid", transport=httpx.MockTransport(fake.handle)
    ) as client:
        yield client, fake


async def test_native_adapter_on_fake_contract_matches_memory(
    trace: list[ToolCall],
    expected: dict[str, JsonValue],
    fake_client: tuple[httpx.AsyncClient, FakeFleetbase],
) -> None:
    client, fake = fake_client
    assert await replay(FakeNativeDispatch(client), trace) == expected
    assert not any(fake.records.values()), "native cleanup leaves no owned records"


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

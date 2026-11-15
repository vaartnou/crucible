"""Transport failure read-back and native workflow boundary checks."""

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import JsonValue

from crucible.backends import BackendEpisode, ResourceLease
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.deployment import make_bundle
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle


async def release() -> None:
    pass


@pytest.fixture
async def app() -> AsyncIterator[tuple[FleetbaseAdapter, list[str], dict[str, JsonValue]]]:
    records: dict[str, JsonValue] = {}
    calls: list[str] = []
    fail_assignment = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal fail_assignment
        import json

        calls.append(request.method + " " + request.url.path)
        resource = request.url.path.removeprefix("/v1/")
        path, _, native = resource.partition("/")
        if request.method == "GET" and not native:
            return httpx.Response(
                200, json=[value for key, value in records.items() if key.startswith(path)]
            )
        if request.method == "GET":
            return httpx.Response(200, json=records[resource])
        if request.method == "POST":
            data = json.loads(request.content)
            record: dict[str, JsonValue] = {"id": path + "_" + str(len(records)), **data}
            records[path + "/" + str(record["id"])] = record
            if path == "orders":
                raise httpx.ReadTimeout("response lost after commit", request=request)
            return httpx.Response(201, json=record)
        if request.method == "PUT":
            existing_record = records[resource]
            assert isinstance(existing_record, dict)
            record = existing_record
            data = json.loads(request.content)
            record["driver_assigned"] = {"id": data["driver"]}
            record["vehicle_assigned"] = {"id": data["vehicle"]}
            if fail_assignment:
                fail_assignment = False
                return httpx.Response(500, json={"error": "secret internal config"})
            return httpx.Response(200, json=record)
        if request.method == "DELETE":
            del records[resource]
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(request.method)

    async with httpx.AsyncClient(
        base_url="http://fleetbase.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        episode = BackendEpisode(
            ResourceLease("episode", "exclusive", "exclusive", release), client
        )
        yield FleetbaseAdapter(episode), calls, records


async def test_create_lost_response_reads_back_without_duplicate(
    app: tuple[FleetbaseAdapter, list[str], dict[str, JsonValue]],
) -> None:
    adapter, calls, _ = app
    request = Request("r1", (0, 0), (1, 1), 1, 0, 60, 100)
    first = await adapter.create_order(request)
    second = await adapter.create_order(request)
    assert first["id"] == second["id"]
    assert calls.count("POST /v1/orders") == 1
    assert calls[-1] == "GET /v1/orders"


async def test_assignment_500_reads_committed_state_and_cleanup_is_idempotent(
    app: tuple[FleetbaseAdapter, list[str], dict[str, JsonValue]],
) -> None:
    adapter, calls, records = app
    await adapter.prepare((Vehicle("v1", "d1", 4, (0, 0)),))
    await adapter.create_order(Request("r1", (0, 0), (1, 1), 1, 0, 60, 100))
    result = await adapter.assign_order("r1", "v1")
    assert isinstance(result["vehicle_assigned"], dict)
    assert sum(call.startswith("PUT /v1/orders/") for call in calls) == 1
    state = await adapter.committed()
    assert state["r1"]["vehicle_id"] == "v1"
    assert state["r1"]["dispatched"] is False
    await adapter.cleanup()
    count = len(calls)
    await adapter.cleanup()
    assert len(calls) == count
    assert records == {}


def test_bundle_is_pinned_private_and_generates_independent_credentials() -> None:
    first, second = make_bundle(), make_bundle()
    first.validate()
    assert (
        first.sidecar_environment["FLEETBASE_TOKEN"]
        != second.sidecar_environment["FLEETBASE_TOKEN"]
    )
    assert "FLEETBASE_TOKEN" not in first.files["Dockerfile"].decode()
    assert "scheduler" not in first.services
    assert "queue" not in first.services
    app = first.services["fleetbase-app"]
    env = app["environment"]
    assert isinstance(env, dict)
    assert env["QUEUE_CONNECTION"] == "sync"
    assert env["THROTTLE_ENABLED"] == "false"
    assert env["THROTTLE_TRACK_CONSUMERS"] == "false"


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_authorization_and_rate_limits_are_infrastructure_failures(status: int) -> None:
    from crucible.backends import BackendError

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "private backend details"})

    async with httpx.AsyncClient(
        base_url="http://fleetbase.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        adapter = FleetbaseAdapter(
            BackendEpisode(ResourceLease("episode", "exclusive", "exclusive", release), client)
        )
        with pytest.raises(BackendError) as caught:
            await adapter.prepare((Vehicle("v1", "d1", 4, (0, 0)),))
        assert type(caught.value) is BackendError
        assert "private backend details" not in str(caught.value)


async def test_snapshot_measures_duplicate_native_rows_and_cleanup_removes_owned_duplicates(
    app: tuple[FleetbaseAdapter, list[str], dict[str, JsonValue]],
) -> None:
    adapter, _, records = app
    await adapter.create_order(Request("r1", (0, 0), (1, 1), 1, 0, 60, 100))
    order_key = next(key for key in records if key.startswith("orders/"))
    original = records[order_key]
    assert isinstance(original, dict)
    records["orders/order_duplicate"] = {**original, "id": "order_duplicate"}
    snapshot = await adapter.snapshot()
    assert snapshot["duplicate_orders"] == 1
    await adapter.cleanup()
    assert records == {}

"""Native Fleetbase API operations and private verified-progress synchronization."""

from collections.abc import Callable
from typing import cast

import httpx
from pydantic import JsonValue, TypeAdapter

from crucible.backends import BackendEpisode, BackendError
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

_JSON = TypeAdapter(JsonValue)


class FleetbaseOperationError(BackendError):
    """A rejected native operation; public messages contain no backend payloads."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FleetbaseAdapter:
    def __init__(self, episode: BackendEpisode) -> None:
        self._client = episode.client
        self._prefix = "crucible-" + episode.resource.episode_id + "-"
        self._orders: dict[str, str] = {}
        self._drivers: dict[str, str] = {}
        self._vehicles: dict[str, str] = {}
        self._places: dict[str, str] = {}
        self._synced: set[tuple[str, str]] = set()
        self._closed = False

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None = None,
        *,
        key: str | None = None,
    ) -> JsonValue:
        headers = {"Idempotency-Key": key} if key is not None else {}
        try:
            response = await self._client.request(method, "v1/" + path, json=body, headers=headers)
        except httpx.HTTPError:
            raise BackendError("Fleetbase connection failed") from None
        if not response.is_success:
            if response.status_code in {401, 403, 408, 425, 429}:
                raise BackendError(
                    "Fleetbase infrastructure authorization, timeout or rate-limit failure"
                )
            if response.status_code < 500:
                raise FleetbaseOperationError(
                    f"Fleetbase rejected operation (HTTP {response.status_code})",
                    response.status_code,
                )
            raise BackendError(
                "Fleetbase mutation or service failed; committed state requires read-back"
            )
        try:
            return _JSON.validate_python(response.json())
        except ValueError, TypeError:
            raise BackendError("Fleetbase returned invalid JSON") from None

    @staticmethod
    def _record(value: JsonValue) -> dict[str, JsonValue]:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise BackendError("Fleetbase returned an invalid resource")
        return value

    async def _list(self, path: str) -> list[dict[str, JsonValue]]:
        result: list[dict[str, JsonValue]] = []
        for offset in range(0, 10000, 100):
            page = await self._request("GET", f"{path}?limit=100&offset={offset}")
            if isinstance(page, dict):
                page = page.get("data")
            if not isinstance(page, list):
                raise BackendError("Fleetbase returned an invalid collection")
            result.extend(self._record(item) for item in page)
            if len(page) < 100:
                return result
        raise BackendError("Fleetbase collection exceeds audited pagination limit")

    async def _find(self, path: str, internal_id: str) -> dict[str, JsonValue] | None:
        field = "name" if path == "places" else "internal_id"
        expected = internal_id.upper() if path == "places" else internal_id
        found = [item for item in await self._list(path) if item.get(field) == expected]
        if len(found) > 1:
            raise BackendError("Duplicate native records for an episode resource")
        return found[0] if found else None

    async def _create(
        self,
        path: str,
        body: dict[str, JsonValue],
        internal_id: str,
    ) -> dict[str, JsonValue]:
        existing = await self._find(path, internal_id)
        if existing is not None:
            return existing
        try:
            return self._record(await self._request("POST", path, body, key=internal_id))
        except BackendError:
            # Includes a response lost after commit and a 500 after notification failures.
            # No second mutation is sent until native state has been read back.
            existing = await self._find(path, internal_id)
            if existing is not None:
                return existing
            raise

    async def prepare(self, vehicles: tuple[Vehicle, ...]) -> None:
        for vehicle in vehicles:
            location: dict[str, JsonValue] = {
                "type": "Point",
                "coordinates": [
                    24.94 + vehicle.position[0] * 0.001,
                    60.17 + vehicle.position[1] * 0.001,
                ],
            }
            driver_id = self._prefix + vehicle.driver_id
            driver = await self._create(
                "drivers",
                {
                    "internal_id": driver_id,
                    "name": "Crucible " + vehicle.driver_id,
                    "status": "active",
                    "location": location,
                },
                driver_id,
            )
            self._drivers[vehicle.id] = cast(str, driver["id"])
            native_id = self._prefix + vehicle.id
            native = await self._create(
                "vehicles",
                {
                    "internal_id": native_id,
                    "name": "Crucible " + vehicle.id,
                    "plate_number": native_id,
                    "status": "available",
                    "location": location,
                },
                native_id,
            )
            self._vehicles[vehicle.id] = cast(str, native["id"])

    async def create_order(self, request: Request) -> dict[str, JsonValue]:
        internal_id = self._prefix + request.id

        # Scenario coordinates are local distance units, mapped deterministically to
        # valid coordinates for application records. Economics use simulator geometry.
        def place(point: tuple[float, float], name: str) -> dict[str, JsonValue]:
            return {
                "street1": name,
                "city": "Helsinki",
                "country": "FI",
                "latitude": 60.17 + point[1] * 0.001,
                "longitude": 24.94 + point[0] * 0.001,
            }

        pickup = place(request.pickup, "Crucible pickup " + internal_id)
        dropoff = place(request.dropoff, "Crucible destination " + internal_id)
        native_places: list[str] = []
        for index, data in enumerate((pickup, dropoff)):
            stable_name = f"{internal_id}-place-{index}".upper()
            data["name"] = stable_name
            record = await self._create("places", data, stable_name)
            native_place = cast(str, record["id"])
            self._places[native_place] = native_place
            native_places.append(native_place)
        order = await self._create(
            "orders",
            {
                "internal_id": internal_id,
                "type": "transport",
                "pickup": native_places[0],
                "dropoff": native_places[1],
                "meta": {"crucible_request_id": request.id, "crucible_episode": self._prefix},
                "pod_required": False,
                "adhoc": False,
            },
            internal_id,
        )
        self._orders[request.id] = cast(str, order["id"])
        payload = order.get("payload")
        if isinstance(payload, dict):
            for name in ("pickup", "dropoff"):
                place_record = payload.get(name)
                if isinstance(place_record, dict) and isinstance(place_record.get("id"), str):
                    native_place = cast(str, place_record["id"])
                    self._places[native_place] = native_place
        return order

    async def _read_order(self, request_id: str) -> dict[str, JsonValue]:
        native = self._orders.get(request_id)
        if native is None:
            raise FleetbaseOperationError("Create the request's native order first")
        return self._record(await self._request("GET", "orders/" + native))

    async def _mutate(
        self,
        request_id: str,
        method: str,
        suffix: str,
        body: dict[str, JsonValue] | None,
        committed: Callable[[dict[str, JsonValue]], bool],
    ) -> dict[str, JsonValue]:
        current = await self._read_order(request_id)
        if committed(current):
            return current
        try:
            result = self._record(
                await self._request(method, "orders/" + self._orders[request_id] + suffix, body)
            )
        except BackendError:
            current = await self._read_order(request_id)
            if committed(current):
                return current
            raise
        if not committed(result):
            result = await self._read_order(request_id)
            if not committed(result):
                raise BackendError("Fleetbase mutation did not commit requested state")
        return result

    async def assign_order(self, request_id: str, vehicle_id: str) -> dict[str, JsonValue]:
        if vehicle_id not in self._vehicles:
            raise FleetbaseOperationError("Unknown vehicle")
        driver, vehicle = self._drivers[vehicle_id], self._vehicles[vehicle_id]

        def matches(order: dict[str, JsonValue]) -> bool:
            assigned_driver, assigned_vehicle = (
                order.get("driver_assigned"),
                order.get("vehicle_assigned"),
            )
            return (
                isinstance(assigned_driver, dict)
                and assigned_driver.get("id") == driver
                and isinstance(assigned_vehicle, dict)
                and assigned_vehicle.get("id") == vehicle
            )

        return await self._mutate(
            request_id, "PUT", "", {"driver": driver, "vehicle": vehicle}, matches
        )

    async def dispatch_order(self, request_id: str) -> dict[str, JsonValue]:
        return await self._mutate(
            request_id, "PATCH", "/dispatch", None, lambda item: item.get("dispatched") is True
        )

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        result: dict[str, dict[str, JsonValue]] = {}
        reverse = {native: key for key, native in self._vehicles.items()}
        for request_id in self._orders:
            order = await self._read_order(request_id)
            vehicle = order.get("vehicle_assigned")
            driver = order.get("driver_assigned")
            native_vehicle = vehicle.get("id") if isinstance(vehicle, dict) else None
            vehicle_id = reverse.get(native_vehicle) if isinstance(native_vehicle, str) else None
            valid_driver = (
                isinstance(driver, dict)
                and vehicle_id is not None
                and driver.get("id") == self._drivers[vehicle_id]
            )
            result[request_id] = {
                "vehicle_id": vehicle_id if valid_driver else None,
                "dispatched": order.get("dispatched") is True,
            }
        return result

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        for event in events:
            key = (event.request_id, event.kind)
            if key in self._synced or event.kind not in {"picked_up", "delivered", "late"}:
                continue
            request_id = event.request_id
            order = await self._read_order(request_id)
            if order.get("status") != "completed":
                order = await self._mutate(
                    request_id, "POST", "/start", None, lambda item: item.get("started") is True
                )
                target = "enroute" if event.kind == "picked_up" else "completed"
                for _ in range(5):
                    order = await self._read_order(request_id)
                    if order.get("status") in {target, "completed"}:
                        break
                    activities = await self._request(
                        "GET", "orders/" + self._orders[request_id] + "/next-activity"
                    )
                    if (
                        not isinstance(activities, list)
                        or not activities
                        or not isinstance(activities[0], dict)
                    ):
                        raise BackendError("Fleetbase has no legal next activity")
                    activity = activities[0]
                    code = activity.get("code")
                    await self._mutate(
                        request_id,
                        "POST",
                        "/update-activity",
                        {"activity": activity},
                        lambda item, code=code: item.get("status") == code,
                    )
                else:
                    raise BackendError("Fleetbase activity synchronization did not settle")
            self._synced.add(key)

    async def snapshot(self) -> dict[str, JsonValue]:
        orders: dict[str, JsonValue] = {}
        for request_id in self._orders:
            order = await self._read_order(request_id)
            orders[request_id] = {
                key: order.get(key)
                for key in (
                    "id",
                    "internal_id",
                    "status",
                    "dispatched",
                    "started",
                    "driver_assigned",
                    "vehicle_assigned",
                    "meta",
                    "payload",
                )
            }
        native_counts: dict[str, int] = {}
        for record in await self._list("orders"):
            internal = record.get("internal_id")
            if isinstance(internal, str) and internal.startswith(self._prefix):
                native_counts[internal] = native_counts.get(internal, 0) + 1
        return {
            "orders": orders,
            "vehicles": dict(self._vehicles),
            "drivers": dict(self._drivers),
            "duplicate_orders": sum(max(0, count - 1) for count in native_counts.values()),
        }

    async def cleanup(self) -> None:
        if self._closed:
            return
        # Native deletes only owned records; never reset an attached installation.
        failures = False
        resources = (
            ("orders", self._orders),
            ("places", self._places),
            ("vehicles", self._vehicles),
            ("drivers", self._drivers),
        )
        # Recover ownership after partial startup or a duplicate native creation.
        # This also finds rows committed before a response could be recorded.
        for resource, mapping in resources:
            try:
                for record in await self._list(resource):
                    identity = record.get("name" if resource == "places" else "internal_id")
                    prefix = self._prefix.upper() if resource == "places" else self._prefix
                    native = record.get("id")
                    if (
                        isinstance(identity, str)
                        and identity.startswith(prefix)
                        and isinstance(native, str)
                        and native not in mapping.values()
                    ):
                        mapping[native] = native
            except BackendError:
                failures = True
        for resource, mapping in (
            ("orders", self._orders),
            ("places", self._places),
            ("vehicles", self._vehicles),
            ("drivers", self._drivers),
        ):
            for key, native in list(mapping.items()):
                try:
                    await self._request("DELETE", resource + "/" + native)
                except FleetbaseOperationError as exc:
                    if exc.status_code == 404:
                        del mapping[key]
                    else:
                        failures = True
                except BackendError:
                    failures = True
                else:
                    del mapping[key]
        self._closed = not failures
        if failures:
            raise BackendError("Fleetbase episode resource cleanup failed")

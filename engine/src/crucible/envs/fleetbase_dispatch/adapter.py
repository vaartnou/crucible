"""Native Fleetbase API operations and private verified-progress synchronization.

Every mutation follows one discipline: read the record, send nothing if the requested
state is already committed, and after any ambiguous failure (lost response, HTTP 500)
read the record back before deciding. A mutation is never resubmitted blind.

v2 state that native Fleetbase does not model lives in the order's ``meta``
(see ``docs/fleetbase-api-audit.md``, "v2 operations"):

- ``crucible_route``: ``{"pickup": rank, "dropoff": rank}`` or ``null``. Fleetbase has
  no cross-order stop sequencing for a driver, so route ranks are meta only.
- ``crucible_undispatched``: ``true`` after an unassign of a dispatched order. Native
  Fleetbase has no un-dispatch; the effective dispatch flag is native ``dispatched``
  without this marker. A later dispatch clears the marker instead of a second native
  dispatch (which Fleetbase rejects).
- ``note``, ``priority``, ``cancelled_by`` and ``dropoff``, as in the memory backend.

Keys are cleared by writing ``null``, so the result is the same whether the native
``PUT`` replaces or merges ``meta``. Guards that native Fleetbase may not enforce
(changes after pickup, changes to a cancelled order) are checked here, as in the memory
backend, so both backends reject the same calls.
"""

from collections.abc import Callable
from typing import cast

import httpx
from pydantic import JsonValue, TypeAdapter

from crucible.backends import BackendEpisode, BackendError
from crucible.envs.fleetbase_dispatch.adapter_protocol import FleetbaseOperationError
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

_JSON = TypeAdapter(JsonValue)
_PROGRESS = frozenset({"picked_up", "delivered", "delivered_late"})
_V2_CHANGES = frozenset({"unassigned", "customer_cancelled", "dropoff_changed"})
_VEHICLE_EVENTS = frozenset({"breakdown", "repaired", "shift_ended"})
_EDITABLE = frozenset({"note", "priority"})
CANCELLED = "canceled"  # native spelling
# Native vehicle status is a free string; these values are this adapter's convention.
VEHICLE_AVAILABLE, VEHICLE_BROKEN, VEHICLE_OFF_SHIFT = "available", "out_of_service", "inactive"

__all__ = ["FleetbaseAdapter", "FleetbaseOperationError"]

Predicate = Callable[[dict[str, JsonValue]], bool]


def _coordinates(point: tuple[float, float] | list[float]) -> tuple[float, float]:
    """Scenario distance units to deterministic Helsinki (latitude, longitude)."""
    return 60.17 + point[1] * 0.001, 24.94 + point[0] * 0.001


def _place_at(record: dict[str, JsonValue], latitude: float, longitude: float) -> bool:
    """Whether a native place record is at the coordinates (either serialization)."""
    lat, lon = record.get("latitude"), record.get("longitude")
    location = record.get("location")
    if isinstance(location, dict):
        coordinates = location.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) == 2:
            lon, lat = coordinates
    return (
        isinstance(lat, int | float)
        and isinstance(lon, int | float)
        and abs(lat - latitude) < 1e-7
        and abs(lon - longitude) < 1e-7
    )


class FleetbaseAdapter:
    """Native implementation of ``DispatchAdapter``."""

    def __init__(self, episode: BackendEpisode) -> None:
        self._client = episode.client
        self._prefix = "crucible-" + episode.resource.episode_id + "-"
        self._orders: dict[str, str] = {}
        self._drivers: dict[str, str] = {}
        self._vehicles: dict[str, str] = {}
        self._places: dict[str, str] = {}
        self._order_places: dict[str, tuple[str, str]] = {}
        self._synced: set[tuple[str, str]] = set()
        self._picked_up: set[str] = set()
        self._vehicle_flags: dict[str, set[str]] = {}
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

    async def _read(self, path: str) -> dict[str, JsonValue]:
        return self._record(await self._request("GET", path))

    async def _mutate_record(
        self,
        path: str,
        method: str,
        target: str,
        body: dict[str, JsonValue] | None,
        committed: Predicate,
        current: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        """Send one mutation to ``target`` unless ``path``'s record already satisfies it."""
        if current is None:
            current = await self._read(path)
        if committed(current):
            return current
        try:
            # A response that is not the record (for example a status envelope) is
            # ambiguous too and is settled by read-back.
            result = self._record(await self._request(method, target, body))
        except BackendError:
            current = await self._read(path)
            if committed(current):
                return current
            raise
        if not committed(result):
            result = await self._read(path)
            if not committed(result):
                raise BackendError("Fleetbase mutation did not commit requested state")
        return result

    async def prepare(self, vehicles: tuple[Vehicle, ...]) -> None:
        for vehicle in vehicles:
            latitude, longitude = _coordinates(vehicle.position)
            location: dict[str, JsonValue] = {
                "type": "Point",
                "coordinates": [longitude, latitude],
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
                    "status": VEHICLE_AVAILABLE,
                    "location": location,
                },
                native_id,
            )
            self._vehicles[vehicle.id] = cast(str, native["id"])
            self._vehicle_flags.setdefault(vehicle.id, set())

    async def create_order(self, request: Request) -> dict[str, JsonValue]:
        internal_id = self._prefix + request.id

        # Scenario coordinates are local distance units, mapped deterministically to
        # valid coordinates for application records. Economics use simulator geometry.
        def place(point: tuple[float, float], name: str) -> dict[str, JsonValue]:
            latitude, longitude = _coordinates(point)
            return {
                "street1": name,
                "city": "Helsinki",
                "country": "FI",
                "latitude": latitude,
                "longitude": longitude,
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
        self._order_places[request.id] = (native_places[0], native_places[1])
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

    # Order helpers -------------------------------------------------------------------

    async def _read_order(self, request_id: str) -> dict[str, JsonValue]:
        native = self._orders.get(request_id)
        if native is None:
            raise FleetbaseOperationError("Create the request's native order first")
        return await self._read("orders/" + native)

    async def _open_order(self, request_id: str) -> dict[str, JsonValue]:
        order = await self._read_order(request_id)
        if order.get("status") == CANCELLED:
            raise FleetbaseOperationError("Order is cancelled")
        return order

    async def _mutate(
        self,
        request_id: str,
        method: str,
        suffix: str,
        body: dict[str, JsonValue] | None,
        committed: Predicate,
        current: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        if current is None:
            current = await self._read_order(request_id)
        path = "orders/" + self._orders[request_id]
        return await self._mutate_record(path, method, path + suffix, body, committed, current)

    @staticmethod
    def _meta(order: dict[str, JsonValue]) -> dict[str, JsonValue]:
        meta = order.get("meta")
        return dict(meta) if isinstance(meta, dict) else {}

    async def _update_order(
        self,
        request_id: str,
        order: dict[str, JsonValue],
        changes: dict[str, JsonValue],
        fields: dict[str, JsonValue] | None = None,
        also: Predicate | None = None,
    ) -> dict[str, JsonValue]:
        """``PUT`` meta ``changes`` (the full merged meta) plus native ``fields``."""

        def done(item: dict[str, JsonValue]) -> bool:
            meta = self._meta(item)
            return all(meta.get(key) == value for key, value in changes.items()) and (
                also is None or also(item)
            )

        body: dict[str, JsonValue] = {**(fields or {}), "meta": self._meta(order) | changes}
        return await self._mutate(request_id, "PUT", "", body, done, order)

    def _vehicle_of(self, order: dict[str, JsonValue]) -> str | None:
        """The vehicle whose native vehicle and driver are both assigned to the order."""
        vehicle, driver = order.get("vehicle_assigned"), order.get("driver_assigned")
        native = vehicle.get("id") if isinstance(vehicle, dict) else None
        reverse = {value: key for key, value in self._vehicles.items()}
        vehicle_id = reverse.get(native) if isinstance(native, str) else None
        if (
            vehicle_id is None
            or not isinstance(driver, dict)
            or driver.get("id") != self._drivers[vehicle_id]
        ):
            return None
        return vehicle_id

    def _dispatched(self, order: dict[str, JsonValue]) -> bool:
        return (
            order.get("dispatched") is True
            and self._meta(order).get("crucible_undispatched") is not True
        )

    @staticmethod
    def _unassigned(order: dict[str, JsonValue]) -> bool:
        return not isinstance(order.get("driver_assigned"), dict) and not isinstance(
            order.get("vehicle_assigned"), dict
        )

    async def _unassign(self, request_id: str, order: dict[str, JsonValue]) -> dict[str, JsonValue]:
        changes: dict[str, JsonValue] = {"crucible_route": None}
        if order.get("dispatched") is True:
            changes["crucible_undispatched"] = True
        return await self._update_order(
            request_id,
            order,
            changes,
            {"driver": None, "vehicle": None},
            self._unassigned,
        )

    async def _cancel(self, request_id: str, order: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await self._mutate(
            request_id,
            "DELETE",
            "/cancel",
            None,
            lambda item: item.get("status") == CANCELLED,
            order,
        )

    # Agent operations ----------------------------------------------------------------

    async def assign_order(self, request_id: str, vehicle_id: str) -> dict[str, JsonValue]:
        if vehicle_id not in self._vehicles:
            raise FleetbaseOperationError("Unknown vehicle")
        order = await self._open_order(request_id)
        if self._vehicle_of(order) == vehicle_id:
            return order
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        top = -1
        for other_id, commit in (await self.committed()).items():
            if other_id != request_id and commit["vehicle_id"] == vehicle_id:
                for key in ("pickup_rank", "dropoff_rank"):
                    rank = commit[key]
                    if isinstance(rank, int):
                        top = max(top, rank)
        route: dict[str, JsonValue] = {"pickup": top + 1, "dropoff": top + 2}
        driver, vehicle = self._drivers[vehicle_id], self._vehicles[vehicle_id]
        return await self._update_order(
            request_id,
            order,
            {"crucible_route": route},
            {"driver": driver, "vehicle": vehicle},
            lambda item: self._vehicle_of(item) == vehicle_id,
        )

    async def dispatch_order(self, request_id: str) -> dict[str, JsonValue]:
        order = await self._open_order(request_id)
        if self._dispatched(order):
            return order
        if order.get("dispatched") is True:
            # Natively dispatched before an unassign: re-dispatch clears the marker.
            if not isinstance(order.get("driver_assigned"), dict):
                raise FleetbaseOperationError("Assign a driver before dispatch")
            return await self._update_order(request_id, order, {"crucible_undispatched": None})
        return await self._mutate(request_id, "PATCH", "/dispatch", None, self._dispatched, order)

    async def cancel_order(self, request_id: str) -> dict[str, JsonValue]:
        order = await self._read_order(request_id)
        if order.get("status") == CANCELLED:
            return order
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        return await self._cancel(request_id, order)

    async def update_order(
        self, request_id: str, fields: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        order = await self._open_order(request_id)
        if set(fields) - _EDITABLE:
            raise FleetbaseOperationError("Only note and priority can be updated")
        return await self._update_order(request_id, order, dict(fields))

    async def unassign_order(self, request_id: str) -> dict[str, JsonValue]:
        order = await self._open_order(request_id)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        return await self._unassign(request_id, order)

    async def set_route(self, vehicle_id: str, stops: list[tuple[str, str]]) -> None:
        if vehicle_id not in self._vehicles:
            raise FleetbaseOperationError("Unknown vehicle")
        orders: dict[str, dict[str, JsonValue]] = {}
        ranks: dict[str, dict[str, JsonValue]] = {}
        # Validate every stop before the first write, like the memory backend.
        for rank, (request_id, action) in enumerate(stops):
            if request_id not in orders:
                orders[request_id] = await self._open_order(request_id)
            if self._vehicle_of(orders[request_id]) != vehicle_id:
                raise FleetbaseOperationError("Order is not assigned to this vehicle")
            if action not in {"pickup", "dropoff"}:
                raise FleetbaseOperationError("Unknown stop action")
            ranks.setdefault(request_id, {})[action] = rank
        for request_id, route in ranks.items():
            order = orders[request_id]
            previous = self._meta(order).get("crucible_route")
            merged: dict[str, JsonValue] = dict(previous) if isinstance(previous, dict) else {}
            merged.update(route)
            await self._update_order(request_id, order, {"crucible_route": merged})

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        result: dict[str, dict[str, JsonValue]] = {}
        for request_id in self._orders:
            order = await self._read_order(request_id)
            cancelled = order.get("status") == CANCELLED
            route = self._meta(order).get("crucible_route")
            route = route if isinstance(route, dict) and not cancelled else {}
            pickup, dropoff = route.get("pickup"), route.get("dropoff")
            result[request_id] = {
                "vehicle_id": None if cancelled else self._vehicle_of(order),
                "dispatched": self._dispatched(order),
                "cancelled": cancelled,
                "pickup_rank": pickup if isinstance(pickup, int) else None,
                "dropoff_rank": dropoff if isinstance(dropoff, int) else None,
            }
        return result

    # World synchronization -----------------------------------------------------------

    async def _sync_vehicle(self, event: PhysicalEvent) -> None:
        vehicle_id = event.vehicle_id
        if vehicle_id is None or vehicle_id not in self._vehicles:
            return
        flags = self._vehicle_flags.setdefault(vehicle_id, set())
        if event.kind == "breakdown":
            flags.add("broken")
        elif event.kind == "repaired":
            flags.discard("broken")
        else:
            flags.add("off_shift")
        status = (
            VEHICLE_OFF_SHIFT
            if "off_shift" in flags
            else VEHICLE_BROKEN
            if "broken" in flags
            else VEHICLE_AVAILABLE
        )
        path = "vehicles/" + self._vehicles[vehicle_id]
        await self._mutate_record(
            path, "PUT", path, {"status": status}, lambda item: item.get("status") == status
        )

    async def _sync_change(self, event: PhysicalEvent) -> None:
        request_id = event.request_id
        order = await self._read_order(request_id)
        if event.kind == "unassigned":
            await self._unassign(request_id, order)
        elif event.kind == "customer_cancelled":
            if order.get("status") != CANCELLED:
                order = await self._update_order(
                    request_id, order, {"cancelled_by": "customer", "crucible_route": None}
                )
                await self._cancel(request_id, order)
        else:
            point = event.data.get("dropoff")
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(value, int | float) for value in point)
            ):
                raise BackendError("Invalid dropoff change")
            latitude, longitude = _coordinates(cast(list[float], point))
            native_place = self._order_places[request_id][1]
            path = "places/" + native_place
            await self._mutate_record(
                path,
                "PUT",
                path,
                {"latitude": latitude, "longitude": longitude},
                lambda item: _place_at(item, latitude, longitude),
            )
            await self._update_order(request_id, order, {"dropoff": list(point)})

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        for event in events:
            if event.kind in _VEHICLE_EVENTS:
                await self._sync_vehicle(event)
                continue
            if event.kind in _V2_CHANGES:
                if event.request_id not in self._orders:
                    continue
                try:
                    await self._sync_change(event)
                except FleetbaseOperationError:
                    # A verified world change is not the agent's operation to reject.
                    raise BackendError("Fleetbase rejected a world record change") from None
                continue
            key = (event.request_id, event.kind)
            if key in self._synced or event.kind not in _PROGRESS:
                continue
            request_id = event.request_id
            order = await self._read_order(request_id)
            self._picked_up.add(request_id)
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
            record: dict[str, JsonValue] = {
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
            # The effective commitment: native dispatch without the unassign marker.
            record["dispatched"] = self._dispatched(order)
            orders[request_id] = record
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

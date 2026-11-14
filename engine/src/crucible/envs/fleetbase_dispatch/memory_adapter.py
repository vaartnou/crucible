"""Deterministic in-memory ``DispatchAdapter``: no HTTP, no application services.

It mirrors what ``FleetbaseAdapter`` returns and rejects, as far as the task uses it:

- ``create_order`` is idempotent, like native: the native adapter looks up the
  stable ``internal_id`` first and returns the existing order without a second
  POST. A repeated create therefore returns the existing record and is not an
  error, also for a cancelled order.
- ``assign_order`` with the vehicle that is already assigned, and
  ``dispatch_order`` on a dispatched order, return the record unchanged. The
  native adapter reads the order first and sends no mutation when the requested
  state is already committed.
- Rejections (``FleetbaseOperationError``): unknown vehicle, unknown (uncreated)
  order, cancelled order, dispatch before assignment (native: "assigned driver
  required"), and reassignment to another vehicle after pickup. The adapter learns
  about pickup only through ``sync_events``, like native.

Order statuses follow the native transport flow
``created -> dispatched -> enroute -> completed``; ``canceled`` uses the native
spelling. Native IDs are deterministic counters, so observations are reproducible.
"""

import copy

from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.adapter_protocol import FleetbaseOperationError
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

_PROGRESS = frozenset({"picked_up", "delivered", "late"})


class InMemoryAdapter:
    """In-memory implementation of ``DispatchAdapter``."""

    def __init__(self, episode_id: str = "memory") -> None:
        self._prefix = "crucible-" + episode_id + "-"
        self._counters: dict[str, int] = {}
        self._drivers: dict[str, str] = {}
        self._vehicles: dict[str, str] = {}
        self._orders: dict[str, dict[str, JsonValue]] = {}
        self._picked_up: set[str] = set()
        self._cancelled: set[str] = set()
        self._synced: set[tuple[str, str]] = set()

    def _new_id(self, kind: str) -> str:
        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        return f"{kind}_{count:04d}"

    def _order(self, request_id: str) -> dict[str, JsonValue]:
        order = self._orders.get(request_id)
        if order is None:
            raise FleetbaseOperationError("Create the request's native order first")
        return order

    def _open_order(self, request_id: str) -> dict[str, JsonValue]:
        order = self._order(request_id)
        if request_id in self._cancelled:
            raise FleetbaseOperationError("Order is cancelled")
        return order

    @staticmethod
    def _assigned(order: dict[str, JsonValue], key: str) -> str | None:
        value = order.get(key)
        if isinstance(value, dict):
            native = value.get("id")
            return native if isinstance(native, str) else None
        return None

    async def prepare(self, vehicles: tuple[Vehicle, ...]) -> None:
        for vehicle in vehicles:
            if vehicle.id not in self._drivers:
                self._drivers[vehicle.id] = self._new_id("driver")
            if vehicle.id not in self._vehicles:
                self._vehicles[vehicle.id] = self._new_id("vehicle")

    async def create_order(self, request: Request) -> dict[str, JsonValue]:
        existing = self._orders.get(request.id)
        if existing is not None:
            return copy.deepcopy(existing)
        internal_id = self._prefix + request.id
        pickup, dropoff = self._new_id("place"), self._new_id("place")
        order: dict[str, JsonValue] = {
            "id": self._new_id("order"),
            "internal_id": internal_id,
            "status": "created",
            "dispatched": False,
            "started": False,
            "driver_assigned": None,
            "vehicle_assigned": None,
            "meta": {"crucible_request_id": request.id, "crucible_episode": self._prefix},
            "payload": {
                "pickup": {"id": pickup, "name": f"{internal_id}-place-0".upper()},
                "dropoff": {"id": dropoff, "name": f"{internal_id}-place-1".upper()},
            },
        }
        self._orders[request.id] = order
        return copy.deepcopy(order)

    async def assign_order(self, request_id: str, vehicle_id: str) -> dict[str, JsonValue]:
        if vehicle_id not in self._vehicles:
            raise FleetbaseOperationError("Unknown vehicle")
        order = self._open_order(request_id)
        driver, vehicle = self._drivers[vehicle_id], self._vehicles[vehicle_id]
        if (
            self._assigned(order, "driver_assigned") == driver
            and self._assigned(order, "vehicle_assigned") == vehicle
        ):
            return copy.deepcopy(order)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        order["driver_assigned"] = {"id": driver}
        order["vehicle_assigned"] = {"id": vehicle}
        return copy.deepcopy(order)

    async def dispatch_order(self, request_id: str) -> dict[str, JsonValue]:
        order = self._open_order(request_id)
        if order.get("dispatched") is True:
            return copy.deepcopy(order)
        if self._assigned(order, "driver_assigned") is None:
            raise FleetbaseOperationError("Assign a driver before dispatch")
        order["dispatched"] = True
        order["status"] = "dispatched"
        return copy.deepcopy(order)

    async def cancel_order(self, request_id: str) -> dict[str, JsonValue]:
        """Cancel an order before pickup. Not a public tool yet (plan step 2/3)."""
        order = self._order(request_id)
        if request_id in self._cancelled:
            return copy.deepcopy(order)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        self._cancelled.add(request_id)
        order["status"] = "canceled"
        return copy.deepcopy(order)

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        reverse = {native: key for key, native in self._vehicles.items()}
        result: dict[str, dict[str, JsonValue]] = {}
        for request_id, order in self._orders.items():
            if request_id in self._cancelled:
                result[request_id] = {"vehicle_id": None, "dispatched": False}
                continue
            native_vehicle = self._assigned(order, "vehicle_assigned")
            vehicle_id = reverse.get(native_vehicle) if native_vehicle is not None else None
            valid_driver = (
                vehicle_id is not None
                and self._assigned(order, "driver_assigned") == self._drivers[vehicle_id]
            )
            result[request_id] = {
                "vehicle_id": vehicle_id if valid_driver else None,
                "dispatched": order.get("dispatched") is True,
            }
        return result

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        for event in events:
            key = (event.request_id, event.kind)
            if key in self._synced or event.kind not in _PROGRESS:
                continue
            order = self._order(event.request_id)
            self._picked_up.add(event.request_id)
            if order.get("status") != "completed":
                order["started"] = True
                order["status"] = "enroute" if event.kind == "picked_up" else "completed"
            self._synced.add(key)

    async def snapshot(self) -> dict[str, JsonValue]:
        orders: dict[str, JsonValue] = {
            request_id: copy.deepcopy(order) for request_id, order in self._orders.items()
        }
        return {
            "orders": orders,
            "vehicles": dict(self._vehicles),
            "drivers": dict(self._drivers),
            "duplicate_orders": 0,
        }

    async def cleanup(self) -> None:
        self._orders.clear()
        self._drivers.clear()
        self._vehicles.clear()
        self._picked_up.clear()
        self._cancelled.clear()
        self._synced.clear()


__all__ = ["InMemoryAdapter"]

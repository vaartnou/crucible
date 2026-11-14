"""Deterministic in-memory ``DispatchAdapter``: no HTTP, no application services.

It mirrors what ``FleetbaseAdapter`` returns and rejects, as far as the task uses it:

- ``create_order`` is idempotent, like native: the native adapter looks up the
  stable ``internal_id`` first and returns the existing order without a second
  POST. A repeated create therefore returns the existing record and is not an
  error, also for a cancelled order.
- ``assign_order`` with the vehicle that is already assigned, ``dispatch_order`` on a
  dispatched order and ``cancel_order`` on a cancelled order return the record
  unchanged. The native adapter reads the order first and sends no mutation when the
  requested state is already committed.
- Rejections (``FleetbaseOperationError``): unknown vehicle, unknown (uncreated)
  order, cancelled order, dispatch before assignment, and any change of assignment,
  route or cancellation after pickup. The adapter learns about pickup only through
  ``sync_events``, like native.
- A vehicle can hold many orders. Each order's route position is kept in its meta
  (``crucible_route``: ``{"pickup": rank, "dropoff": rank}``). Assigning to a new
  vehicle appends both stops after the vehicle's highest rank; ``set_route`` rewrites
  the ranks of the listed stops.

Order statuses follow the native transport flow
``created -> dispatched -> enroute -> completed``; ``canceled`` uses the native
spelling. Native IDs are deterministic counters, so observations are reproducible.
"""

import copy

from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.adapter_protocol import FleetbaseOperationError
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle

_PROGRESS = frozenset({"picked_up", "delivered", "delivered_late"})
_EDITABLE = frozenset({"note", "priority"})


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

    def _before_pickup(self, request_id: str) -> dict[str, JsonValue]:
        order = self._open_order(request_id)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        return order

    @staticmethod
    def _assigned(order: dict[str, JsonValue], key: str) -> str | None:
        value = order.get(key)
        if isinstance(value, dict):
            native = value.get("id")
            return native if isinstance(native, str) else None
        return None

    @staticmethod
    def _meta(order: dict[str, JsonValue]) -> dict[str, JsonValue]:
        meta = order.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            order["meta"] = meta
        return meta

    def _vehicle_of(self, order: dict[str, JsonValue]) -> str | None:
        native = self._assigned(order, "vehicle_assigned")
        reverse = {value: key for key, value in self._vehicles.items()}
        vehicle_id = reverse.get(native) if native is not None else None
        if vehicle_id is None or self._assigned(order, "driver_assigned") != self._drivers.get(
            vehicle_id
        ):
            return None
        return vehicle_id

    @staticmethod
    def _ranks(order: dict[str, JsonValue]) -> tuple[int | None, int | None]:
        meta = order.get("meta")
        route = meta.get("crucible_route") if isinstance(meta, dict) else None
        if not isinstance(route, dict):
            return None, None
        pickup, dropoff = route.get("pickup"), route.get("dropoff")
        return (
            pickup if isinstance(pickup, int) else None,
            dropoff if isinstance(dropoff, int) else None,
        )

    def _clear(self, order: dict[str, JsonValue]) -> None:
        order["driver_assigned"] = None
        order["vehicle_assigned"] = None
        order["dispatched"] = False
        if order.get("status") == "dispatched":
            order["status"] = "created"
        self._meta(order).pop("crucible_route", None)

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
        if self._vehicle_of(order) == vehicle_id:
            return copy.deepcopy(order)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        top = -1
        for other in self._orders.values():
            if other is not order and self._vehicle_of(other) == vehicle_id:
                top = max([top, *(rank for rank in self._ranks(other) if rank is not None)])
        order["driver_assigned"] = {"id": self._drivers[vehicle_id]}
        order["vehicle_assigned"] = {"id": self._vehicles[vehicle_id]}
        self._meta(order)["crucible_route"] = {"pickup": top + 1, "dropoff": top + 2}
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
        order = self._order(request_id)
        if request_id in self._cancelled:
            return copy.deepcopy(order)
        if request_id in self._picked_up:
            raise FleetbaseOperationError("Order cannot change after pickup")
        self._cancelled.add(request_id)
        order["status"] = "canceled"
        self._meta(order).pop("crucible_route", None)
        return copy.deepcopy(order)

    async def update_order(
        self, request_id: str, fields: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        order = self._open_order(request_id)
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise FleetbaseOperationError("Only note and priority can be updated")
        self._meta(order).update(copy.deepcopy(fields))
        return copy.deepcopy(order)

    async def unassign_order(self, request_id: str) -> dict[str, JsonValue]:
        order = self._before_pickup(request_id)
        self._clear(order)
        return copy.deepcopy(order)

    async def set_route(self, vehicle_id: str, stops: list[tuple[str, str]]) -> None:
        if vehicle_id not in self._vehicles:
            raise FleetbaseOperationError("Unknown vehicle")
        ranks: dict[str, dict[str, JsonValue]] = {}
        for rank, (request_id, action) in enumerate(stops):
            order = self._open_order(request_id)
            if self._vehicle_of(order) != vehicle_id:
                raise FleetbaseOperationError("Order is not assigned to this vehicle")
            if action not in {"pickup", "dropoff"}:
                raise FleetbaseOperationError("Unknown stop action")
            ranks.setdefault(request_id, {})[action] = rank
        for request_id, route in ranks.items():
            meta = self._meta(self._orders[request_id])
            previous = meta.get("crucible_route")
            merged: dict[str, JsonValue] = dict(previous) if isinstance(previous, dict) else {}
            merged.update(route)
            meta["crucible_route"] = merged

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        result: dict[str, dict[str, JsonValue]] = {}
        for request_id, order in self._orders.items():
            pickup, dropoff = self._ranks(order)
            cancelled = request_id in self._cancelled
            result[request_id] = {
                "vehicle_id": None if cancelled else self._vehicle_of(order),
                "dispatched": order.get("dispatched") is True,
                "cancelled": cancelled,
                "pickup_rank": pickup,
                "dropoff_rank": dropoff,
            }
        return result

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        for event in events:
            key = (event.request_id, event.kind)
            if key in self._synced:
                continue
            if event.kind in _PROGRESS:
                order = self._order(event.request_id)
                self._picked_up.add(event.request_id)
                if order.get("status") != "completed":
                    order["started"] = True
                    order["status"] = "enroute" if event.kind == "picked_up" else "completed"
            elif event.kind in {"unassigned", "customer_cancelled", "dropoff_changed"}:
                order = self._orders.get(event.request_id)
                if order is None:
                    continue
                if event.kind == "unassigned":
                    # A later breakdown can unassign the same order again.
                    self._clear(order)
                    continue
                if event.kind == "customer_cancelled":
                    self._cancelled.add(event.request_id)
                    order["status"] = "canceled"
                    self._meta(order)["cancelled_by"] = "customer"
                    self._meta(order).pop("crucible_route", None)
                else:
                    self._meta(order)["dropoff"] = copy.deepcopy(event.data.get("dropoff"))
                    # A changed dropoff can change again; only progress is synced once.
                    continue
            else:
                continue
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

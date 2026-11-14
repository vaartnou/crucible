"""The adapter contract shared by the native Fleetbase and in-memory backends.

The task reads commitments and records only through this protocol, so every
backend must return the same shapes:

- ``committed()`` maps each created request ID to ``{"vehicle_id": str | None,
  "dispatched": bool, "cancelled": bool, "pickup_rank": int | None,
  "dropoff_rank": int | None}``. Ranks order the vehicle's stops; they live in the
  order's ``meta`` (``crucible_route``), so a vehicle's route is the rank-sorted stops
  of its dispatched orders.
- ``snapshot()`` returns ``{"orders": {request_id: record}, "vehicles": {...},
  "drivers": {...}, "duplicate_orders": int}``. Each order record has the keys
  ``id``, ``internal_id``, ``status``, ``dispatched``, ``started``,
  ``driver_assigned``, ``vehicle_assigned``, ``meta`` and ``payload``.

A rejected business operation raises ``FleetbaseOperationError``. Any other
``BackendError`` is an infrastructure failure.
"""

from typing import Protocol

from pydantic import JsonValue

from crucible.backends import BackendError
from crucible.envs.fleetbase_dispatch.sim import PhysicalEvent
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle


class FleetbaseOperationError(BackendError):
    """A rejected native operation; public messages contain no backend payloads."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DispatchAdapter(Protocol):
    """Order operations and private progress synchronization for one episode."""

    async def prepare(self, vehicles: tuple[Vehicle, ...]) -> None:
        """Create the episode's drivers and vehicles."""
        ...

    async def create_order(self, request: Request) -> dict[str, JsonValue]:
        """Create the request's order. A repeated create returns the existing order."""
        ...

    async def assign_order(self, request_id: str, vehicle_id: str) -> dict[str, JsonValue]:
        """Assign the vehicle and its driver to a created order before pickup.

        A new vehicle gets the order's pickup and dropoff appended to the end of its
        route. Assigning the current vehicle again changes nothing.
        """
        ...

    async def dispatch_order(self, request_id: str) -> dict[str, JsonValue]:
        """Dispatch an assigned order. A repeated dispatch returns the order unchanged."""
        ...

    async def cancel_order(self, request_id: str) -> dict[str, JsonValue]:
        """Cancel an order before pickup. A repeated cancel returns the order unchanged."""
        ...

    async def update_order(
        self, request_id: str, fields: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Store the agent-editable fields (``note``, ``priority``) in the order's meta."""
        ...

    async def unassign_order(self, request_id: str) -> dict[str, JsonValue]:
        """Clear the vehicle, driver, dispatch flag and route ranks before pickup."""
        ...

    async def set_route(self, vehicle_id: str, stops: list[tuple[str, str]]) -> None:
        """Write route ranks for (request_id, "pickup" | "dropoff") stops, in order.

        Every listed order must be assigned to the vehicle.
        """
        ...

    async def committed(self) -> dict[str, dict[str, JsonValue]]:
        """The complete committed assignment and dispatch state of all created orders."""
        ...

    async def sync_events(self, events: list[PhysicalEvent]) -> None:
        """Apply verified world changes to the order records.

        Kinds: ``picked_up``, ``delivered``, ``delivered_late`` (progress),
        ``unassigned`` (breakdown), ``customer_cancelled`` and ``dropoff_changed``.
        Other kinds are ignored. Changes to uncreated orders are skipped, except progress.
        """
        ...

    async def snapshot(self) -> dict[str, JsonValue]:
        """Private read-back of all order records, for observation and grading."""
        ...

    async def cleanup(self) -> None:
        """Delete the episode's records. Repeated calls are allowed."""
        ...


__all__ = ["DispatchAdapter", "FleetbaseOperationError"]

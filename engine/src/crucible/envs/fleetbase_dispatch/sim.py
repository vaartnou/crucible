"""Deterministic private physics; native application status is never evidence."""

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.world import Point, Request, Vehicle, World


@dataclass(frozen=True)
class PhysicalEvent:
    kind: str
    time: float
    request_id: str
    vehicle_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return dict(asdict(self))


@dataclass
class _RequestState:
    request: Request
    status: str = "waiting"
    vehicle_id: str | None = None
    pickup_time: float | None = None
    delivery_time: float | None = None


@dataclass
class _VehicleState:
    vehicle: Vehicle
    position: Point
    current_request: str | None = None
    distance: float = 0.0


class WorldSimulator:
    """Single-order physical commitments reconciled from native application records.

    Reconcile accepts the *complete* committed snapshot. A missing or unassigned
    request can stop its approach before pickup. A picked-up request cannot be
    reassigned or removed. App ``status`` and ``completed`` flags are ignored.
    ``advance`` is the only operation that changes time or physical economics.
    """

    def __init__(self, world: World) -> None:
        if world.horizon <= 0 or world.speed <= 0 or world.cost_per_distance < 0:
            raise ValueError("invalid physical world parameters")
        if len({r.id for r in world.requests}) != len(world.requests):
            raise ValueError("duplicate request IDs")
        if len({v.id for v in world.vehicles}) != len(world.vehicles):
            raise ValueError("duplicate vehicle IDs")
        self.world = world
        self.now = 0.0
        self._requests = {r.id: _RequestState(r) for r in world.requests}
        self._vehicles = {v.id: _VehicleState(v, v.position) for v in world.vehicles}
        self.events: list[PhysicalEvent] = [
            PhysicalEvent("released", 0.0, r.id) for r in world.requests if r.release <= 0
        ]
        self._last_rejections: set[tuple[str, str | None, str]] = set()

    def reconcile(self, committed: Mapping[str, Mapping[str, JsonValue]]) -> list[PhysicalEvent]:
        """Observe app commitments without trusting app delivery claims.

        Conflicting dispatches cannot teleport vehicles or load extra cargo. The
        existing approach has priority over an additional conflicting dispatch.
        Repeated rejected snapshots do not multiply diagnostic events.
        """
        if self.now >= self.world.horizon:
            return []
        emitted: list[PhysicalEvent] = []
        rejections: set[tuple[str, str | None, str]] = set()

        def reject(request_id: str, vehicle_id: str | None, detail: str) -> None:
            rejection = (request_id, vehicle_id, detail)
            rejections.add(rejection)
            if rejection not in self._last_rejections:
                emitted.append(PhysicalEvent("rejected", self.now, request_id, vehicle_id, detail))

        previous_active = {
            v.current_request for v in self._vehicles.values() if v.current_request is not None
        }
        # Release mutable approaches before applying a complete new snapshot.
        for vehicle in self._vehicles.values():
            current = self._requests.get(vehicle.current_request or "")
            if current is None or current.status != "picked_up":
                vehicle.current_request = None
        for state in self._requests.values():
            if state.pickup_time is None and state.delivery_time is None:
                state.vehicle_id = None
                state.status = "waiting"

        ordered = sorted(
            committed, key=lambda request_id: (request_id not in previous_active, request_id)
        )
        for request_id in ordered:
            record = committed[request_id]
            state = self._requests.get(request_id)
            raw_vehicle = record.get("vehicle_id")
            vehicle_id = raw_vehicle if isinstance(raw_vehicle, str) else None
            if state is None:
                reject(request_id, vehicle_id, "unknown demand")
                continue
            if state.request.release > self.now:
                reject(request_id, vehicle_id, "demand is not released")
                continue
            if state.delivery_time is not None:
                continue
            if state.pickup_time is not None:
                if vehicle_id != state.vehicle_id or record.get("dispatched") is not True:
                    reject(request_id, vehicle_id, "pickup commitment is irreversible")
                continue
            if vehicle_id is None:
                continue
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                reject(request_id, vehicle_id, "unknown vehicle")
                continue
            if state.request.load > vehicle.vehicle.capacity:
                reject(request_id, vehicle_id, "capacity exceeded")
                continue
            state.vehicle_id = vehicle_id
            state.status = "assigned"
            if record.get("dispatched") is not True:
                continue
            if vehicle.current_request is not None:
                reject(request_id, vehicle_id, "vehicle already carries a commitment")
                continue
            state.status = "dispatched"
            vehicle.current_request = request_id
        # A removed native row cannot erase cargo already physically picked up.
        for state in self._requests.values():
            if state.status == "picked_up" and state.request.id not in committed:
                reject(state.request.id, state.vehicle_id, "pickup commitment is irreversible")
        self._last_rejections = rejections
        self.events.extend(emitted)
        return emitted

    def advance(self, minutes: float) -> list[PhysicalEvent]:
        """Advance continuous motion and demand releases, then return settled events."""
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError("minutes must be finite and nonnegative")
        target = min(self.world.horizon, self.now + minutes)
        emitted: list[PhysicalEvent] = [
            PhysicalEvent("released", r.release, r.id)
            for r in self.world.requests
            if self.now < r.release <= target
        ]
        for vehicle_id, vehicle in sorted(self._vehicles.items()):
            if vehicle.current_request is None:
                continue
            state = self._requests[vehicle.current_request]
            cursor = self.now
            while cursor <= target and vehicle.current_request is not None:
                destination = (
                    state.request.pickup if state.pickup_time is None else state.request.dropoff
                )
                distance = math.dist(vehicle.position, destination)
                travel_time = distance / self.world.speed
                available = target - cursor
                if travel_time > available + 1e-10:
                    traveled = available * self.world.speed
                    fraction = traveled / distance
                    vehicle.position = (
                        vehicle.position[0] + (destination[0] - vehicle.position[0]) * fraction,
                        vehicle.position[1] + (destination[1] - vehicle.position[1]) * fraction,
                    )
                    vehicle.distance += traveled
                    break
                vehicle.position = destination
                vehicle.distance += distance
                cursor = min(target, cursor + travel_time)
                if state.pickup_time is None:
                    state.pickup_time = cursor
                    state.status = "picked_up"
                    emitted.append(PhysicalEvent("picked_up", cursor, state.request.id, vehicle_id))
                else:
                    state.delivery_time = cursor
                    state.status = (
                        "delivered" if cursor <= state.request.deadline + 1e-9 else "late"
                    )
                    emitted.append(
                        PhysicalEvent(state.status, cursor, state.request.id, vehicle_id)
                    )
                    vehicle.current_request = None
        self.now = target
        emitted.sort(key=lambda event: (event.time, _event_priority(event.kind), event.request_id))
        self.events.extend(emitted)
        return emitted

    def finish(self) -> list[PhysicalEvent]:
        """Published continuation: finish committed work until the horizon, no new dispatches."""
        return self.advance(max(0.0, self.world.horizon - self.now))

    def observation(self) -> dict[str, JsonValue]:
        requests: list[JsonValue] = []
        for state in self._requests.values():
            request = state.request
            if request.release > self.now:
                continue
            requests.append(
                {
                    "id": request.id,
                    "pickup": list(request.pickup),
                    "dropoff": list(request.dropoff),
                    "load": request.load,
                    "release": request.release,
                    "deadline": request.deadline,
                    "value": request.value,
                    "status": state.status,
                    "vehicle_id": state.vehicle_id,
                }
            )
        vehicles: list[JsonValue] = [
            {
                "id": vehicle.vehicle.id,
                "driver_id": vehicle.vehicle.driver_id,
                "capacity": vehicle.vehicle.capacity,
                "position": list(vehicle.position),
                "current_request": vehicle.current_request,
            }
            for vehicle in self._vehicles.values()
        ]
        return {
            "now": self.now,
            "horizon": self.world.horizon,
            "speed": self.world.speed,
            "cost_per_distance": self.world.cost_per_distance,
            "next_release": min(
                (r.release for r in self.world.requests if r.release > self.now), default=None
            ),
            "requests": requests,
            "vehicles": vehicles,
        }

    def economics(self) -> dict[str, float]:
        """Economic reward from physical records, including the entire generated demand."""
        realized = sum(s.request.value for s in self._requests.values() if s.status == "delivered")
        total = sum(r.value for r in self.world.requests)
        distance = sum(v.distance for v in self._vehicles.values())
        driving_cost = distance * self.world.cost_per_distance
        utility = realized - driving_cost
        deliveries = sum(s.status == "delivered" for s in self._requests.values())
        late = sum(s.status == "late" for s in self._requests.values())
        return {
            "realized_value": realized,
            "driving_cost": driving_cost,
            "utility": utility,
            "total_generated_value": total,
            "reward": min(1.0, max(0.0, utility / total)) if total else 0.0,
            "deliveries": float(deliveries),
            "late": float(late),
            "unserved": float(len(self._requests) - deliveries - late),
            "distance": distance,
        }


def _event_priority(kind: str) -> int:
    return {"released": 0, "picked_up": 1, "delivered": 2, "late": 2}.get(kind, 3)

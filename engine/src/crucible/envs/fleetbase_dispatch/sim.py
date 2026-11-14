"""Deterministic private physics; native application status is never evidence.

Commitments
    `reconcile(committed)` reads the *complete* committed order snapshot from the
    adapter. Each record is ``{"vehicle_id", "dispatched", "cancelled", "pickup_rank",
    "dropoff_rank"}``. A vehicle executes the stops of its dispatched orders, sorted by
    rank. Stops without a rank go last, grouped per order (pickup, then dropoff).
    Assigned but undispatched orders are not executed. Capacity must hold at every
    stop of the executed route, starting from the cargo on board; an order whose pickup
    would exceed it is rejected and not executed. App ``status`` is ignored.

Movement
    Straight lines at `world.speed`, multiplied by the factor of every active traffic
    zone that contains the vehicle. Movement through zones is integrated exactly per
    segment (zone borders and zone start/end times are breakpoints). A vehicle waits at
    a pickup until the request's ready time. Stops take no time. A broken-down vehicle
    does not move until it is repaired, then resumes its route. A vehicle stops for good
    at its shift end; cargo still on board is never delivered.

Disruptions (pre-generated in the world, applied when their time is reached)
    - breakdown: orders assigned to the vehicle and not yet picked up become unassigned.
      Cargo on board stays on board and waits.
    - customer cancellation: an order not yet picked up leaves every route. Cargo
      already on board is unloaded on the spot: it frees capacity, its dropoff stop
      is removed and it earns nothing. A customer cancellation costs nothing.
    - dropoff change: every route uses the new dropoff from that time on.
    - traffic zone: announced in the event feed when it starts.
    Silent disruptions produce events with ``public=False``: they reach the adapter
    (the order record changes) but not the public event feed.

Economics
    U = on-time value + late value - sum of per-vehicle driving cost - cancellation fees.
    Late value decays linearly from the full value at the deadline to 0 at
    ``deadline + late_window``. A fee is charged once when the *agent* cancels an order
    that was ever dispatched. ``reward = clip(U / u_bound, 0, 1)``.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.world import Disruption, Point, Request, Vehicle, World

EPS = 1e-9
_UNRANKED = 1 << 30
Stop = tuple[str, str]  # (request_id, "pickup" | "dropoff")


@dataclass(frozen=True)
class PhysicalEvent:
    kind: str
    time: float
    request_id: str = ""
    vehicle_id: str | None = None
    detail: str = ""
    public: bool = True
    data: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    def to_dict(self) -> dict[str, JsonValue]:
        return dict(asdict(self))


@dataclass
class _RequestState:
    request: Request
    dropoff: Point
    released: bool = False
    vehicle_id: str | None = None
    dispatched: bool = False
    ever_dispatched: bool = False
    pickup_time: float | None = None
    delivery_time: float | None = None
    carrier: str | None = None
    cancelled_by: str | None = None
    fee_charged: bool = False

    @property
    def on_board(self) -> bool:
        return (
            self.pickup_time is not None
            and self.delivery_time is None
            and self.cancelled_by is None
        )

    @property
    def closed(self) -> bool:
        return self.delivery_time is not None or self.cancelled_by is not None


@dataclass
class _VehicleState:
    vehicle: Vehicle
    position: Point
    route: list[Stop] = field(default_factory=list[Stop])
    onboard: list[str] = field(default_factory=list[str])
    distance: float = 0.0
    broken_until: float | None = None
    off_shift: bool = False
    reposition: Point | None = None


@dataclass(frozen=True)
class _Zone:
    bounds: tuple[float, float, float, float]
    start: float
    end: float
    factor: float


@dataclass(frozen=True)
class _Plan:
    assignment: dict[str, tuple[str, bool]]
    routes: dict[str, list[Stop]]
    cancels: dict[str, bool]
    rejections: list[tuple[str, str | None, str]]


def zones_of(disruptions: Sequence[Disruption]) -> list[_Zone]:
    return [
        _Zone(d.bounds, d.time, d.time + d.duration, d.factor)
        for d in disruptions
        if d.kind == "traffic_zone" and d.bounds is not None
    ]


def _inside(point: Point, bounds: tuple[float, float, float, float]) -> bool:
    return bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]


def _crossings(start: Point, end: Point, bounds: tuple[float, float, float, float]) -> list[float]:
    """Fractions along start→end where the segment crosses the rectangle's border."""
    t0, t1 = 0.0, 1.0
    dx, dy = end[0] - start[0], end[1] - start[1]
    for p, q in (
        (-dx, start[0] - bounds[0]),
        (dx, bounds[2] - start[0]),
        (-dy, start[1] - bounds[1]),
        (dy, bounds[3] - start[1]),
    ):
        if abs(p) < EPS:
            if q < 0:
                return []
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
    if t0 > t1:
        return []
    return [t0, t1]


def travel(
    position: Point,
    destination: Point,
    now: float,
    until: float,
    speed: float,
    zones: Sequence[_Zone],
) -> tuple[Point, float, float]:
    """Move toward `destination` from `now` until arrival or `until`.

    Returns (position, time, distance travelled). Speed is piecewise constant: zone
    borders along the segment and zone start/end times are breakpoints.
    """
    moved = 0.0
    while True:
        remaining = math.dist(position, destination)
        if remaining <= EPS:
            return destination, now, moved
        if now >= until - EPS:
            return position, now, moved
        fractions = [f for zone in zones for f in _crossings(position, destination, zone.bounds)]
        spatial = min((f * remaining for f in fractions if f * remaining > EPS), default=remaining)
        spatial = min(spatial, remaining)
        mid_fraction = spatial / remaining / 2
        mid = (
            position[0] + (destination[0] - position[0]) * mid_fraction,
            position[1] + (destination[1] - position[1]) * mid_fraction,
        )
        factor = 1.0
        for zone in zones:
            if zone.start <= now + EPS and now < zone.end - EPS and _inside(mid, zone.bounds):
                factor *= zone.factor
        temporal = min(
            (t for zone in zones for t in (zone.start, zone.end) if t > now + EPS),
            default=math.inf,
        )
        rate = speed * factor
        available = min(until, temporal) - now
        if spatial <= rate * available + EPS:
            step = spatial
            now += spatial / rate
        else:
            step = rate * available
            now += available
        fraction = step / remaining
        position = (
            destination[0]
            if fraction >= 1
            else position[0] + (destination[0] - position[0]) * fraction,
            destination[1]
            if fraction >= 1
            else position[1] + (destination[1] - position[1]) * fraction,
        )
        moved += step


def late_factor(delivery: float, deadline: float, late_window: float) -> float:
    if delivery <= deadline + EPS:
        return 1.0
    return max(0.0, 1.0 - (delivery - deadline) / late_window)


def _rank(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class WorldSimulator:
    """Physics and economics of one world. ``advance`` is the only way time passes."""

    def __init__(self, world: World) -> None:
        if world.horizon <= 0 or world.speed <= 0 or world.late_window <= 0:
            raise ValueError("invalid physical world parameters")
        if any(v.cost_per_distance < 0 for v in world.vehicles):
            raise ValueError("invalid physical world parameters")
        if len({r.id for r in world.requests}) != len(world.requests):
            raise ValueError("duplicate request IDs")
        if len({v.id for v in world.vehicles}) != len(world.vehicles):
            raise ValueError("duplicate vehicle IDs")
        self.world = world
        self.now = 0.0
        self._requests = {r.id: _RequestState(r, r.dropoff) for r in world.requests}
        self._vehicles = {v.id: _VehicleState(v, v.position) for v in world.vehicles}
        self._zones = zones_of(world.disruptions)
        self._schedule = self._build_schedule()
        self._cursor = 0
        self._fees = 0
        self.events: list[PhysicalEvent] = []
        self._last_rejections: set[tuple[str, str | None, str]] = set()
        initial: list[PhysicalEvent] = []
        self._apply_world_events(initial)
        self.events.extend(initial)

    # Schedule ------------------------------------------------------------------------

    def _build_schedule(self) -> list[tuple[float, int, str, str, Disruption | None]]:
        """(time, priority, kind, subject id, disruption) sorted by time."""
        items: list[tuple[float, int, str, str, Disruption | None]] = []
        for r in self.world.requests:
            items.append((r.release, 0, "release", r.id, None))
        for v in self.world.vehicles:
            if v.shift_end <= self.world.horizon:
                items.append((v.shift_end, 3, "shift_end", v.id, None))
        for d in self.world.disruptions:
            subject = d.vehicle_id or d.request_id or d.id
            items.append((d.time, 2, d.kind, subject, d))
            if d.kind == "breakdown":
                items.append((d.time + d.duration, 1, "repair", subject, d))
        items.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        return items

    def _next_world_time(self) -> float:
        if self._cursor < len(self._schedule):
            return self._schedule[self._cursor][0]
        return math.inf

    def _apply_world_events(self, emitted: list[PhysicalEvent]) -> None:
        while self._cursor < len(self._schedule) and self._schedule[self._cursor][0] <= self.now:
            time, _, kind, subject, disruption = self._schedule[self._cursor]
            self._cursor += 1
            self._apply(time, kind, subject, disruption, emitted)

    def _drop_stops(self, request_id: str) -> None:
        for vehicle in self._vehicles.values():
            vehicle.route = [stop for stop in vehicle.route if stop[0] != request_id]

    def _apply(
        self,
        time: float,
        kind: str,
        subject: str,
        disruption: Disruption | None,
        emitted: list[PhysicalEvent],
    ) -> None:
        if kind == "release":
            state = self._requests[subject]
            state.released = True
            emitted.append(
                PhysicalEvent("released", time, subject, data={"urgent": state.request.urgent})
            )
        elif kind == "shift_end":
            self._vehicles[subject].off_shift = True
            emitted.append(PhysicalEvent("shift_ended", time, vehicle_id=subject))
        elif kind == "repair":
            vehicle = self._vehicles[subject]
            vehicle.broken_until = None
            emitted.append(PhysicalEvent("repaired", time, vehicle_id=subject))
        elif disruption is None:
            raise AssertionError(kind)
        elif kind == "breakdown":
            vehicle = self._vehicles[subject]
            until = round(time + disruption.duration, 6)
            vehicle.broken_until = until
            emitted.append(
                PhysicalEvent(
                    "breakdown",
                    time,
                    vehicle_id=subject,
                    data={"until": until, "disruption_id": disruption.id},
                )
            )
            for request_id, state in sorted(self._requests.items()):
                if state.vehicle_id != subject or state.pickup_time is not None or state.closed:
                    continue
                state.vehicle_id = None
                state.dispatched = False
                self._drop_stops(request_id)
                emitted.append(
                    PhysicalEvent("unassigned", time, request_id, subject, "vehicle breakdown")
                )
        elif kind == "cancellation":
            state = self._requests[subject]
            if state.closed:
                return
            if state.on_board and state.carrier is not None:
                carrier = self._vehicles[state.carrier]
                carrier.onboard.remove(subject)
            state.vehicle_id = None
            state.dispatched = False
            state.cancelled_by = "customer"
            self._drop_stops(subject)
            emitted.append(
                PhysicalEvent(
                    "customer_cancelled",
                    time,
                    subject,
                    state.carrier,
                    public=not disruption.silent,
                    data={"disruption_id": disruption.id},
                )
            )
        elif kind == "dropoff_change":
            state = self._requests[subject]
            if state.closed or disruption.point is None:
                return
            state.dropoff = disruption.point
            emitted.append(
                PhysicalEvent(
                    "dropoff_changed",
                    time,
                    subject,
                    public=not disruption.silent,
                    data={"dropoff": list(disruption.point), "disruption_id": disruption.id},
                )
            )
        elif kind == "traffic_zone":
            bounds = disruption.bounds or (0.0, 0.0, 0.0, 0.0)
            emitted.append(
                PhysicalEvent(
                    "traffic_zone",
                    time,
                    data={
                        "disruption_id": disruption.id,
                        "bounds": list(bounds),
                        "factor": disruption.factor,
                        "start": time,
                        "end": round(time + disruption.duration, 6),
                    },
                )
            )

    # Commitments ---------------------------------------------------------------------

    def _plan(self, committed: Mapping[str, Mapping[str, JsonValue]]) -> _Plan:
        """The routes a complete committed snapshot implies. Pure: changes nothing."""
        rejections: list[tuple[str, str | None, str]] = []
        assignment: dict[str, tuple[str, bool]] = {}
        cancels: dict[str, bool] = {}
        keyed: dict[str, list[tuple[tuple[int, str, int], Stop]]] = {v: [] for v in self._vehicles}

        def keys(request_id: str, record: Mapping[str, JsonValue]) -> tuple[int, int]:
            pickup, dropoff = _rank(record.get("pickup_rank")), _rank(record.get("dropoff_rank"))
            return (
                _UNRANKED if pickup is None else pickup,
                _UNRANKED if dropoff is None else dropoff,
            )

        for request_id in sorted(committed):
            record = committed[request_id]
            state = self._requests.get(request_id)
            raw_vehicle = record.get("vehicle_id")
            vehicle_id = raw_vehicle if isinstance(raw_vehicle, str) else None
            dispatched = record.get("dispatched") is True
            cancelled = record.get("cancelled") is True
            if state is None:
                rejections.append((request_id, vehicle_id, "unknown demand"))
                continue
            if not state.released:
                rejections.append((request_id, vehicle_id, "demand is not released"))
                continue
            if state.closed:
                continue
            if state.pickup_time is not None:
                if cancelled or vehicle_id != state.carrier or not dispatched:
                    rejections.append((request_id, vehicle_id, "pickup commitment is irreversible"))
                continue
            if cancelled:
                cancels[request_id] = dispatched
                continue
            if vehicle_id is None:
                continue
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                rejections.append((request_id, vehicle_id, "unknown vehicle"))
                continue
            if state.request.load > vehicle.vehicle.capacity:
                rejections.append((request_id, vehicle_id, "capacity exceeded"))
                continue
            assignment[request_id] = (vehicle_id, dispatched)
            if not dispatched:
                continue
            pickup, dropoff = keys(request_id, record)
            if pickup > dropoff or (pickup == dropoff and pickup != _UNRANKED):
                rejections.append((request_id, vehicle_id, "dropoff before pickup"))
                assignment[request_id] = (vehicle_id, False)
                continue
            keyed[vehicle_id].append(((pickup, request_id, 0), (request_id, "pickup")))
            keyed[vehicle_id].append(((dropoff, request_id, 1), (request_id, "dropoff")))
        for request_id, state in sorted(self._requests.items()):
            if not state.on_board or state.carrier is None:
                continue
            record = committed.get(request_id)
            if record is None:
                rejections.append((request_id, None, "pickup commitment is irreversible"))
            _, dropoff = keys(request_id, record or {})
            keyed[state.carrier].append(((dropoff, request_id, 1), (request_id, "dropoff")))
        routes: dict[str, list[Stop]] = {}
        for vehicle_id, entries in keyed.items():
            vehicle = self._vehicles[vehicle_id]
            load = sum(self._requests[r].request.load for r in vehicle.onboard)
            excluded: set[str] = set()
            route: list[Stop] = []
            for _, stop in sorted(entries):
                request_id, action = stop
                request_load = self._requests[request_id].request.load
                if request_id in excluded:
                    continue
                if action == "pickup":
                    if load + request_load > vehicle.vehicle.capacity:
                        excluded.add(request_id)
                        rejections.append((request_id, vehicle_id, "capacity exceeded at a stop"))
                        assignment[request_id] = (vehicle_id, False)
                        continue
                    load += request_load
                else:
                    load -= request_load
                route.append(stop)
            routes[vehicle_id] = route
        return _Plan(assignment, routes, cancels, rejections)

    def preview(
        self, committed: Mapping[str, Mapping[str, JsonValue]]
    ) -> list[tuple[str, str | None, str]]:
        """Rejections a hypothetical committed snapshot would cause, without applying it."""
        return self._plan(committed).rejections

    def reconcile(self, committed: Mapping[str, Mapping[str, JsonValue]]) -> list[PhysicalEvent]:
        """Apply the complete committed snapshot without trusting app delivery claims.

        Repeated identical rejections do not multiply diagnostic events.
        """
        if self.now >= self.world.horizon:
            return []
        plan = self._plan(committed)
        emitted: list[PhysicalEvent] = []
        for request_id, state in self._requests.items():
            if state.pickup_time is not None or state.closed:
                continue
            vehicle_id, dispatched = plan.assignment.get(request_id, (None, False))
            state.vehicle_id = vehicle_id
            state.dispatched = dispatched
            state.ever_dispatched = state.ever_dispatched or dispatched
        for request_id, dispatched in plan.cancels.items():
            state = self._requests[request_id]
            state.ever_dispatched = state.ever_dispatched or dispatched
            state.cancelled_by = "agent"
            state.vehicle_id = None
            state.dispatched = False
            if state.ever_dispatched and not state.fee_charged:
                state.fee_charged = True
                self._fees += 1
            emitted.append(PhysicalEvent("agent_cancelled", self.now, request_id))
        for vehicle_id, route in plan.routes.items():
            vehicle = self._vehicles[vehicle_id]
            vehicle.route = route
            if route:
                vehicle.reposition = None
        rejections = set(plan.rejections)
        for rejection in plan.rejections:
            if rejection not in self._last_rejections:
                request_id, vehicle_id, detail = rejection
                emitted.append(PhysicalEvent("rejected", self.now, request_id, vehicle_id, detail))
        self._last_rejections = rejections
        self.events.extend(emitted)
        return emitted

    def reposition(self, vehicle_id: str, point: Point) -> str | None:
        """Send an idle vehicle toward `point`. Returns an error, or None on success."""
        vehicle = self._vehicles.get(vehicle_id)
        if vehicle is None:
            return "Unknown vehicle"
        if vehicle.off_shift:
            return "Vehicle shift has ended"
        if vehicle.route:
            return "Vehicle has dispatched stops; reposition applies only to an idle vehicle"
        area = self.world.area
        if not (0 <= point[0] <= area and 0 <= point[1] <= area):
            return f"Point is outside the service area [0, {area}]"
        vehicle.reposition = point
        return None

    # Time ----------------------------------------------------------------------------

    def advance(self, minutes: float) -> list[PhysicalEvent]:
        """Advance motion and the disruption schedule, then return the settled events."""
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError("minutes must be finite and nonnegative")
        target = min(self.world.horizon, self.now + minutes)
        emitted: list[PhysicalEvent] = []
        while True:
            self._apply_world_events(emitted)
            if self.now >= target:
                break
            until = min(self._next_world_time(), target)
            for vehicle_id in sorted(self._vehicles):
                self._move(vehicle_id, until, emitted)
            self.now = until
        emitted.sort(key=lambda event: (event.time, _event_priority(event.kind)))
        self.events.extend(emitted)
        return emitted

    def finish(self) -> list[PhysicalEvent]:
        """Continue committed work until the horizon, with no new dispatches."""
        return self.advance(max(0.0, self.world.horizon - self.now))

    def _move(self, vehicle_id: str, until: float, emitted: list[PhysicalEvent]) -> None:
        vehicle = self._vehicles[vehicle_id]
        if vehicle.off_shift or vehicle.broken_until is not None:
            return
        now = self.now
        while True:
            if not vehicle.route:
                if vehicle.reposition is None or now >= until - EPS:
                    return
                position, now, moved = travel(
                    vehicle.position, vehicle.reposition, now, until, self.world.speed, self._zones
                )
                vehicle.position = position
                vehicle.distance += moved
                if position == vehicle.reposition:
                    vehicle.reposition = None
                continue
            request_id, action = vehicle.route[0]
            state = self._requests[request_id]
            destination = state.request.pickup if action == "pickup" else state.dropoff
            if math.dist(vehicle.position, destination) > EPS:
                if now >= until - EPS:
                    return
                position, now, moved = travel(
                    vehicle.position, destination, now, until, self.world.speed, self._zones
                )
                vehicle.position = position
                vehicle.distance += moved
                continue
            vehicle.position = destination
            if action == "pickup":
                earliest = max(state.request.release, state.request.ready_time)
                if now < earliest - EPS:
                    now = min(until, earliest)
                    if now < earliest - EPS:
                        return
                load = sum(self._requests[r].request.load for r in vehicle.onboard)
                if load + state.request.load > vehicle.vehicle.capacity:
                    vehicle.route = [s for s in vehicle.route if s[0] != request_id]
                    state.dispatched = False
                    emitted.append(
                        PhysicalEvent(
                            "rejected", now, request_id, vehicle_id, "capacity exceeded at a stop"
                        )
                    )
                    continue
                state.pickup_time = now
                state.carrier = vehicle_id
                vehicle.onboard.append(request_id)
                emitted.append(PhysicalEvent("picked_up", now, request_id, vehicle_id))
            else:
                state.delivery_time = now
                vehicle.onboard.remove(request_id)
                kind = "delivered" if now <= state.request.deadline + EPS else "delivered_late"
                emitted.append(PhysicalEvent(kind, now, request_id, vehicle_id))
            vehicle.route.pop(0)

    # Public views --------------------------------------------------------------------

    def announced_zones(self) -> list[_Zone]:
        """Traffic zones already announced in the event feed (start time reached)."""
        return [zone for zone in self._zones if zone.start <= self.now]

    def request_view(self, request_id: str) -> dict[str, JsonValue] | None:
        """Public physical facts of a released request, or None."""
        state = self._requests.get(request_id)
        if state is None or not state.released:
            return None
        request = state.request
        return {
            "id": request.id,
            "pickup": list(request.pickup),
            "dropoff": list(state.dropoff),
            "load": request.load,
            "released_at": request.release,
            "ready_time": max(request.release, request.ready_time),
            "deadline": request.deadline,
            "value": request.value,
            "urgent": request.urgent,
            "picked_up_at": state.pickup_time,
            "delivered_at": state.delivery_time,
            "cancelled_by": state.cancelled_by,
        }

    def physical_status(self, request_id: str) -> str | None:
        """cancelled, delivered, delivered_late or picked_up; None before pickup."""
        state = self._requests[request_id]
        if state.cancelled_by is not None:
            return "cancelled"
        if state.delivery_time is not None:
            late = state.delivery_time > state.request.deadline + EPS
            return "delivered_late" if late else "delivered"
        if state.pickup_time is not None:
            return "picked_up"
        return None

    def released_ids(self) -> list[str]:
        return [rid for rid, state in self._requests.items() if state.released]

    def vehicle_ids(self) -> list[str]:
        return list(self._vehicles)

    def vehicle_view(self, vehicle_id: str) -> dict[str, JsonValue] | None:
        vehicle = self._vehicles.get(vehicle_id)
        if vehicle is None:
            return None
        if vehicle.off_shift:
            status = "off_shift"
        elif vehicle.broken_until is not None:
            status = "broken_down"
        elif vehicle.route or vehicle.reposition is not None:
            status = "moving"
        else:
            status = "idle"
        load = sum(self._requests[r].request.load for r in vehicle.onboard)
        return {
            "id": vehicle.vehicle.id,
            "driver_id": vehicle.vehicle.driver_id,
            "capacity": vehicle.vehicle.capacity,
            "cost_per_distance": vehicle.vehicle.cost_per_distance,
            "shift_end": vehicle.vehicle.shift_end,
            "position": [round(vehicle.position[0], 2), round(vehicle.position[1], 2)],
            "status": status,
            "repaired_at": vehicle.broken_until,
            "load": load,
            "onboard": list(vehicle.onboard),
            "route": [{"request_id": r, "action": a} for r, a in vehicle.route],
            "reposition_target": list(vehicle.reposition) if vehicle.reposition else None,
            "distance_travelled": round(vehicle.distance, 4),
        }

    def check_route(self, vehicle_id: str, stops: Sequence[Stop], assigned: set[str]) -> str | None:
        """Why `stops` is not a valid complete route for `assigned` orders, or None.

        The route must list each assigned open order's remaining stops exactly once,
        pickup before dropoff, and capacity must hold at every stop.
        """
        vehicle = self._vehicles.get(vehicle_id)
        if vehicle is None:
            return "Unknown vehicle"
        expected: set[Stop] = set()
        for request_id in assigned:
            state = self._requests.get(request_id)
            if state is None or state.closed:
                continue
            if state.pickup_time is None:
                expected.add((request_id, "pickup"))
            expected.add((request_id, "dropoff"))
        for request_id in vehicle.onboard:
            expected.add((request_id, "dropoff"))
        if len(set(stops)) != len(stops):
            return "Each stop may appear only once"
        if set(stops) != expected:
            return (
                "Route must list exactly the remaining stops of the orders assigned to this "
                "vehicle (pickup if not yet picked up, and dropoff)"
            )
        seen: set[str] = set()
        load = sum(self._requests[r].request.load for r in vehicle.onboard)
        for request_id, action in stops:
            state = self._requests[request_id]
            if action == "pickup":
                seen.add(request_id)
                load += state.request.load
                if load > vehicle.vehicle.capacity:
                    return f"Capacity exceeded at the pickup of {request_id}"
            else:
                if state.pickup_time is None and request_id not in seen:
                    return f"Dropoff of {request_id} comes before its pickup"
                load -= state.request.load
        return None

    def estimate(self, vehicle_id: str, stops: Sequence[Stop]) -> dict[str, JsonValue] | None:
        """Plan `stops` from the vehicle's current state with public knowledge only.

        Uses current dropoffs and announced traffic zones. Unannounced disruptions are
        not known, so the estimate can be wrong.
        """
        vehicle = self._vehicles.get(vehicle_id)
        if vehicle is None:
            return None
        zones = self.announced_zones()
        now = self.now if vehicle.broken_until is None else max(self.now, vehicle.broken_until)
        position = vehicle.position
        load = sum(self._requests[r].request.load for r in vehicle.onboard)
        distance = 0.0
        rows: list[JsonValue] = []
        problems: list[str] = []
        for request_id, action in stops:
            state = self._requests[request_id]
            destination = state.request.pickup if action == "pickup" else state.dropoff
            position, now, moved = travel(
                position, destination, now, math.inf, self.world.speed, zones
            )
            distance += moved
            if action == "pickup":
                now = max(now, state.request.release, state.request.ready_time)
                load += state.request.load
                if load > vehicle.vehicle.capacity:
                    problems.append(f"capacity exceeded at the pickup of {request_id}")
            else:
                load -= state.request.load
            row: dict[str, JsonValue] = {
                "request_id": request_id,
                "action": action,
                "eta": round(now, 2),
                "load_after": load,
            }
            if action == "dropoff":
                row["late_minutes"] = round(max(0.0, now - state.request.deadline), 2)
            rows.append(row)
        if now > vehicle.vehicle.shift_end + EPS:
            problems.append("route ends after the shift end")
        return {
            "vehicle_id": vehicle_id,
            "feasible": not problems,
            "problems": list[JsonValue](problems),
            "stops": rows,
            "distance": round(distance, 2),
            "driving_cost": round(distance * vehicle.vehicle.cost_per_distance, 2),
            "end_time": round(now, 2),
        }

    # Economics -----------------------------------------------------------------------

    def upper_bound(self) -> float:
        """Optimistic utility bound: each request served directly with no driving cost.

        A request counts with its value at the earliest possible delivery (decayed if
        that is late) unless the customer cancels it before that delivery.
        """
        # TODO(step4): replace with hindsight solver
        cancel_time = {
            d.request_id: d.time
            for d in self.world.disruptions
            if d.kind == "cancellation" and d.request_id is not None
        }
        changed = {
            d.request_id: d.point
            for d in self.world.disruptions
            if d.kind == "dropoff_change" and d.request_id is not None and d.point is not None
        }
        total = 0.0
        for request in self.world.requests:
            start = max(request.release, request.ready_time)
            trip = math.dist(request.pickup, request.dropoff)
            new = changed.get(request.id)
            if new is not None:
                trip = min(trip, math.dist(request.pickup, new))
            earliest = start + trip / self.world.speed
            if earliest > self.world.horizon + EPS:
                continue
            if cancel_time.get(request.id, math.inf) < earliest - EPS:
                continue
            total += request.value * late_factor(earliest, request.deadline, self.world.late_window)
        return total

    def economics(self) -> dict[str, float]:
        """Economic reward from physical records only."""
        on_time = 0.0
        late_value = 0.0
        on_time_count = late_count = 0
        for state in self._requests.values():
            if state.delivery_time is None:
                continue
            factor = late_factor(
                state.delivery_time, state.request.deadline, self.world.late_window
            )
            if factor == 1.0 and state.delivery_time <= state.request.deadline + EPS:
                on_time += state.request.value
                on_time_count += 1
            else:
                late_value += state.request.value * factor
                late_count += 1
        driving_cost = sum(
            v.distance * v.vehicle.cost_per_distance for v in self._vehicles.values()
        )
        distance = sum(v.distance for v in self._vehicles.values())
        fees = self._fees * self.world.cancellation_fee
        utility = on_time + late_value - driving_cost - fees
        bound = self.upper_bound()
        reward = min(1.0, max(0.0, utility / bound)) if bound > 0 else 0.0
        states = self._requests.values()
        return {
            "on_time_value": on_time,
            "late_value": late_value,
            "driving_cost": driving_cost,
            "cancellation_fees": fees,
            "utility": utility,
            "u_bound": bound,
            "reward": reward,
            "deliveries": float(on_time_count),
            "late": float(late_count),
            "customer_cancelled": float(sum(s.cancelled_by == "customer" for s in states)),
            "agent_cancelled": float(sum(s.cancelled_by == "agent" for s in states)),
            "fees_charged": float(self._fees),
            "unserved": float(
                sum(s.delivery_time is None and s.cancelled_by is None for s in states)
            ),
            "released": float(sum(s.released for s in states)),
            "requests": float(len(self._requests)),
            "distance": distance,
        }


def _event_priority(kind: str) -> int:
    return {
        "released": 0,
        "picked_up": 1,
        "delivered": 2,
        "delivered_late": 2,
    }.get(kind, 3)


__all__ = ["EPS", "PhysicalEvent", "Stop", "WorldSimulator", "late_factor", "travel", "zones_of"]

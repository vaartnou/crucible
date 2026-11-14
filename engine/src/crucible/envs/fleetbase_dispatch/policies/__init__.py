"""Operators that use only the public tools and their results.

Each operator is registered for `fleetbase_dispatch` and for `fleetbase_sim`, which share
tools and results. They work in epochs: read the vehicles and the open orders, plan,
run the create/assign/dispatch workflow for each planned order, then advance the clock.

- `reference`: appends each open order to the vehicle where it adds the most profit per
  minute (value at the estimated delivery time minus driving cost), for vehicles that
  are free within a short look-ahead. Simple on purpose; plan step 4 adds the strong
  reference.
- `greedy`: gives each idle vehicle the open order with the nearest pickup, ignoring
  value, deadlines and cost.
- `stop_early`: runs the reference until the first successful dispatch, then finishes.
"""

import math
from dataclasses import dataclass

from pydantic import JsonValue

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec

EPOCH_MINUTES = 10.0
LOOKAHEAD_MINUTES = 10.0
LATE_WINDOW = 30.0
_OPEN = ("requested", "created")
_WORKFLOW = frozenset({"create_order", "assign_order", "dispatch_order"})

Point = tuple[float, float]


def _number(value: JsonValue, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _point(value: JsonValue) -> Point | None:
    if isinstance(value, list) and len(value) == 2:
        return _number(value[0]), _number(value[1])
    return None


def _string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class _Order:
    id: str
    status: str
    pickup: Point
    dropoff: Point
    load: float
    ready: float
    deadline: float
    value: float

    @staticmethod
    def parse(data: JsonValue) -> _Order | None:
        if not isinstance(data, dict):
            return None
        order_id, pickup, dropoff = (
            _string(data.get("id")),
            _point(data.get("pickup")),
            _point(data.get("dropoff")),
        )
        if order_id is None or pickup is None or dropoff is None:
            return None
        return _Order(
            order_id,
            _string(data.get("status")) or "requested",
            pickup,
            dropoff,
            _number(data.get("load"), 1),
            _number(data.get("ready_time")),
            _number(data.get("deadline")),
            _number(data.get("value")),
        )


@dataclass(frozen=True)
class _Vehicle:
    id: str
    status: str
    position: Point
    capacity: float
    cost: float
    shift_end: float
    repaired_at: float | None
    route: tuple[tuple[str, str], ...]

    @staticmethod
    def parse(data: JsonValue) -> _Vehicle | None:
        if not isinstance(data, dict):
            return None
        vehicle_id, position = _string(data.get("id")), _point(data.get("position"))
        if vehicle_id is None or position is None:
            return None
        route: list[tuple[str, str]] = []
        raw_route = data.get("route")
        for stop in raw_route if isinstance(raw_route, list) else []:
            if isinstance(stop, dict):
                request_id, action = _string(stop.get("request_id")), _string(stop.get("action"))
                if request_id is not None and action is not None:
                    route.append((request_id, action))
        repaired = data.get("repaired_at")
        return _Vehicle(
            vehicle_id,
            _string(data.get("status")) or "idle",
            position,
            _number(data.get("capacity")),
            _number(data.get("cost_per_distance")),
            _number(data.get("shift_end"), math.inf),
            _number(repaired) if repaired is not None else None,
            tuple(route),
        )


class Operator:
    """Epoch loop shared by the operators. Subclasses override `_plan`."""

    name = "reference"
    stop_after_dispatch = False

    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self._queue: list[ToolCall] = []
        self._last: ToolCall | None = None
        self._phase = "read"
        self._now = 0.0
        self._horizon = math.inf
        self._orders: dict[str, _Order] = {}
        self._open: dict[str, _Order] = {}
        self._vehicles: dict[str, _Vehicle] = {}
        self._failed: set[str] = set()
        self._broken_seen = False
        self._dispatched = False

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._reset_state()

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is not None and self._last is not None:
            self._absorb(self._last, obs.last_result)
        elif "now" in obs.data:
            self._now = _number(obs.data.get("now"))
            self._horizon = _number(obs.data.get("horizon"), math.inf)
        call = self._next()
        self._last = call
        return call

    # Results -------------------------------------------------------------------------

    def _absorb(self, call: ToolCall, result: ToolResult) -> None:
        request_id = _string(call.args.get("request_id"))
        if not result.ok:
            if call.name in _WORKFLOW and request_id is not None:
                self._failed.add(request_id)
                self._queue = [c for c in self._queue if c.args.get("request_id") != request_id]
            return
        content = result.content
        if not isinstance(content, dict):
            return
        if call.name == "list_vehicles":
            items = content.get("vehicles")
            for item in items if isinstance(items, list) else []:
                vehicle = _Vehicle.parse(item)
                if vehicle is not None:
                    self._vehicles[vehicle.id] = vehicle
                    self._broken_seen = self._broken_seen or vehicle.status == "broken_down"
        elif call.name == "list_orders":
            items = content.get("orders")
            for item in items if isinstance(items, list) else []:
                order = _Order.parse(item)
                if order is None:
                    continue
                self._orders[order.id] = order
                if order.status in _OPEN and order.id not in self._failed:
                    self._open[order.id] = order
            page, pages = _number(content.get("page"), 1), _number(content.get("pages"), 1)
            if page < pages:
                self._queue.insert(
                    0,
                    ToolCall(
                        name="list_orders",
                        args={"status": call.args.get("status"), "page": int(page) + 1},
                    ),
                )
        elif call.name in _WORKFLOW:
            order = _Order.parse(content)
            if order is not None:
                self._orders[order.id] = order
            if call.name == "dispatch_order":
                self._dispatched = True
        elif call.name == "advance":
            self._now = _number(content.get("now"), self._now)

    # Decisions -----------------------------------------------------------------------

    def _next(self) -> ToolCall:
        if self.stop_after_dispatch and self._dispatched:
            return ToolCall(name="finish")
        if self._queue:
            return self._queue.pop(0)
        if self._now >= self._horizon:
            return ToolCall(name="finish")
        if self._phase == "read":
            self._phase = "plan"
            self._open = {}
            reads = [
                ToolCall(name="list_vehicles", args={"page": 1}),
                ToolCall(name="list_orders", args={"status": "requested", "page": 1}),
            ]
            if self._broken_seen:
                reads.append(ToolCall(name="list_orders", args={"status": "created", "page": 1}))
            self._broken_seen = False
            self._queue = reads[1:]
            return reads[0]
        if self._phase == "plan":
            self._phase = "advance"
            for order, vehicle_id in self._plan():
                if order.status == "requested":
                    self._queue.append(ToolCall(name="create_order", args={"request_id": order.id}))
                self._queue.append(
                    ToolCall(
                        name="assign_order",
                        args={"request_id": order.id, "vehicle_id": vehicle_id},
                    )
                )
                self._queue.append(ToolCall(name="dispatch_order", args={"request_id": order.id}))
            if self._queue:
                return self._queue.pop(0)
        self._phase = "read"
        minutes = min(EPOCH_MINUTES, self._horizon - self._now)
        return ToolCall(name="advance", args={"minutes": minutes})

    def _route_end(self, vehicle: _Vehicle) -> tuple[Point, float] | None:
        """Where and when the vehicle finishes its current route, from cached orders."""
        position = vehicle.position
        time = self._now
        if vehicle.status == "broken_down":
            time = max(time, vehicle.repaired_at or time)
        for request_id, action in vehicle.route:
            order = self._orders.get(request_id)
            if order is None:
                return None
            target = order.pickup if action == "pickup" else order.dropoff
            time += math.dist(position, target)
            if action == "pickup":
                time = max(time, order.ready)
            position = target
        return position, time

    def _plan(self) -> list[tuple[_Order, str]]:
        free: dict[str, tuple[Point, float]] = {}
        for vehicle in self._vehicles.values():
            if vehicle.status == "off_shift":
                continue
            end = self._route_end(vehicle)
            if end is not None:
                free[vehicle.id] = end
        remaining = sorted(self._open.values(), key=lambda o: o.id)
        plans: list[tuple[_Order, str]] = []
        while remaining:
            best: tuple[float, _Order, str, Point, float] | None = None
            for order in remaining:
                for vehicle_id, (position, time) in sorted(free.items()):
                    vehicle = self._vehicles[vehicle_id]
                    if order.load > vehicle.capacity or time > self._now + LOOKAHEAD_MINUTES:
                        continue
                    approach = math.dist(position, order.pickup)
                    trip = math.dist(order.pickup, order.dropoff)
                    pickup_time = max(time + approach, order.ready)
                    delivery = pickup_time + trip
                    if delivery > min(vehicle.shift_end, self._horizon):
                        continue
                    late = max(0.0, delivery - order.deadline)
                    value = order.value * max(0.0, 1 - late / LATE_WINDOW)
                    profit = value - vehicle.cost * (approach + trip)
                    if profit <= 0:
                        continue
                    score = profit / (delivery - time + 1)
                    if best is None or score > best[0] + 1e-12:
                        best = (score, order, vehicle_id, order.dropoff, delivery)
            if best is None:
                break
            _, order, vehicle_id, end, delivery = best
            plans.append((order, vehicle_id))
            free[vehicle_id] = (end, delivery)
            remaining.remove(order)
        return plans


@register_policy("reference", "fleetbase_dispatch")
@register_policy("reference", "fleetbase_sim")
class ReferenceOperator(Operator):
    """Profit-per-minute append insertion over the open orders, every epoch."""

    name = "reference"


@register_policy("greedy", "fleetbase_dispatch")
@register_policy("greedy", "fleetbase_sim")
class GreedyOperator(Operator):
    """Each idle vehicle takes the open order with the nearest pickup."""

    name = "greedy"

    def _plan(self) -> list[tuple[_Order, str]]:
        remaining = sorted(self._open.values(), key=lambda o: o.id)
        plans: list[tuple[_Order, str]] = []
        for vehicle in sorted(self._vehicles.values(), key=lambda v: v.id):
            if vehicle.status != "idle" or vehicle.route:
                continue
            fits = [o for o in remaining if o.load <= vehicle.capacity]
            if not fits:
                continue
            order = min(fits, key=lambda o: (math.dist(vehicle.position, o.pickup), o.id))
            plans.append((order, vehicle.id))
            remaining.remove(order)
        return plans


@register_policy("stop_early", "fleetbase_dispatch")
@register_policy("stop_early", "fleetbase_sim")
class StopEarlyOperator(Operator):
    """Dispatch one order with the reference workflow, then finish."""

    name = "stop_early"
    stop_after_dispatch = True


__all__ = ["GreedyOperator", "Operator", "ReferenceOperator", "StopEarlyOperator"]

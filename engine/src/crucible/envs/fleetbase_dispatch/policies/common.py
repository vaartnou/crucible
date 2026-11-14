"""Parsing of public tool results and the simple epoch loop of the baseline operators."""

import math
import re
from dataclasses import dataclass

from pydantic import JsonValue

from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec

EPOCH_MINUTES = 10.0
LOOKAHEAD_MINUTES = 10.0
LATE_WINDOW = 30.0
OPEN = ("requested", "created")
WORKFLOW = frozenset({"create_order", "assign_order", "dispatch_order"})

Point = tuple[float, float]


def number(value: JsonValue, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def point(value: JsonValue) -> Point | None:
    if isinstance(value, list) and len(value) == 2:
        return number(value[0]), number(value[1])
    return None


def text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def instruction_number(instruction: str, pattern: str, default: float) -> float:
    """A number the public instruction states, found with `pattern` (one group)."""
    match = re.search(pattern, instruction)
    return float(match.group(1)) if match else default


@dataclass(frozen=True)
class OrderView:
    id: str
    status: str
    pickup: Point
    dropoff: Point
    load: float
    ready: float
    deadline: float
    value: float
    released_at: float = 0.0
    vehicle_id: str | None = None
    dispatched: bool = False

    @staticmethod
    def parse(data: JsonValue) -> OrderView | None:
        if not isinstance(data, dict):
            return None
        order_id, pickup, dropoff = (
            text(data.get("id")),
            point(data.get("pickup")),
            point(data.get("dropoff")),
        )
        if order_id is None or pickup is None or dropoff is None:
            return None
        return OrderView(
            order_id,
            text(data.get("status")) or "requested",
            pickup,
            dropoff,
            number(data.get("load"), 1),
            number(data.get("ready_time")),
            number(data.get("deadline")),
            number(data.get("value")),
            number(data.get("released_at")),
            text(data.get("vehicle_id")),
            data.get("dispatched") is True,
        )


@dataclass(frozen=True)
class VehicleView:
    id: str
    status: str
    position: Point
    capacity: float
    cost: float
    shift_end: float
    repaired_at: float | None
    route: tuple[tuple[str, str], ...]
    onboard: tuple[str, ...] = ()
    assigned: tuple[str, ...] = ()

    @staticmethod
    def parse(data: JsonValue) -> VehicleView | None:
        if not isinstance(data, dict):
            return None
        vehicle_id, position = text(data.get("id")), point(data.get("position"))
        if vehicle_id is None or position is None:
            return None
        route: list[tuple[str, str]] = []
        raw_route = data.get("route")
        for stop in raw_route if isinstance(raw_route, list) else []:
            if isinstance(stop, dict):
                request_id, action = text(stop.get("request_id")), text(stop.get("action"))
                if request_id is not None and action is not None:
                    route.append((request_id, action))
        repaired = data.get("repaired_at")
        raw_onboard = data.get("onboard")
        raw_assigned = data.get("assigned_orders")
        return VehicleView(
            vehicle_id,
            text(data.get("status")) or "idle",
            position,
            number(data.get("capacity")),
            number(data.get("cost_per_distance")),
            number(data.get("shift_end"), math.inf),
            number(repaired) if repaired is not None else None,
            tuple(route),
            tuple(
                o
                for o in (raw_onboard if isinstance(raw_onboard, list) else [])
                if isinstance(o, str)
            ),
            tuple(
                o
                for o in (raw_assigned if isinstance(raw_assigned, list) else [])
                if isinstance(o, str)
            ),
        )


def pages_follow_up(call: ToolCall, content: dict[str, JsonValue]) -> ToolCall | None:
    """The next page of a paged list call, or None after the last page."""
    page, pages = number(content.get("page"), 1), number(content.get("pages"), 1)
    if page >= pages:
        return None
    return ToolCall(name=call.name, args={**call.args, "page": int(page) + 1})


class Operator:
    """Epoch loop of the baseline operators. Subclasses override `_plan`.

    Every `EPOCH_MINUTES`: read the vehicles and the open orders (status ``requested``
    and, with `reads_created`, ``created``, which includes orders a breakdown
    unassigned), plan, run create/assign/dispatch for each planned order, advance.
    """

    name = "operator"
    stop_after_dispatch = False
    reads_created = True

    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self._queue: list[ToolCall] = []
        self._last: ToolCall | None = None
        self._phase = "read"
        self._now = 0.0
        self._horizon = math.inf
        self._orders: dict[str, OrderView] = {}
        self._open: dict[str, OrderView] = {}
        self._vehicles: dict[str, VehicleView] = {}
        self._failed: set[str] = set()
        self._dispatched = False

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._reset_state()

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is not None and self._last is not None:
            self._absorb(self._last, obs.last_result)
        elif "now" in obs.data:
            self._now = number(obs.data.get("now"))
            self._horizon = number(obs.data.get("horizon"), math.inf)
        call = self._next()
        self._last = call
        return call

    # Results -------------------------------------------------------------------------

    def _absorb(self, call: ToolCall, result: ToolResult) -> None:
        request_id = text(call.args.get("request_id"))
        if not result.ok:
            if call.name in WORKFLOW and request_id is not None:
                self._failed.add(request_id)
                self._queue = [c for c in self._queue if c.args.get("request_id") != request_id]
            return
        content = result.content
        if not isinstance(content, dict):
            return
        if call.name == "list_vehicles":
            items = content.get("vehicles")
            for item in items if isinstance(items, list) else []:
                vehicle = VehicleView.parse(item)
                if vehicle is not None:
                    self._vehicles[vehicle.id] = vehicle
        elif call.name == "list_orders":
            items = content.get("orders")
            for item in items if isinstance(items, list) else []:
                order = OrderView.parse(item)
                if order is None:
                    continue
                self._orders[order.id] = order
                if order.status in OPEN and order.id not in self._failed:
                    self._open[order.id] = order
            follow = pages_follow_up(call, content)
            if follow is not None:
                self._queue.insert(0, follow)
        elif call.name in WORKFLOW:
            order = OrderView.parse(content)
            if order is not None:
                self._orders[order.id] = order
            if call.name == "dispatch_order":
                self._dispatched = True
        elif call.name == "advance":
            self._now = number(content.get("now"), self._now)

    # Decisions -----------------------------------------------------------------------

    def _reads(self) -> list[ToolCall]:
        reads = [
            ToolCall(name="list_vehicles", args={"page": 1}),
            ToolCall(name="list_orders", args={"status": "requested", "page": 1}),
        ]
        if self.reads_created:
            reads.append(ToolCall(name="list_orders", args={"status": "created", "page": 1}))
        return reads

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
            reads = self._reads()
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
                self._queue += self._after_dispatch(order, vehicle_id)
            self._queue += self._extra_calls()
            if self._queue:
                return self._queue.pop(0)
        self._phase = "read"
        minutes = min(EPOCH_MINUTES, self._horizon - self._now)
        return ToolCall(name="advance", args={"minutes": minutes})

    def _plan(self) -> list[tuple[OrderView, str]]:
        return []

    def _after_dispatch(self, order: OrderView, vehicle_id: str) -> list[ToolCall]:
        """Calls queued right after an order's dispatch. None by default."""
        return []

    def _extra_calls(self) -> list[ToolCall]:
        """Calls queued after the epoch's workflow. None by default."""
        return []

    def _route_end(self, vehicle: VehicleView) -> tuple[Point, float] | None:
        """Where and when the vehicle finishes its route, from the cached order records."""
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

    def _idle_plan(self, pick: str) -> list[tuple[OrderView, str]]:
        """Each vehicle whose route ends within `LOOKAHEAD_MINUTES`, in ID order, gets the
        `nearest` (pickup closest to the route end) or `oldest` open order that fits,
        appended to its route."""
        remaining = sorted(self._open.values(), key=lambda o: o.id)
        plans: list[tuple[OrderView, str]] = []
        for vehicle in sorted(self._vehicles.values(), key=lambda v: v.id):
            if vehicle.status in {"off_shift", "broken_down"}:
                continue
            end = self._route_end(vehicle)
            if end is None or end[1] > self._now + LOOKAHEAD_MINUTES:
                continue
            fits = [o for o in remaining if o.load <= vehicle.capacity]
            if not fits:
                continue
            position = end[0]
            if pick == "nearest":
                order = min(fits, key=lambda o: (math.dist(position, o.pickup), o.id))
            else:
                order = min(fits, key=lambda o: (o.released_at, o.id))
            plans.append((order, vehicle.id))
            remaining.remove(order)
        return plans


__all__ = [
    "EPOCH_MINUTES",
    "LATE_WINDOW",
    "LOOKAHEAD_MINUTES",
    "OPEN",
    "WORKFLOW",
    "Operator",
    "OrderView",
    "Point",
    "VehicleView",
    "instruction_number",
    "number",
    "pages_follow_up",
    "point",
    "text",
]

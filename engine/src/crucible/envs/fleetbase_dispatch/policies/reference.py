"""The reference operator: online regret insertion with 2-opt, public tools only.

Cycle (repeated until the horizon):

1. ``list_events(since)`` until no more. Releases, unassignments, pickups,
   deliveries, breakdowns and traffic zones update the operator's view.
2. ``list_vehicles``; ``list_orders`` for new releases (``requested``), for orders a
   breakdown unassigned (``created``), and for the orders in progress (``dispatched``
   and ``picked_up``). The last two find silent customer changes: a changed dropoff
   shows on the record, a cancelled order disappears from both lists.
3. Plan with `solver.plan` (public knowledge only: released orders, current dropoffs,
   announced traffic zones, repair times, shift ends), once from the current routes
   and once from scratch, and keep the better plan.
4. Apply the difference: unassign dropped orders, create/assign/dispatch new ones,
   and ``set_vehicle_route`` where the stop order changed.
5. ``advance`` by 5 minutes. When an advance brings no public event, the next cycle
   only advances again; a second quiet advance in a row runs a full cycle anyway, so
   silent changes are seen within 10 minutes.

The operator counts its calls against the budget in the reset observation and keeps
a reserve; with few calls left it stops polling and finally finishes.
"""

import math
from dataclasses import replace

from pydantic import JsonValue

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec
from crucible.envs.fleetbase_dispatch.policies.common import (
    LATE_WINDOW,
    OrderView,
    VehicleView,
    instruction_number,
    number,
    pages_follow_up,
    text,
)
from crucible.envs.fleetbase_dispatch.policies.solver import (
    DROPOFF,
    PICKUP,
    Job,
    Problem,
    Settings,
    Stop,
    Truck,
    Zone,
    plan,
    route_profit,
)

EPOCH_MINUTES = 5.0
QUIET_LIMIT = 1  # quiet advances in a row before a full cycle anyway
RESERVE = 6  # calls kept for the end of the episode
# The opportunity cost per truck-minute keeps trucks free for later, better orders.
SETTINGS = Settings(regret=1.0, passes=2, min_gain=0.5, time_cost=0.2)
# A fresh plan replaces the incremental one when it is better by more than this.
FRESH_GAIN = 1.0
_ACTIVE = ("requested", "created", "assigned", "dispatched")
_IN_PROGRESS = ("dispatched", "picked_up")


def _total(problem: Problem, routes: dict[str, list[Stop]]) -> float:
    return sum(route_profit(problem, t, routes.get(t.id, [])) for t in problem.trucks)


@register_policy("reference", "fleetbase_dispatch")
@register_policy("reference", "fleetbase_sim")
class ReferenceOperator:
    """Online regret insertion + 2-opt, replanning after every public change."""

    name = "reference"
    stop_after_dispatch = False

    def __init__(self) -> None:
        self._budget = 0
        self._late_window = LATE_WINDOW
        self._speed = 1.0
        self._init_state()

    def _init_state(self) -> None:
        self._queue: list[ToolCall] = []
        self._last: ToolCall | None = None
        self._phase = "events"
        self._used = 0
        self._now = 0.0
        self._horizon = math.inf
        self._since = 0
        self._latest = 0
        self._quiet = 0
        self._orders: dict[str, OrderView] = {}
        self._vehicles: dict[str, VehicleView] = {}
        self._zones: list[Zone] = []
        self._closed: set[str] = set()
        self._need_requested = True
        self._need_created = False
        self._stale: set[str] = set()
        self._failures = 0
        self._retried = False
        self._dispatched = False
        self._fresh = False

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._init_state()
        self._budget = task_spec.max_steps
        self._late_window = instruction_number(
            instruction, r"to zero over (\d+(?:\.\d+)?) minutes", LATE_WINDOW
        )
        self._speed = instruction_number(instruction, r"at (\d+(?:\.\d+)?) distance unit", 1.0)

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is not None and self._last is not None:
            self._absorb(self._last, obs.last_result)
        elif "now" in obs.data:
            self._now = number(obs.data.get("now"))
            self._horizon = number(obs.data.get("horizon"), math.inf)
            self._budget = int(number(obs.data.get("budget"), self._budget))
        call = self._next()
        self._last = call
        self._used += 1
        return call

    @property
    def _left(self) -> int:
        return self._budget - self._used

    # Results -------------------------------------------------------------------------

    def _absorb(self, call: ToolCall, result: ToolResult) -> None:
        if not result.ok:
            self._failures += 1
            request_id = text(call.args.get("request_id"))
            if request_id is not None:
                self._queue = [c for c in self._queue if c.args.get("request_id") != request_id]
            # A route built on a wrong view fails; replan from fresh reads instead.
            self._queue = [c for c in self._queue if c.name != "set_vehicle_route"]
            return
        content = result.content
        if not isinstance(content, dict):
            return
        if call.name == "list_events":
            self._absorb_events(content)
        elif call.name == "list_vehicles":
            items = content.get("vehicles")
            for item in items if isinstance(items, list) else []:
                vehicle = VehicleView.parse(item)
                if vehicle is not None:
                    self._vehicles[vehicle.id] = vehicle
            follow = pages_follow_up(call, content)
            if follow is not None:
                self._queue.insert(0, follow)
        elif call.name == "list_orders":
            items = content.get("orders")
            for item in items if isinstance(items, list) else []:
                self._store(OrderView.parse(item))
            follow = pages_follow_up(call, content)
            if follow is not None:
                self._queue.insert(0, follow)
        elif call.name in {"create_order", "assign_order", "dispatch_order", "unassign_order"}:
            self._store(OrderView.parse(content))
            if call.name == "dispatch_order":
                self._dispatched = True
        elif call.name == "set_vehicle_route":
            vehicle = VehicleView.parse(content)
            if vehicle is not None:
                self._vehicles[vehicle.id] = vehicle
        elif call.name == "advance":
            self._now = number(content.get("now"), self._now)
            self._latest = int(number(content.get("latest_event_seq"), self._latest))

    def _store(self, order: OrderView | None) -> None:
        if order is None:
            return
        self._orders[order.id] = order
        self._stale.discard(order.id)
        if order.status in {"delivered", "delivered_late", "cancelled"}:
            self._closed.add(order.id)

    def _absorb_events(self, content: dict[str, JsonValue]) -> None:
        events = content.get("events")
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            kind = text(event.get("kind"))
            request_id = text(event.get("request_id"))
            if kind == "released":
                self._need_requested = True
            elif kind == "unassigned":
                self._need_created = True
            elif kind in {"delivered", "delivered_late", "agent_cancelled"} and request_id:
                self._closed.add(request_id)
            elif kind == "picked_up" and request_id in self._orders:
                order = self._orders[request_id]
                self._orders[request_id] = replace(order, status="picked_up")
            elif kind == "rejected":
                self._need_created = True
            elif kind == "traffic_zone":
                bounds = event.get("bounds")
                if isinstance(bounds, list) and len(bounds) == 4:
                    b = [number(v) for v in bounds]
                    self._zones.append(
                        Zone(
                            (b[0], b[1], b[2], b[3]),
                            number(event.get("start")),
                            number(event.get("end")),
                            number(event.get("factor"), 1.0) or 1.0,
                        )
                    )
        self._since = int(number(content.get("next_since"), self._since))
        self._latest = max(self._latest, self._since)
        if content.get("more") is True:
            self._queue.insert(0, ToolCall(name="list_events", args={"since": self._since}))

    # Decisions -----------------------------------------------------------------------

    def _next(self) -> ToolCall:
        if self.stop_after_dispatch and self._dispatched:
            return ToolCall(name="finish")
        if self._left <= 1 or self._now >= self._horizon:
            return ToolCall(name="finish")
        if self._queue:
            return self._queue.pop(0)
        while True:
            if self._phase == "after_advance":
                self._phase = "events"
                if self._latest <= self._since and self._quiet < QUIET_LIMIT:
                    self._quiet += 1
                    return self._advance()
                self._quiet = 0
            elif self._phase == "events":
                self._phase = "reads"
                if self._left > RESERVE:
                    return ToolCall(name="list_events", args={"since": self._since})
            elif self._phase == "reads":
                self._phase = "plan"
                reads = self._reads()
                self._fresh = bool(reads)
                if reads:
                    self._queue = reads[1:]
                    return reads[0]
            elif self._phase == "plan":
                self._phase = "advance"
                for request_id in self._stale:
                    # Silently cancelled: in neither in-progress list and not delivered.
                    self._closed.add(request_id)
                self._stale = set()
                self._failures = 0
                # Without fresh reads the view may be stale: change nothing.
                actions = self._actions() if self._fresh else []
                if actions:
                    self._queue = actions
                    return self._queue.pop(0)
            else:
                if self._failures and not self._retried and self._left > 4 * RESERVE:
                    self._retried = True
                    self._failures = 0
                    self._phase = "events"
                    continue
                self._retried = False
                return self._advance()

    def _advance(self) -> ToolCall:
        minutes = min(EPOCH_MINUTES, self._horizon - self._now)
        if self._left <= RESERVE:
            minutes = self._horizon - self._now
        self._phase = "after_advance"
        return ToolCall(name="advance", args={"minutes": minutes})

    def _reads(self) -> list[ToolCall]:
        if self._left <= 2 * RESERVE:
            return []
        reads = [ToolCall(name="list_vehicles", args={"page": 1})]
        if self._need_requested:
            reads.append(ToolCall(name="list_orders", args={"status": "requested", "page": 1}))
        if self._need_created:
            reads.append(ToolCall(name="list_orders", args={"status": "created", "page": 1}))
        self._need_requested = self._need_created = False
        self._stale = set()
        if self._left > 4 * RESERVE:
            # A dispatched order can be picked up since, so both lists are read.
            self._stale = {
                o.id
                for o in self._orders.values()
                if o.status in _IN_PROGRESS and o.id not in self._closed
            }
            if self._stale:
                for status in _IN_PROGRESS:
                    reads.append(ToolCall(name="list_orders", args={"status": status, "page": 1}))
        return reads

    # Planning ------------------------------------------------------------------------

    def _problem(self) -> tuple[Problem, dict[str, list[Stop]]]:
        lw, now = self._late_window, self._now
        trucks: list[Truck] = []
        onboard: set[str] = set()
        for vehicle in sorted(self._vehicles.values(), key=lambda v: v.id):
            if vehicle.status == "off_shift":
                continue
            start = now
            if vehicle.status == "broken_down" and vehicle.repaired_at is not None:
                start = max(now, vehicle.repaired_at)
            cargo = tuple(o for o in vehicle.onboard if o in self._orders)
            onboard |= set(vehicle.onboard)
            trucks.append(
                Truck(
                    vehicle.id,
                    vehicle.position,
                    start,
                    int(vehicle.capacity),
                    vehicle.cost,
                    vehicle.shift_end,
                    cargo,
                )
            )
        jobs: dict[str, Job] = {}
        for order in self._orders.values():
            if order.id in self._closed:
                continue
            if order.id not in onboard:
                if order.status not in _ACTIVE:
                    continue
                trip = math.dist(order.pickup, order.dropoff) / self._speed
                if now + trip > order.deadline + lw:
                    continue
            jobs[order.id] = Job(
                order.id,
                order.pickup,
                order.dropoff,
                int(order.load),
                order.ready,
                order.deadline,
                order.value,
                release=min(order.released_at, now),
            )
        routes = {
            vehicle.id: [stop for stop in vehicle.route if stop[0] in jobs]
            for vehicle in self._vehicles.values()
        }
        problem = Problem(
            now=now,
            horizon=self._horizon,
            jobs=jobs,
            trucks=tuple(trucks),
            zones=tuple(z for z in self._zones if z.end > now),
            speed=self._speed,
            late_window=lw,
        )
        return problem, routes

    def _actions(self) -> list[ToolCall]:
        if not self._vehicles:
            return []
        problem, current = self._problem()
        if not problem.trucks:
            return []
        routes = plan(problem, current, SETTINGS)
        fresh = plan(problem, None, SETTINGS)
        if _total(problem, fresh) > _total(problem, routes) + FRESH_GAIN:
            routes = fresh
        # Cargo on board stays with its vehicle, also on a vehicle whose shift ended.
        onboard = {j for v in self._vehicles.values() for j in v.onboard}
        holder: dict[str, str] = {}
        dispatched: set[str] = set()
        for vehicle in self._vehicles.values():
            for request_id, _ in vehicle.route:
                holder[request_id] = vehicle.id
                dispatched.add(request_id)
            for request_id in vehicle.assigned:
                holder[request_id] = vehicle.id
        planned = {rid: truck for truck, route in routes.items() for rid, _ in route}
        calls: list[ToolCall] = []
        for request_id in sorted(holder):
            if request_id not in planned and request_id not in onboard:
                calls.append(ToolCall(name="unassign_order", args={"request_id": request_id}))
        route_calls: list[ToolCall] = []
        for truck in sorted(routes):
            route = routes[truck]
            appended: list[Stop] = []
            seen: set[str] = set()
            for request_id, _ in route:
                if request_id in seen or request_id in onboard:
                    continue
                seen.add(request_id)
                args: dict[str, JsonValue] = {"request_id": request_id}
                if self._orders[request_id].status == "requested":
                    calls.append(ToolCall(name="create_order", args=args))
                if holder.get(request_id) != truck:
                    calls.append(
                        ToolCall(
                            name="assign_order",
                            args={"request_id": request_id, "vehicle_id": truck},
                        )
                    )
                    appended += [(request_id, PICKUP), (request_id, DROPOFF)]
                if request_id not in dispatched:
                    calls.append(ToolCall(name="dispatch_order", args=args))
            vehicle = self._vehicles[truck]
            expected = [s for s in vehicle.route if planned.get(s[0]) == truck or s[0] in onboard]
            expected += appended
            undispatched = any(r in vehicle.assigned for r in planned if planned[r] == truck)
            if expected != route or undispatched:
                stops: list[JsonValue] = [
                    {"request_id": request_id, "action": action} for request_id, action in route
                ]
                route_calls.append(
                    ToolCall(name="set_vehicle_route", args={"vehicle_id": truck, "stops": stops})
                )
        calls += route_calls
        # Stay within the budget: keep the reserve for advancing and finishing.
        room = max(0, self._left - RESERVE)
        return calls[:room]


__all__ = ["ReferenceOperator"]

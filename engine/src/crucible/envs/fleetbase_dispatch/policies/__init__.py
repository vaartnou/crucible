"""Native-workflow operators that use only documented public observations/tools.

Each operator is registered for `fleetbase_dispatch` and for `fleetbase_sim`, which share
tools and observations.
"""

import math
from dataclasses import dataclass

from pydantic import JsonValue

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec


@dataclass(frozen=True)
class _Request:
    id: str
    pickup: tuple[float, float]
    dropoff: tuple[float, float]
    load: float
    deadline: float
    value: float
    status: str
    native_id: str | None
    vehicle_id: str | None


@dataclass(frozen=True)
class _Vehicle:
    id: str
    position: tuple[float, float]
    capacity: float
    current_request: str | None


@dataclass(frozen=True)
class _View:
    now: float
    horizon: float
    speed: float
    cost: float
    next_release: float | None
    requests: tuple[_Request, ...]
    vehicles: tuple[_Vehicle, ...]


def _number(value: JsonValue, default: float = 0) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _point(value: JsonValue) -> tuple[float, float]:
    if isinstance(value, list) and len(value) == 2:
        return _number(value[0]), _number(value[1])
    return 0.0, 0.0


def _view(obs: Observation) -> _View | None:
    data = obs.data
    if obs.last_result is not None and isinstance(obs.last_result.content, dict):
        data = obs.last_result.content
    if "now" not in data or not isinstance(data.get("requests"), list):
        return None
    requests: list[_Request] = []
    raw_requests = data.get("requests")
    if isinstance(raw_requests, list):
        for item in raw_requests:
            if not isinstance(item, dict):
                continue
            request_id = _string(item.get("id"))
            if request_id is None:
                continue
            requests.append(
                _Request(
                    request_id,
                    _point(item.get("pickup")),
                    _point(item.get("dropoff")),
                    _number(item.get("load")),
                    _number(item.get("deadline")),
                    _number(item.get("value")),
                    _string(item.get("status")) or "waiting",
                    _string(item.get("native_id")),
                    _string(item.get("vehicle_id")),
                )
            )
    vehicles: list[_Vehicle] = []
    raw_vehicles = data.get("vehicles")
    if isinstance(raw_vehicles, list):
        for item in raw_vehicles:
            if not isinstance(item, dict):
                continue
            vehicle_id = _string(item.get("id"))
            if vehicle_id is None:
                continue
            vehicles.append(
                _Vehicle(
                    vehicle_id,
                    _point(item.get("position")),
                    _number(item.get("capacity")),
                    _string(item.get("current_request")),
                )
            )
    release = data.get("next_release")
    return _View(
        _number(data.get("now")),
        _number(data.get("horizon"), 180),
        _number(data.get("speed"), 1),
        _number(data.get("cost_per_distance"), 0.2),
        _number(release) if release is not None else None,
        tuple(requests),
        tuple(vehicles),
    )


def _availability(vehicle: _Vehicle, view: _View) -> tuple[tuple[float, float], float]:
    current = next((r for r in view.requests if r.id == vehicle.current_request), None)
    if current is None:
        return vehicle.position, view.now
    remaining = math.dist(vehicle.position, current.dropoff)
    if current.status != "picked_up":
        remaining = math.dist(vehicle.position, current.pickup) + math.dist(
            current.pickup, current.dropoff
        )
    return current.dropoff, view.now + remaining / view.speed


def _route_options(
    vehicle: _Vehicle, view: _View, requests: tuple[_Request, ...]
) -> dict[int, tuple[float, str | None]]:
    """Feasible routes per demand subset; shortest distance dominates time and cost.

    Every leg includes pickup **and** the terminal delivery. This prevents treating
    arrival at the final pickup as a feasible completed terminal tour.
    """
    position, available_at = _availability(vehicle, view)
    states: dict[tuple[int, int], tuple[float, str]] = {}
    options: dict[int, tuple[float, str | None]] = {0: (0.0, None)}
    for index, request in enumerate(requests):
        distance = math.dist(position, request.pickup) + math.dist(request.pickup, request.dropoff)
        arrival = available_at + distance / view.speed
        if (
            request.load <= vehicle.capacity
            and arrival <= min(request.deadline, view.horizon) + 1e-9
        ):
            states[(1 << index, index)] = (distance, request.id)
    # Increasing subset size gives a topological order for the route DP.
    for size in range(1, len(requests) + 1):
        layer = [(key, value) for key, value in states.items() if key[0].bit_count() == size]
        for (mask, last), (distance, first) in layer:
            value = sum(r.value for i, r in enumerate(requests) if mask & (1 << i))
            utility = value - view.cost * distance
            previous = options.get(mask)
            if previous is None or utility > previous[0] + 1e-9:
                options[mask] = (utility, first)
            for index, request in enumerate(requests):
                if mask & (1 << index) or request.load > vehicle.capacity:
                    continue
                extended = (
                    distance
                    + math.dist(requests[last].dropoff, request.pickup)
                    + math.dist(request.pickup, request.dropoff)
                )
                if (
                    available_at + extended / view.speed
                    > min(request.deadline, view.horizon) + 1e-9
                ):
                    continue
                key = (mask | (1 << index), index)
                if key not in states or extended < states[key][0] - 1e-9:
                    states[key] = (extended, first)
    return options


def _reference_assignment(view: _View) -> tuple[str, str] | None:
    active = {v.current_request for v in view.vehicles if v.current_request is not None}
    requests = tuple(
        r for r in view.requests if r.id not in active and r.status not in {"delivered", "late"}
    )
    if not requests:
        return None
    # Disjoint per-vehicle subsets maximize total utility on currently public demand.
    plans: dict[int, tuple[float, tuple[tuple[str, str | None], ...]]] = {0: (0, ())}
    for vehicle in view.vehicles:
        choices = _route_options(vehicle, view, requests)
        next_plans: dict[int, tuple[float, tuple[tuple[str, str | None], ...]]] = {}
        for used, (utility, selected) in plans.items():
            for mask, (route_utility, first) in choices.items():
                if used & mask:
                    continue
                merged = used | mask
                candidate = (utility + route_utility, (*selected, (vehicle.id, first)))
                if merged not in next_plans or candidate[0] > next_plans[merged][0] + 1e-9:
                    next_plans[merged] = candidate
        plans = next_plans
    _, selected = max(plans.values(), key=lambda plan: plan[0])
    idle = {v.id for v in view.vehicles if v.current_request is None}
    return next(
        ((request, vehicle) for vehicle, request in selected if vehicle in idle and request), None
    )


def _greedy_assignment(view: _View) -> tuple[str, str] | None:
    active = {v.current_request for v in view.vehicles if v.current_request is not None}
    candidates: list[tuple[float, str, str]] = []
    for vehicle in view.vehicles:
        if vehicle.current_request is not None:
            continue
        for request in view.requests:
            if request.id in active or request.status in {"delivered", "late"}:
                continue
            distance = math.dist(vehicle.position, request.pickup) + math.dist(
                request.pickup, request.dropoff
            )
            if (
                request.load <= vehicle.capacity
                and view.now + distance / view.speed <= min(request.deadline, view.horizon) + 1e-9
                and request.value > distance * view.cost
            ):
                candidates.append((distance, request.id, vehicle.id))
    if not candidates:
        return None
    _, request, vehicle = min(candidates)
    return request, vehicle


class WorkflowOperator:
    """Verify every native workflow step and reobserve boundedly after failures."""

    name = "reference"

    def __init__(self) -> None:
        self._pending: tuple[str, str] | None = None
        self._failures = 0
        self._failed: set[tuple[str, str]] = set()

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self._pending = None
        self._failures = 0
        self._failed.clear()

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is not None and not obs.last_result.ok:
            self._failures += 1
            if self._failures >= 3:
                if self._pending is not None:
                    self._failed.add(self._pending)
                self._pending = None
                self._failures = 0
            return ToolCall(name="observe")
        view = _view(obs)
        if view is None:
            return ToolCall(name="observe")
        if view.now >= view.horizon:
            return ToolCall(name="finish")
        if self._pending is not None:
            request_id, vehicle_id = self._pending
            request = next((r for r in view.requests if r.id == request_id), None)
            vehicle = next((v for v in view.vehicles if v.id == vehicle_id), None)
            if (
                request is None
                or vehicle is None
                or request.status in {"delivered", "late", "picked_up"}
                or (vehicle.current_request not in {None, request_id})
            ):
                self._pending = None
            elif request.status == "dispatched" and request.vehicle_id == vehicle_id:
                self._pending = None
                self._failures = 0
            else:
                return self._next_workflow(request, vehicle_id)
        blocked = {request_id for request_id, _ in self._failed}
        planning = _View(
            view.now,
            view.horizon,
            view.speed,
            view.cost,
            view.next_release,
            tuple(r for r in view.requests if r.id not in blocked),
            view.vehicles,
        )
        assignment = (
            _greedy_assignment(planning)
            if self.name == "greedy"
            else _reference_assignment(planning)
        )
        if assignment is not None and assignment not in self._failed:
            self._pending = assignment
            request_id, vehicle_id = assignment
            request = next(r for r in view.requests if r.id == request_id)
            return self._next_workflow(request, vehicle_id)
        # Wake when an active vehicle finishes, or when the next public wave arrives.
        times = [
            available
            for vehicle in view.vehicles
            if vehicle.current_request is not None
            for _, available in [_availability(vehicle, view)]
            if available > view.now + 1e-8
        ]
        if view.next_release is not None and view.next_release > view.now:
            times.append(view.next_release)
        if not times:
            return ToolCall(name="finish")
        minutes = min(min(times), view.horizon) - view.now
        return ToolCall(name="advance", args={"minutes": max(0.000001, minutes)})

    @staticmethod
    def _next_workflow(request: _Request, vehicle_id: str) -> ToolCall:
        if request.native_id is None:
            return ToolCall(name="create_order", args={"request_id": request.id})
        if request.vehicle_id != vehicle_id or request.status == "waiting":
            return ToolCall(
                name="assign_order", args={"request_id": request.id, "vehicle_id": vehicle_id}
            )
        return ToolCall(name="dispatch_order", args={"request_id": request.id})


@register_policy("reference", "fleetbase_dispatch")
@register_policy("reference", "fleetbase_sim")
class ReferenceOperator(WorkflowOperator):
    """Optimize feasible routes over all released demand using the public workflow."""

    name = "reference"


@register_policy("greedy", "fleetbase_dispatch")
@register_policy("greedy", "fleetbase_sim")
class GreedyOperator(WorkflowOperator):
    """Nearest feasible order baseline with the same native workflow and clock."""

    name = "greedy"


@register_policy("stop_early", "fleetbase_dispatch")
@register_policy("stop_early", "fleetbase_sim")
class StopEarlyOperator(WorkflowOperator):
    """Create, assign and dispatch one order, then invoke published continuation."""

    name = "stop_early"

    async def act(self, obs: Observation) -> ToolCall | None:
        view = _view(obs)
        if view is not None and any(
            r.status in {"dispatched", "picked_up", "delivered", "late"} for r in view.requests
        ):
            return ToolCall(name="finish")
        return await super().act(obs)

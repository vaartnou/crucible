"""Fleetbase dispatch workflow backed by private deterministic physical evidence.

`DispatchTaskBase` holds the tools, the physics bridge and grading. Subclasses only
choose the order backend through `_open_adapter`:

- `fleetbase_dispatch@1` uses native Fleetbase, or the in-memory backend when
  `FLEETBASE_BACKEND=memory`. Until plan step 5 the native adapter lacks the v2
  operations (cancel, update, unassign, routes), so v2 episodes need the memory backend.
- `fleetbase_sim@1` always uses the in-memory backend and runs the generic checks.

Tool results are small: an action returns only the record it touched, lists return
pages of `PAGE_SIZE` records, and the reset observation has only counts. The sim is
reconciled with the committed records after every successful mutation.

`update_order` can change only `note` (free text) and `priority` (low, normal, high).
Both are stored on the order record for the operator's own bookkeeping and have no
effect on physics or economics, so they cannot be exploited.
"""

import copy
import json
import os
from abc import abstractmethod
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from crucible.backends import (
    BackendConnection,
    BackendError,
    ExclusiveLeaseProvider,
    HttpBackendRuntime,
)
from crucible.core.registry import register_task
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskChecks, TaskSpec
from crucible.core.tools import (
    Observation,
    ToolCall,
    ToolKind,
    ToolResult,
    ToolSpec,
    tool_map,
    validate_call,
)
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter
from crucible.envs.fleetbase_dispatch.adapter_protocol import (
    DispatchAdapter,
    FleetbaseOperationError,
)
from crucible.envs.fleetbase_dispatch.deployment import make_bundle
from crucible.envs.fleetbase_dispatch.memory_adapter import InMemoryAdapter
from crucible.envs.fleetbase_dispatch.policies import EXPLOITS
from crucible.envs.fleetbase_dispatch.sim import Stop, WorldSimulator
from crucible.envs.fleetbase_dispatch.world import World, WorldConfig, make_world
from crucible.export.deployment import DeploymentBundle

BACKEND_ENV = "FLEETBASE_BACKEND"
PAGE_SIZE = 10
MAX_STEPS = 530  # The reference uses about 60 % on the dev sweep (docs/fleetbase-validation.md).
ORDER_STATUSES = (
    "requested",
    "created",
    "assigned",
    "dispatched",
    "picked_up",
    "delivered",
    "delivered_late",
    "cancelled",
)
OrderStatus = Literal[
    "requested",
    "created",
    "assigned",
    "dispatched",
    "picked_up",
    "delivered",
    "delivered_late",
    "cancelled",
]
_STRICT = ConfigDict(extra="forbid")
_MAX_MINUTES = WorldConfig().horizon


class EmptyArgs(BaseModel):
    model_config = _STRICT


class OrderArgs(BaseModel):
    model_config = _STRICT
    request_id: str


class ListOrdersArgs(BaseModel):
    model_config = _STRICT
    status: OrderStatus | None = None
    page: int = Field(1, ge=1)


class UpdateOrderArgs(BaseModel):
    model_config = _STRICT
    request_id: str
    note: str | None = Field(None, max_length=200)
    priority: Literal["low", "normal", "high"] | None = None


class AssignArgs(BaseModel):
    model_config = _STRICT
    request_id: str
    vehicle_id: str


class StopArgs(BaseModel):
    model_config = _STRICT
    request_id: str
    action: Literal["pickup", "dropoff"]


class RouteArgs(BaseModel):
    model_config = _STRICT
    vehicle_id: str
    stops: list[StopArgs] = Field(max_length=100)


class PageArgs(BaseModel):
    model_config = _STRICT
    page: int = Field(1, ge=1)


class VehicleArgs(BaseModel):
    model_config = _STRICT
    vehicle_id: str


class RepositionArgs(BaseModel):
    model_config = _STRICT
    vehicle_id: str
    point: list[float] = Field(min_length=2, max_length=2)


class EventsArgs(BaseModel):
    model_config = _STRICT
    since: int = Field(0, ge=0)


class AdvanceArgs(BaseModel):
    model_config = _STRICT
    minutes: float = Field(ge=0, le=_MAX_MINUTES, allow_inf_nan=False)


def _tool(name: str, description: str, kind: ToolKind, args: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description, kind=kind, args_model=args)


_TOOLS = (
    _tool(
        "list_orders",
        f"List released orders, {PAGE_SIZE} per page, optionally filtered by status.",
        "read",
        ListOrdersArgs,
    ),
    _tool("get_order", "Read one released order.", "read", OrderArgs),
    _tool("create_order", "Create the native order for a released request.", "action", OrderArgs),
    _tool(
        "update_order",
        "Set the order's note and/or priority (bookkeeping only).",
        "action",
        UpdateOrderArgs,
    ),
    _tool("cancel_order", "Cancel an order before pickup.", "action", OrderArgs),
    _tool(
        "assign_order",
        "Assign an order to a vehicle and its driver before pickup. A new vehicle gets "
        "the order's stops appended to the end of its route.",
        "action",
        AssignArgs,
    ),
    _tool("unassign_order", "Remove an order from its vehicle before pickup.", "action", OrderArgs),
    _tool(
        "dispatch_order",
        "Dispatch an assigned order: its vehicle starts serving its stops.",
        "action",
        OrderArgs,
    ),
    _tool(
        "set_vehicle_route",
        "Reorder a vehicle's route. List every remaining stop of the orders assigned to it.",
        "action",
        RouteArgs,
    ),
    _tool("list_vehicles", f"List vehicles, {PAGE_SIZE} per page.", "read", PageArgs),
    _tool("get_vehicle", "Read one vehicle.", "read", VehicleArgs),
    _tool(
        "reposition_vehicle",
        "Send an idle vehicle toward a point [x, y].",
        "action",
        RepositionArgs,
    ),
    _tool(
        "list_events",
        f"List public events with sequence number above `since`, {PAGE_SIZE} at a time.",
        "read",
        EventsArgs,
    ),
    _tool("get_forecast", "Expected demand per zone for the next wave.", "read", EmptyArgs),
    _tool(
        "estimate_route",
        "Estimate times, loads and cost of a stop list for a vehicle. Changes nothing.",
        "read",
        RouteArgs,
    ),
    _tool("advance", "Let time pass by the given minutes.", "clock", AdvanceArgs),
    _tool("finish", "End the episode. Dispatched work continues to the horizon.", "end", EmptyArgs),
)
_TOOL_MAP = tool_map(_TOOLS)


def _fail(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message)


def _coords(values: tuple[float, ...] | list[float]) -> str:
    """Coordinates as they print inside a JSON list, for the hidden-state check."""
    return json.dumps(list(values))[1:-1]


class DispatchTaskBase(Task):
    """One dispatch episode. Not registered; subclasses set `spec` and `_open_adapter`."""

    scoring_hash: ClassVar[str] = "fleetbase-physical-economics-v2"
    world_config: ClassVar[WorldConfig] = WorldConfig()

    def __init__(self) -> None:
        self._adapter: DispatchAdapter | None = None
        self._sim: WorldSimulator | None = None
        self._score: Score | None = None
        self._ended = False
        self._evidence: dict[str, JsonValue] | None = None
        self._rejected_operations = 0
        self._calls = 0
        self._public_time = 0.0
        self._episode_id = ""
        self._started = False
        self._resources_closed = False
        self._cleanup_failed = False

    # Hooks for subclasses ------------------------------------------------------------

    @abstractmethod
    async def _open_adapter(self, seed: int, salt: str | None) -> DispatchAdapter:
        """Acquire the episode's backend, set `_episode_id` and return its adapter."""

    async def _close_backend(self) -> None:
        """Release backend infrastructure after adapter cleanup. Must tolerate repeats."""
        return None

    def _make_world(self, seed: int, salt: str | None) -> World:
        return make_world(seed, salt, self.world_config)

    def _make_sim(self, world: World) -> WorldSimulator:
        return WorldSimulator(world)

    # Task API ------------------------------------------------------------------------

    def instruction(self) -> str:
        c = self.world_config
        return (
            "You dispatch a delivery fleet through order-management tools. "
            f"There are {c.vehicles} vehicles. Each has a capacity, a cost per distance unit "
            "and a shift end, all shown on its record. Customer requests are released over "
            f"a {c.horizon:g}-minute horizon in {c.waves} waves, and some urgent requests "
            "are released between waves. Only released requests are visible. get_forecast "
            "gives the expected demand per zone for the next wave only.\n"
            "Workflow: create_order for a released request, assign_order to a vehicle, "
            "then dispatch_order. Only dispatched orders are served. A vehicle follows its "
            "route: the ordered pickup and dropoff stops of its dispatched orders. "
            "assign_order appends the order's stops to the end of the route; "
            "set_vehicle_route reorders it. The cargo on board must not exceed the "
            "capacity at any stop. A pickup cannot happen before the order's ready time. "
            "Before pickup an order can be reassigned, unassigned or cancelled; after "
            "pickup it stays with its vehicle. update_order changes only a note and a "
            "priority, which have no effect on the operation. reposition_vehicle moves an "
            "idle vehicle.\n"
            "Tool calls take no time. advance(minutes) moves time forward. Vehicles drive "
            f"in straight lines at {c.speed:g} distance unit per minute and wait at a pickup "
            "until its ready time. Inside an announced traffic zone, speed is multiplied by "
            "the zone's factor. A vehicle that breaks down stops for the announced time; "
            "its orders that are not picked up become unassigned, and cargo on board "
            "stays on board. A vehicle stops for good at its shift end. Customers can "
            "cancel an order or change its dropoff at any time before delivery; these "
            "changes appear only on the order record, not in the event feed. Cargo of a "
            "cancelled order is unloaded where the vehicle is and earns nothing. "
            "Breakdowns, traffic zones, releases, pickups and deliveries appear in "
            "list_events.\n"
            "Economics: delivery by the deadline earns the order's value. A late delivery "
            "earns the value reduced linearly to zero over "
            f"{c.late_window:g} minutes after the deadline. Every distance unit a vehicle "
            "drives costs that vehicle's cost per distance. If you cancel an order that was "
            f"ever dispatched, you pay a fee of {c.cancellation_fee:g}; customer "
            "cancellations cost nothing. Utility U = on-time value + late value - driving "
            "cost - cancellation fees. Reward = clip(U / U_hindsight, 0, 1), where "
            "U_hindsight is the private utility that a planner reaches in the same world "
            "when it knows all requests and disruptions in advance. Only physical delivery "
            "counts; the order status in the application is not evidence.\n"
            "Actions return only the record they changed. Lists return "
            f"{PAGE_SIZE} records per page. Rejected calls change nothing and have no reward "
            f"penalty. You have {self.spec.max_steps} tool calls. finish, or running out of "
            "calls, ends your control: dispatched work continues to the horizon and no new "
            "work is started."
        )

    def tools(self) -> list[ToolSpec]:
        return list(_TOOLS)

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        if difficulty != 0:
            raise ValueError("Fleetbase currently supports difficulty=0 only")
        if self._started:
            raise RuntimeError("Create a fresh task instance for every Fleetbase episode")
        self._started = True
        self._adapter = await self._open_adapter(seed, salt)
        self._sim = self._make_sim(self._make_world(seed, salt))
        await self._adapter.prepare(self._sim.world.vehicles)
        return Observation(
            data={
                "now": self._sim.now,
                "horizon": self._sim.world.horizon,
                "released_orders": len(self._sim.released_ids()),
                "vehicles": len(self._sim.vehicle_ids()),
                "budget": self.spec.max_steps,
            }
        )

    def hidden_strings(self) -> frozenset[str]:
        """Words that only unreleased requests or unrevealed disruptions contain.

        Hidden are: request IDs and coordinates released after the last successful tool
        result, new dropoffs and zone bounds revealed after it, every disruption ID that
        was not announced by then (silent ones are never announced), and private field
        names. Coordinates that are public anyway are removed.
        """
        if self._sim is None:
            return frozenset()
        world = self._sim.world
        seen = self._public_time
        hidden: set[str] = {"u_hindsight", "silent"}
        public: set[str] = {_coords(v.position) for v in world.vehicles}
        for request in world.requests:
            texts = {_coords(request.pickup), _coords(request.dropoff)}
            if request.release > seen:
                hidden.add(request.id)
                hidden |= texts
            else:
                public |= texts
        for disruption in world.disruptions:
            revealed = disruption.time <= seen
            announced = revealed and not disruption.silent
            if not announced:
                hidden.add(disruption.id)
            if disruption.point is not None:
                (public if revealed else hidden).add(_coords(disruption.point))
            if disruption.bounds is not None and not revealed:
                hidden.add(_coords(disruption.bounds))
        return frozenset(hidden - public)

    # Helpers -------------------------------------------------------------------------

    @property
    def _ready(self) -> tuple[WorldSimulator, DispatchAdapter]:
        assert self._sim is not None and self._adapter is not None
        return self._sim, self._adapter

    async def _sync(self) -> dict[str, dict[str, JsonValue]]:
        sim, adapter = self._ready
        committed = await adapter.committed()
        sim.reconcile(committed)
        return committed

    async def _orders(self) -> dict[str, JsonValue]:
        snapshot = await self._ready[1].snapshot()
        orders = snapshot.get("orders", {})
        if not isinstance(orders, dict):
            raise BackendError("Invalid private native snapshot")
        return orders

    def _order_record(
        self,
        request_id: str,
        orders: dict[str, JsonValue],
        committed: dict[str, dict[str, JsonValue]],
    ) -> dict[str, JsonValue]:
        sim = self._ready[0]
        record = sim.request_view(request_id)
        assert record is not None
        order = orders.get(request_id)
        commit = committed.get(request_id, {})
        vehicle_id = commit.get("vehicle_id")
        dispatched = commit.get("dispatched") is True
        status = sim.physical_status(request_id)
        if status is None:
            if not isinstance(order, dict):
                status = "requested"
            elif not isinstance(vehicle_id, str):
                status = "created"
            else:
                status = "dispatched" if dispatched else "assigned"
        meta = order.get("meta") if isinstance(order, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        return {
            **record,
            "status": status,
            "native_id": order.get("id") if isinstance(order, dict) else None,
            "vehicle_id": vehicle_id if isinstance(vehicle_id, str) else None,
            "dispatched": dispatched,
            "note": meta.get("note"),
            "priority": meta.get("priority", "normal"),
        }

    async def _order_result(self, request_id: str) -> ToolResult:
        committed = await self._sync()
        return ToolResult(
            ok=True, content=self._order_record(request_id, await self._orders(), committed)
        )

    def _vehicle_record(
        self, vehicle_id: str, committed: dict[str, dict[str, JsonValue]]
    ) -> dict[str, JsonValue]:
        record = self._ready[0].vehicle_view(vehicle_id)
        assert record is not None
        record["assigned_orders"] = [
            request_id
            for request_id, commit in sorted(committed.items())
            if commit.get("vehicle_id") == vehicle_id
            and commit.get("dispatched") is not True
            and commit.get("cancelled") is not True
        ]
        return record

    def _known_request(self, request_id: str) -> str | None:
        if self._ready[0].request_view(request_id) is None:
            return "Unknown or unreleased request"
        return None

    def _preview_error(
        self, hypothetical: dict[str, dict[str, JsonValue]], request_ids: set[str]
    ) -> str | None:
        sim = self._ready[0]
        for request_id, _, detail in sim.preview(hypothetical):
            if request_id in request_ids:
                return f"Rejected: {detail}"
        return None

    def _stops(self, args: RouteArgs) -> tuple[list[Stop], str | None]:
        stops: list[Stop] = [(s.request_id, s.action) for s in args.stops]
        for request_id, _ in stops:
            error = self._known_request(request_id)
            if error is not None:
                return stops, error
        return stops, None

    @staticmethod
    def _page(items: list[JsonValue], page: int, key: str) -> dict[str, JsonValue]:
        pages = max(1, -(-len(items) // PAGE_SIZE))
        start = (page - 1) * PAGE_SIZE
        return {
            "page": page,
            "pages": pages,
            "total": len(items),
            key: items[start : start + PAGE_SIZE],
        }

    # Steps ---------------------------------------------------------------------------

    async def step(self, call: ToolCall) -> ToolResult:
        if self.done():
            return _fail("Episode is over")
        self._calls += 1
        parsed = validate_call(_TOOL_MAP, call)
        if isinstance(parsed, ToolResult):
            self._rejected_operations += 1
            return parsed
        try:
            result = await self._run(call.name, parsed)
        except FleetbaseOperationError as exc:
            result = _fail(f"Operation rejected: {exc}")
        if result.ok:
            self._public_time = self._ready[0].now
        else:
            self._rejected_operations += 1
        return result

    async def _run(self, name: str, args: BaseModel) -> ToolResult:
        sim, adapter = self._ready
        if isinstance(args, ListOrdersArgs):
            committed = await self._sync()
            orders = await self._orders()
            records: list[JsonValue] = []
            for request_id in sorted(sim.released_ids()):
                record = self._order_record(request_id, orders, committed)
                if args.status is None or record["status"] == args.status:
                    records.append(record)
            return ToolResult(ok=True, content=self._page(records, args.page, "orders"))
        if isinstance(args, UpdateOrderArgs):
            error = self._known_request(args.request_id)
            if error is not None:
                return _fail(error)
            fields: dict[str, JsonValue] = {}
            if args.note is not None:
                fields["note"] = args.note
            if args.priority is not None:
                fields["priority"] = args.priority
            if not fields:
                return _fail("Give a note or a priority")
            await adapter.update_order(args.request_id, fields)
            return await self._order_result(args.request_id)
        if isinstance(args, AssignArgs):
            return await self._assign(args.request_id, args.vehicle_id)
        if isinstance(args, OrderArgs):
            return await self._order_action(name, args.request_id)
        if isinstance(args, RouteArgs):
            return await self._route(name, args)
        if isinstance(args, PageArgs):
            committed = await self._sync()
            vehicles: list[JsonValue] = [
                self._vehicle_record(v, committed) for v in sim.vehicle_ids()
            ]
            return ToolResult(ok=True, content=self._page(vehicles, args.page, "vehicles"))
        if isinstance(args, VehicleArgs):
            if sim.vehicle_view(args.vehicle_id) is None:
                return _fail("Unknown vehicle")
            committed = await self._sync()
            return ToolResult(ok=True, content=self._vehicle_record(args.vehicle_id, committed))
        if isinstance(args, RepositionArgs):
            await self._sync()
            error = sim.reposition(args.vehicle_id, (args.point[0], args.point[1]))
            if error is not None:
                return _fail(error)
            committed = await self._sync()
            return ToolResult(ok=True, content=self._vehicle_record(args.vehicle_id, committed))
        if isinstance(args, EventsArgs):
            return ToolResult(ok=True, content=self._events(args.since))
        if isinstance(args, AdvanceArgs):
            await self._advance(args.minutes)
            return ToolResult(
                ok=True,
                content={
                    "now": sim.now,
                    "latest_event_seq": self._public_event_count(),
                    "calls_remaining": max(0, self.spec.max_steps - self._calls),
                },
            )
        if name == "get_forecast":
            return ToolResult(ok=True, content=self._forecast())
        if name == "finish":
            self._ended = True
            return ToolResult(ok=True, content={"finished": True, "now": sim.now})
        raise AssertionError(name)

    async def _order_action(self, name: str, request_id: str) -> ToolResult:
        sim, adapter = self._ready
        error = self._known_request(request_id)
        if error is not None:
            return _fail(error)
        if name == "get_order":
            return await self._order_result(request_id)
        status = sim.physical_status(request_id)
        if name == "create_order":
            if status == "cancelled" and request_id not in await adapter.committed():
                return _fail("Request was cancelled")
            request = next(r for r in sim.world.requests if r.id == request_id)
            await adapter.create_order(request)
            return await self._order_result(request_id)
        if status in {"picked_up", "delivered", "delivered_late"}:
            return _fail("Physical pickup commitment is irreversible")
        if status == "cancelled":
            return _fail("Order is cancelled")
        if name == "cancel_order":
            await adapter.cancel_order(request_id)
        elif name == "unassign_order":
            await adapter.unassign_order(request_id)
        elif name == "dispatch_order":
            committed = await self._sync()
            if request_id in committed:
                hypothetical = copy.deepcopy(committed)
                hypothetical[request_id]["dispatched"] = True
                error = self._preview_error(hypothetical, {request_id})
                if error is not None:
                    return _fail(error)
            await adapter.dispatch_order(request_id)
        else:
            raise AssertionError(name)
        return await self._order_result(request_id)

    async def _assign(self, request_id: str, vehicle_id: str) -> ToolResult:
        sim, adapter = self._ready
        error = self._known_request(request_id)
        if error is not None:
            return _fail(error)
        vehicle = sim.vehicle_view(vehicle_id)
        if vehicle is None:
            return _fail("Unknown vehicle")
        status = sim.physical_status(request_id)
        if status in {"picked_up", "delivered", "delivered_late"}:
            return _fail("Physical pickup commitment is irreversible")
        if status == "cancelled":
            return _fail("Order is cancelled")
        if vehicle["status"] == "off_shift":
            return _fail("Vehicle shift has ended")
        view = sim.request_view(request_id) or {}
        load, capacity = view.get("load"), vehicle.get("capacity")
        if isinstance(load, int) and isinstance(capacity, int) and load > capacity:
            return _fail("Order load exceeds vehicle capacity")
        committed = await self._sync()
        current = committed.get(request_id)
        if current is not None and current.get("vehicle_id") != vehicle_id:
            top = -1
            for other_id, other in committed.items():
                if other_id != request_id and other.get("vehicle_id") == vehicle_id:
                    for key in ("pickup_rank", "dropoff_rank"):
                        rank = other.get(key)
                        if isinstance(rank, int):
                            top = max(top, rank)
            hypothetical = copy.deepcopy(committed)
            hypothetical[request_id].update(
                {"vehicle_id": vehicle_id, "pickup_rank": top + 1, "dropoff_rank": top + 2}
            )
            error = self._preview_error(hypothetical, {request_id})
            if error is not None:
                return _fail(error)
        await adapter.assign_order(request_id, vehicle_id)
        return await self._order_result(request_id)

    async def _route(self, name: str, args: RouteArgs) -> ToolResult:
        sim, adapter = self._ready
        if sim.vehicle_view(args.vehicle_id) is None:
            return _fail("Unknown vehicle")
        stops, error = self._stops(args)
        if error is not None:
            return _fail(error)
        committed = await self._sync()
        if name == "estimate_route":
            for request_id, action in stops:
                status = sim.physical_status(request_id)
                if status in {"cancelled", "delivered", "delivered_late"}:
                    return _fail(f"Order {request_id} is closed")
                if action == "pickup" and status == "picked_up":
                    return _fail(f"Order {request_id} is already picked up")
            estimate = sim.estimate(args.vehicle_id, stops)
            return ToolResult(ok=True, content=estimate)
        assigned = {
            request_id
            for request_id, commit in committed.items()
            if commit.get("vehicle_id") == args.vehicle_id and commit.get("cancelled") is not True
        }
        error = sim.check_route(args.vehicle_id, stops, assigned)
        if error is not None:
            return _fail(error)
        await adapter.set_route(args.vehicle_id, stops)
        committed = await self._sync()
        return ToolResult(ok=True, content=self._vehicle_record(args.vehicle_id, committed))

    def _public_events(self) -> list[dict[str, JsonValue]]:
        events: list[dict[str, JsonValue]] = []
        for event in self._ready[0].events:
            if not event.public:
                continue
            item: dict[str, JsonValue] = {
                "seq": len(events) + 1,
                "time": round(event.time, 4),
                "kind": event.kind,
            }
            if event.request_id:
                item["request_id"] = event.request_id
            if event.vehicle_id:
                item["vehicle_id"] = event.vehicle_id
            if event.detail:
                item["detail"] = event.detail
            item.update(copy.deepcopy(event.data))
            events.append(item)
        return events

    def _public_event_count(self) -> int:
        return sum(event.public for event in self._ready[0].events)

    def _events(self, since: int) -> dict[str, JsonValue]:
        events = self._public_events()
        selected: list[JsonValue] = list(events[since : since + PAGE_SIZE])
        last = since + len(selected)
        return {
            "events": selected,
            "next_since": last,
            "more": last < len(events),
        }

    def _forecast(self) -> dict[str, JsonValue]:
        sim = self._ready[0]
        world = sim.world
        upcoming = next((f for f in world.forecasts if f.start > sim.now), None)
        next_wave: JsonValue = None
        if upcoming is not None:
            next_wave = {
                "starts_at": upcoming.start,
                "zones": [
                    {"zone": name, "bounds": list(bounds), "expected_requests": expected}
                    for name, bounds, expected in upcoming.zones
                ],
            }
        return {"now": sim.now, "area": [0.0, 0.0, world.area, world.area], "next_wave": next_wave}

    async def _advance(self, minutes: float) -> None:
        sim, adapter = self._ready
        await self._sync()
        events = sim.advance(minutes)
        await adapter.sync_events(events)
        await self._sync()

    def done(self) -> bool:
        return self._ended or (self._sim is not None and self._sim.now >= self._sim.world.horizon)

    async def grade(self) -> Score:
        if self._score is not None:
            return self._score
        sim, adapter = self._ready
        if sim.now < sim.world.horizon:
            await self._advance(sim.world.horizon - sim.now)
        self._ended = True
        economics = sim.economics()
        snapshot = await adapter.snapshot()
        orders = snapshot.get("orders", {})
        if not isinstance(orders, dict):
            raise BackendError("Invalid private native snapshot")
        economics.update(
            {
                "uncreated_orders": float(len(sim.world.requests) - len(orders)),
                "undispatched_orders": float(
                    sum(
                        isinstance(order, dict) and order.get("dispatched") is not True
                        for order in orders.values()
                    )
                ),
                "rejected_operations": float(self._rejected_operations),
                "duplicate_orders": float(str(snapshot.get("duplicate_orders", 0))),
            }
        )
        self._evidence = {
            "events": [event.to_dict() for event in sim.events],
            "economics": dict(economics),
            "native_snapshot": snapshot,
            "world": json.loads(json.dumps(asdict(sim.world))),
            "rejected_operations": self._rejected_operations,
        }
        evidence_dir = os.environ.get("FLEETBASE_ARTIFACT_DIR")
        if evidence_dir:
            target = Path(evidence_dir) / self._episode_id
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / "fleetbase-evidence.json"
            path.write_text(json.dumps(self._evidence, sort_keys=True, allow_nan=False) + "\n")
            path.chmod(0o600)
        self._score = Score(total=economics["reward"], breakdown=economics)
        return self._score

    def grading_evidence(self) -> dict[str, JsonValue]:
        return copy.deepcopy(self._evidence or {})

    async def aclose(self) -> None:
        if self._resources_closed:
            if self._cleanup_failed:
                raise BackendError("Fleetbase resources could not be fully cleaned")
            return
        self._resources_closed = True
        try:
            if self._adapter is not None:
                await self._adapter.cleanup()
        except Exception:
            self._cleanup_failed = True
            raise
        finally:
            await self._close_backend()


def _memory_adapter(seed: int, salt: str | None) -> tuple[str, InMemoryAdapter]:
    # Deterministic per world, so observations and evidence paths are reproducible.
    episode_id = f"memory-{seed}" if salt is None else f"memory-{seed}-salted"
    return episode_id, InMemoryAdapter(episode_id)


@register_task
class FleetbaseDispatch(DispatchTaskBase):
    spec = TaskSpec(
        id="fleetbase_dispatch",
        version=1,
        max_steps=MAX_STEPS,
        title="Fleetbase Dispatch",
        description="Operate native Fleetbase orders against a deterministic delivery world.",
        keywords=("logistics", "fleetbase", "external-backend"),
        cpus=2,
        memory_mb=4096,
        timeout_sec=1800,
    )
    external_backend: ClassVar[bool] = True
    # Generic offline tests cannot start application services. `fleetbase_sim@1` runs
    # the generic checks on the same logic; explicit simulator, adapter, parity and
    # clean-deployment integration tests cover the native backend.
    checks = TaskChecks(
        seeds=(),
        determinism=False,
        do_nothing_zero=False,
        reward_bounds=False,
        no_hidden_state=False,
        reference_solves=False,
        exploits_below_reference=False,
        oracle_perfect=False,
    )

    def __init__(self) -> None:
        super().__init__()
        self._runtime: HttpBackendRuntime | None = None

    async def _open_adapter(self, seed: int, salt: str | None) -> DispatchAdapter:
        backend = os.environ.get(BACKEND_ENV, "native")
        if backend == "memory":
            self._episode_id, adapter = _memory_adapter(seed, salt)
            return adapter
        if backend != "native":
            raise BackendError(f"{BACKEND_ENV} must be 'memory' or 'native'")
        url = os.environ.get("FLEETBASE_URL")
        token = os.environ.get("FLEETBASE_TOKEN")
        deployment = os.environ.get("FLEETBASE_DEPLOYMENT_ID")
        if not url or not token or not deployment:
            raise BackendError("Fleetbase requires private URL, token and deployment configuration")
        if os.environ.get("FLEETBASE_EXCLUSIVE") != "1":
            raise BackendError("Fleetbase requires an exclusively owned test installation")
        connection = BackendConnection(
            deployment_id=deployment,
            base_url=SecretStr(url),
            headers={
                "Authorization": SecretStr(f"Bearer {token}"),
                "Accept": SecretStr("application/json"),
            },
            ownership="managed" if os.environ.get("FLEETBASE_MANAGED") == "1" else "attached",
            exclusive_access=True,
        )
        self._runtime = HttpBackendRuntime(
            connection,
            ExclusiveLeaseProvider(
                Path(os.environ.get("FLEETBASE_LOCK_DIR", "/tmp/crucible-backend-leases"))
            ),
            readiness_path="/v1/orders",
            readiness_attempts=90,
            readiness_interval=1,
        )
        episode = await self._runtime.acquire()
        self._episode_id = episode.resource.episode_id
        return FleetbaseAdapter(episode)

    async def _close_backend(self) -> None:
        if self._runtime is not None:
            await self._runtime.aclose()

    def deployment_bundle(self) -> DeploymentBundle:
        return make_bundle()


@register_task
class FleetbaseSim(DispatchTaskBase):
    """`fleetbase_dispatch@1` on the in-memory backend: same tools, instruction and reward."""

    spec = TaskSpec(
        id="fleetbase_sim",
        version=1,
        max_steps=MAX_STEPS,
        title="Fleetbase Dispatch (simulated backend)",
        description=(
            "Operate dispatch orders against a deterministic delivery world, with an "
            "in-memory order backend that mirrors native Fleetbase."
        ),
        keywords=("logistics", "fleetbase", "simulation"),
    )
    # Online play cannot match the hindsight planner, so there is no oracle at 1.0.
    checks = TaskChecks(exploit_policies=(*EXPLOITS, "stop_early"), oracle_perfect=False)

    async def _open_adapter(self, seed: int, salt: str | None) -> DispatchAdapter:
        self._episode_id, adapter = _memory_adapter(seed, salt)
        return adapter


__all__ = [
    "BACKEND_ENV",
    "MAX_STEPS",
    "ORDER_STATUSES",
    "PAGE_SIZE",
    "DispatchTaskBase",
    "FleetbaseDispatch",
    "FleetbaseSim",
]

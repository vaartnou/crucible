"""Private hindsight denominator: the planner with the whole world known at t=0.

`hindsight_utility(world)` runs the reference's route planner (`policies.solver`) with full
knowledge: every request with its release time, every customer cancellation (a job must
be delivered before it), every dropoff change, every traffic zone and every breakdown
(the truck stands still for the window). Trucks may be sent ahead toward a pickup that is
not released yet, but only when they are empty, as an operator could do with
`reposition_vehicle`.

The plan is then *executed* in a fresh `WorldSimulator` with the real physics: every
minute the released prefix of each planned route is committed with its ranks, and an
idle truck whose next planned stop is an unreleased pickup is repositioned toward it.
`U_hindsight` is the utility that execution actually reaches, so it is achievable and
reproducible. Several planning variants run (one static plan, and rolling replans from
the simulated state every 10, 20 or 40 minutes, see `VARIANTS`); the best executed
utility counts. Each variant is a complete, executed strategy, so the maximum is too.

Results are cached per world, so grading many episodes of one world plans it once.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.policies.solver import (
    PICKUP,
    Job,
    Problem,
    Settings,
    Stop,
    Truck,
    Zone,
    plan,
)
from crucible.envs.fleetbase_dispatch.sim import WorldSimulator
from crucible.envs.fleetbase_dispatch.world import World

STEP_MINUTES = 1.0
_BASE = Settings(regret=1.0, passes=3)
# (replan interval, search settings). None is one static plan made at t=0.
VARIANTS: tuple[tuple[float | None, Settings], ...] = (
    (None, _BASE),
    (10.0, _BASE),
    (40.0, _BASE),
    (20.0, Settings(regret=1.0, passes=3, time_cost=0.2)),
)


@dataclass(frozen=True)
class HindsightResult:
    utility: float
    variant: int
    routes: dict[str, list[Stop]]


def _number(value: JsonValue, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _problem(world: World, sim: WorldSimulator) -> Problem:
    """Everything still open in `sim`, with the future of the world known."""
    now = sim.now
    cancel_at: dict[str, float] = {}
    changes: dict[str, tuple[float, tuple[float, float]]] = {}
    downtime: dict[str, list[tuple[float, float]]] = {}
    zones: list[Zone] = []
    for d in world.disruptions:
        if d.kind == "cancellation" and d.request_id is not None:
            cancel_at[d.request_id] = d.time
        elif d.kind == "dropoff_change" and d.request_id is not None and d.point is not None:
            changes[d.request_id] = (d.time, d.point)
        elif d.kind == "breakdown" and d.vehicle_id is not None:
            if d.time + d.duration > now:
                downtime.setdefault(d.vehicle_id, []).append((d.time, d.time + d.duration))
        elif d.kind == "traffic_zone" and d.bounds is not None:
            zones.append(Zone(d.bounds, d.time, d.time + d.duration, d.factor))
    jobs: dict[str, Job] = {}
    for request in world.requests:
        view = sim.request_view(request.id)
        dropoff = request.dropoff
        if view is not None:
            if view["delivered_at"] is not None or view["cancelled_by"] is not None:
                continue
            raw = view["dropoff"]
            if isinstance(raw, list) and len(raw) == 2:
                dropoff = (_number(raw[0]), _number(raw[1]))
        change = changes.get(request.id)
        new_dropoff, change_time = None, math.inf
        if change is not None and change[0] > now:
            change_time, new_dropoff = change
        jobs[request.id] = Job(
            request.id,
            request.pickup,
            dropoff,
            request.load,
            max(request.release, request.ready_time),
            request.deadline,
            request.value,
            release=request.release,
            due=cancel_at.get(request.id, math.inf),
            new_dropoff=new_dropoff,
            change_time=change_time,
        )
    trucks: list[Truck] = []
    for vehicle in world.vehicles:
        view = sim.vehicle_view(vehicle.id)
        assert view is not None
        if view["status"] == "off_shift":
            continue
        raw_position = view["position"]
        assert isinstance(raw_position, list)
        onboard = view["onboard"]
        assert isinstance(onboard, list)
        repaired = view["repaired_at"]
        start = max(now, _number(repaired, now)) if repaired is not None else now
        trucks.append(
            Truck(
                vehicle.id,
                (_number(raw_position[0]), _number(raw_position[1])),
                start,
                vehicle.capacity,
                vehicle.cost_per_distance,
                vehicle.shift_end,
                tuple(str(j) for j in onboard),
                tuple(sorted(downtime.get(vehicle.id, []))),
            )
        )
    return Problem(
        now=now,
        horizon=world.horizon,
        jobs=jobs,
        trucks=tuple(trucks),
        zones=tuple(zones),
        speed=world.speed,
        late_window=world.late_window,
        early_departure=True,
    )


def _remaining(sim: WorldSimulator, route: list[Stop]) -> list[Stop]:
    kept: list[Stop] = []
    for request_id, action in route:
        view = sim.request_view(request_id)
        if view is not None:
            if view["delivered_at"] is not None or view["cancelled_by"] is not None:
                continue
            if action == PICKUP and view["picked_up_at"] is not None:
                continue
        kept.append((request_id, action))
    return kept


def _commit(
    sim: WorldSimulator, routes: Mapping[str, list[Stop]]
) -> dict[str, dict[str, JsonValue]]:
    """Commit the released prefix of every route; send idle trucks ahead."""
    committed: dict[str, dict[str, JsonValue]] = {}
    ahead: dict[str, tuple[float, float]] = {}
    for vehicle_id, route in routes.items():
        remaining = _remaining(sim, route)
        prefix: list[Stop] = []
        for request_id, action in remaining:
            if action == PICKUP and sim.request_view(request_id) is None:
                if not prefix:
                    request = next(r for r in sim.world.requests if r.id == request_id)
                    ahead[vehicle_id] = request.pickup
                break
            prefix.append((request_id, action))
        for rank, (request_id, action) in enumerate(prefix):
            record = committed.setdefault(
                request_id,
                {
                    "vehicle_id": vehicle_id,
                    "dispatched": True,
                    "cancelled": False,
                    "pickup_rank": None,
                    "dropoff_rank": None,
                },
            )
            record[f"{action}_rank"] = rank
    # A pickup without its dropoff in the prefix keeps its dropoff last.
    for record in committed.values():
        if record["dropoff_rank"] is None:
            record["dropoff_rank"] = 10_000
    sim.reconcile(committed)
    for vehicle_id, point in ahead.items():
        view = sim.vehicle_view(vehicle_id)
        if view is not None and not view["route"] and view["status"] == "idle":
            sim.reposition(vehicle_id, point)
    return committed


def _execute(
    world: World, replan_every: float | None, settings: Settings
) -> tuple[float, dict[str, list[Stop]]]:
    """Executed utility of one variant, and its first plan."""
    sim = WorldSimulator(world)
    routes = plan(_problem(world, sim), None, settings)
    first = {k: list(v) for k, v in routes.items()}
    next_replan = replan_every if replan_every is not None else math.inf
    while sim.now < world.horizon:
        if sim.now >= next_replan - 1e-9:
            current = {k: _remaining(sim, v) for k, v in routes.items()}
            routes = plan(_problem(world, sim), current, settings)
            next_replan += replan_every or math.inf
        _commit(sim, routes)
        sim.advance(STEP_MINUTES)
    return sim.utility(), first


def solve_hindsight(world: World) -> HindsightResult:
    best: HindsightResult | None = None
    for index, (replan_every, settings) in enumerate(VARIANTS):
        utility, routes = _execute(world, replan_every, settings)
        if best is None or utility > best.utility + 1e-9:
            best = HindsightResult(utility, index, routes)
    assert best is not None
    return best


@lru_cache(maxsize=512)
def hindsight_utility(world: World) -> float:
    """U_hindsight of `world` (see the module docstring); cached per world."""
    return solve_hindsight(world).utility


__all__ = ["HindsightResult", "hindsight_utility", "solve_hindsight"]

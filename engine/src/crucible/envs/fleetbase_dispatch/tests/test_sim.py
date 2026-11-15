"""Private physics and trusted grading independent of Fleetbase installation."""

import math
from collections.abc import Mapping

import pytest
from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.sim import WorldSimulator
from crucible.envs.fleetbase_dispatch.world import Request, Vehicle, World, make_world


def small_world(*, deadline: float = 10, load: int = 2) -> World:
    return World(
        requests=(Request("r1", (3, 0), (6, 0), load, 0, deadline, 20),),
        vehicles=(
            Vehicle("v1", "d1", 2, (0, 0)),
            Vehicle("v2", "d2", 4, (10, 0)),
        ),
        horizon=20,
    )


def commitment(vehicle: str = "v1", dispatched: bool = True) -> dict[str, dict[str, JsonValue]]:
    return {"r1": {"vehicle_id": vehicle, "dispatched": dispatched}}


def test_seeded_worlds_and_release_visibility() -> None:
    world = make_world(7, "private")
    assert world == make_world(7, "private")
    assert world != make_world(7, "other")
    assert len(world.requests) == 12
    assert len(world.vehicles) == 3
    sim = WorldSimulator(world)
    first = sim.observation()["requests"]
    assert isinstance(first, list) and len(first) == 6
    assert sim.observation()["next_release"] == 60
    sim.advance(60)
    second = sim.observation()["requests"]
    assert isinstance(second, list) and len(second) == 12
    assert sim.observation()["next_release"] is None


def test_delivery_physics_and_economics() -> None:
    sim = WorldSimulator(small_world())
    sim.reconcile(commitment())
    assert sim.now == 0
    assert sim.economics()["reward"] == 0
    events = sim.advance(6)
    assert [(e.kind, e.time) for e in events] == [("picked_up", 3), ("delivered", 6)]
    assert sim.economics() == {
        "realized_value": 20,
        "driving_cost": pytest.approx(1.2),
        "utility": 18.8,
        "total_generated_value": 20,
        "reward": pytest.approx(0.94),
        "deliveries": 1,
        "late": 0,
        "unserved": 0,
        "distance": 6,
    }


def test_native_completion_cannot_earn_value() -> None:
    sim = WorldSimulator(small_world())
    fake: Mapping[str, Mapping[str, JsonValue]] = {
        "r1": {"vehicle_id": None, "dispatched": False, "status": "completed"}
    }
    sim.reconcile(fake)
    sim.finish()
    assert sim.economics()["reward"] == 0
    assert not any(e.kind == "delivered" for e in sim.events)


def test_capacity_conflicts_and_unreleased_orders() -> None:
    sim = WorldSimulator(small_world(load=3))
    events = sim.reconcile(commitment())
    assert [(e.kind, e.detail) for e in events] == [("rejected", "capacity exceeded")]
    assert sim.reconcile(commitment()) == []
    sim.finish()
    assert sim.economics()["distance"] == 0
    released = WorldSimulator(make_world(0))
    rejected = released.reconcile({"request-07": {"vehicle_id": "vehicle-1", "dispatched": True}})
    assert rejected[0].detail == "demand is not released"


def test_pre_pickup_reassignment_preserves_spent_distance() -> None:
    sim = WorldSimulator(small_world())
    sim.reconcile(commitment())
    sim.advance(2)
    sim.reconcile(commitment("v2"))
    # First vehicle already spent 2 distance units, second must approach from x=10.
    events = sim.advance(10)
    assert [(e.kind, e.time, e.vehicle_id) for e in events] == [
        ("picked_up", 9, "v2"),
        ("late", 12, "v2"),
    ]
    assert sim.economics()["distance"] == 12
    assert sim.economics()["realized_value"] == 0


def test_picked_up_cargo_cannot_be_reassigned_or_deleted() -> None:
    sim = WorldSimulator(small_world())
    sim.reconcile(commitment())
    sim.advance(3)
    assert sim.reconcile(commitment("v2"))[0].detail == "pickup commitment is irreversible"
    assert sim.reconcile({})[0].detail == "pickup commitment is irreversible"
    sim.advance(3)
    delivery = next(e for e in sim.events if e.kind == "delivered")
    assert delivery.vehicle_id == "v1"
    assert sim.economics()["distance"] == 6


def test_duplicates_and_repeated_synchronization_do_not_duplicate_value() -> None:
    sim = WorldSimulator(small_world())
    sim.reconcile(commitment())
    sim.reconcile(commitment())
    sim.advance(6)
    score = sim.economics()
    sim.reconcile(commitment())
    sim.finish()
    sim.finish()
    assert sim.economics() == score
    assert len([e for e in sim.events if e.kind == "delivered"]) == 1
    # The native ID is deliberately absent from the trusted demand key.
    with pytest.raises(ValueError, match="duplicate request"):
        WorldSimulator(World(small_world().requests * 2, small_world().vehicles))


def test_one_physical_commitment_per_vehicle() -> None:
    world = small_world()
    other = Request("r2", (1, 0), (2, 0), 1, 0, 20, 10)
    sim = WorldSimulator(World((*world.requests, other), world.vehicles, horizon=20))
    events = sim.reconcile({**commitment(), "r2": {"vehicle_id": "v1", "dispatched": True}})
    assert events[0].detail == "vehicle already carries a commitment"
    sim.finish()
    assert sim.economics()["deliveries"] == 1
    assert sim.economics()["total_generated_value"] == 30


def test_split_advances_match_single_advance_and_fresh_replay() -> None:
    a, b = (WorldSimulator(small_world()) for _ in range(2))
    for sim in (a, b):
        sim.reconcile(commitment())
    a.advance(1)
    a.advance(2)
    a.advance(3)
    b.advance(6)
    assert a.events == b.events
    assert a.economics() == b.economics()
    assert a.observation() == b.observation()
    fresh = WorldSimulator(small_world())
    assert fresh.economics()["reward"] == 0
    assert fresh.now == 0


def test_terminal_delivery_feasibility_and_finish_horizon() -> None:
    world = World((Request("r1", (1, 0), (100, 0), 1, 0, 10, 20),), small_world().vehicles, 20)
    sim = WorldSimulator(world)
    sim.reconcile(commitment())
    sim.finish()
    assert sim.now == 20
    assert sim.economics()["distance"] == 20
    assert sim.economics()["deliveries"] == 0
    assert sim.economics()["reward"] == 0
    for invalid in (-1, math.inf, math.nan):
        with pytest.raises(ValueError, match="finite and nonnegative"):
            sim.advance(invalid)

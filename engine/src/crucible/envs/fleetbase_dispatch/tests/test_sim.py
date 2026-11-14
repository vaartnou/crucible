"""Private physics and trusted economics, independent of any order backend."""

import math

import pytest
from pydantic import JsonValue

from crucible.envs.fleetbase_dispatch.sim import WorldSimulator, late_factor, travel, zones_of
from crucible.envs.fleetbase_dispatch.world import Disruption, Request, Vehicle, World, make_world


def vehicle(capacity: int = 4, *, cost: float = 0.2, shift_end: float = 1000) -> Vehicle:
    return Vehicle("v1", "d1", capacity, (0.0, 0.0), cost, shift_end)


def request(
    request_id: str = "r1",
    pickup: tuple[float, float] = (3, 0),
    dropoff: tuple[float, float] = (6, 0),
    *,
    load: int = 1,
    release: float = 0,
    deadline: float = 50,
    value: float = 20,
    ready: float = 0,
) -> Request:
    return Request(request_id, pickup, dropoff, load, release, deadline, value, ready)


def world(
    *requests: Request,
    vehicles: tuple[Vehicle, ...] | None = None,
    disruptions: tuple[Disruption, ...] = (),
    horizon: float = 100,
) -> World:
    return World(
        requests=requests or (request(),),
        vehicles=vehicles or (vehicle(),),
        disruptions=disruptions,
        horizon=horizon,
    )


def commit(
    vehicle_id: str | None = "v1",
    *,
    dispatched: bool = True,
    cancelled: bool = False,
    ranks: tuple[int, int] | None = None,
) -> dict[str, JsonValue]:
    return {
        "vehicle_id": vehicle_id,
        "dispatched": dispatched,
        "cancelled": cancelled,
        "pickup_rank": ranks[0] if ranks else None,
        "dropoff_rank": ranks[1] if ranks else None,
    }


def kinds(sim: WorldSimulator) -> list[tuple[str, float, str]]:
    return [
        (e.kind, round(e.time, 6), e.request_id or (e.vehicle_id or ""))
        for e in sim.events
        if e.kind != "released"
    ]


def test_delivery_physics_and_economics() -> None:
    sim = WorldSimulator(world())
    sim.reconcile({"r1": commit()})
    assert sim.economics()["reward"] == 0
    events = sim.advance(6)
    assert [(e.kind, e.time) for e in events] == [("picked_up", 3), ("delivered", 6)]
    economics = sim.economics()
    assert economics["on_time_value"] == 20
    assert economics["driving_cost"] == pytest.approx(1.2)
    assert economics["utility"] == pytest.approx(18.8)
    # The hindsight planner does exactly this, so the reward is 1.
    assert economics["u_hindsight"] == pytest.approx(18.8)
    assert economics["reward"] == pytest.approx(1.0)
    assert economics["deliveries"] == 1 and economics["unserved"] == 0


def test_driving_cost_is_per_vehicle() -> None:
    cheap = Vehicle("v1", "d1", 4, (0, 0), 0.1)
    dear = Vehicle("v2", "d2", 4, (0, 0), 1.0)
    sim = WorldSimulator(world(request(), request("r2"), vehicles=(cheap, dear)))
    sim.reconcile({"r1": commit("v1"), "r2": commit("v2")})
    sim.finish()
    assert sim.economics()["driving_cost"] == pytest.approx(6 * 0.1 + 6 * 1.0)


def test_capacity_is_enforced_at_every_stop() -> None:
    first = request("r1", (1, 0), (5, 0), load=2)
    second = request("r2", (2, 0), (3, 0), load=2)
    pooled = {"r1": commit(ranks=(0, 3)), "r2": commit(ranks=(1, 2))}
    # Capacity 3: carrying both after the second pickup would hold 4.
    sim = WorldSimulator(world(first, second, vehicles=(vehicle(3),)))
    assert sim.preview(pooled) == [("r2", "v1", "capacity exceeded at a stop")]
    assert sim.economics()["distance"] == 0  # preview changes nothing
    events = sim.reconcile(pooled)
    assert [(e.kind, e.request_id, e.detail) for e in events] == [
        ("rejected", "r2", "capacity exceeded at a stop")
    ]
    sim.finish()
    assert sim.economics()["deliveries"] == 1
    # Capacity 4 serves the same pooled route.
    sim = WorldSimulator(world(first, second, vehicles=(vehicle(4),)))
    assert sim.reconcile(pooled) == []
    sim.finish()
    assert kinds(sim) == [
        ("picked_up", 1, "r1"),
        ("picked_up", 2, "r2"),
        ("delivered", 3, "r2"),
        ("delivered", 5, "r1"),
    ]
    # Sequential service never holds more than one load.
    sim = WorldSimulator(world(first, second, vehicles=(vehicle(3),)))
    assert sim.reconcile({"r1": commit(ranks=(0, 1)), "r2": commit(ranks=(2, 3))}) == []
    sim.finish()
    assert sim.economics()["deliveries"] == 2


def test_route_order_and_unranked_stops() -> None:
    first = request("r1", (1, 0), (5, 0))
    second = request("r2", (2, 0), (3, 0))
    sim = WorldSimulator(world(first, second))
    rejected = sim.reconcile({"r1": commit(ranks=(3, 1)), "r2": commit()})
    assert [(e.request_id, e.detail) for e in rejected] == [("r1", "dropoff before pickup")]
    sim.finish()
    # The unranked order is served as pickup then dropoff.
    assert kinds(sim)[-2:] == [("picked_up", 2, "r2"), ("delivered", 3, "r2")]


def test_ready_time_makes_the_vehicle_wait() -> None:
    sim = WorldSimulator(world(request(ready=10)))
    sim.reconcile({"r1": commit()})
    sim.advance(5)
    view = sim.vehicle_view("v1")
    assert view is not None and view["position"] == [3, 0]
    sim.finish()
    assert kinds(sim) == [("picked_up", 10, "r1"), ("delivered", 13, "r1")]
    assert sim.economics()["distance"] == 6


def test_breakdown_unassigns_orders_not_picked_up_and_cargo_waits() -> None:
    on_board = request("r2", (1, 0), (4, 0))
    waiting = request("r1", (6, 0), (8, 0))
    breakdown = Disruption("disruption-01", "breakdown", 2, vehicle_id="v1", duration=5)
    sim = WorldSimulator(world(on_board, waiting, disruptions=(breakdown,)))
    sim.reconcile({"r2": commit(ranks=(0, 1)), "r1": commit(ranks=(2, 3))})
    events = sim.advance(3)
    assert [(e.kind, e.request_id or e.vehicle_id) for e in events] == [
        ("picked_up", "r2"),
        ("breakdown", "v1"),
        ("unassigned", "r1"),
    ]
    assert events[1].data == {"until": 7, "disruption_id": "disruption-01"}
    view = sim.vehicle_view("v1")
    assert view is not None
    assert view["status"] == "broken_down" and view["repaired_at"] == 7
    assert view["onboard"] == ["r2"] and view["route"] == [
        {"request_id": "r2", "action": "dropoff"}
    ]
    sim.advance(3)
    view = sim.vehicle_view("v1")
    assert view is not None and view["position"] == [2, 0]
    # The backend now shows r1 unassigned; the vehicle resumes with its cargo.
    sim.reconcile({"r2": commit(ranks=(0, 1)), "r1": commit(None, dispatched=False)})
    sim.finish()
    assert ("repaired", 7, "v1") in kinds(sim)
    assert ("delivered", 9, "r2") in kinds(sim)
    assert sim.economics()["deliveries"] == 1 and sim.economics()["unserved"] == 1


def test_shift_end_stops_the_vehicle_for_good() -> None:
    sim = WorldSimulator(world(request(dropoff=(10, 0)), vehicles=(vehicle(shift_end=4),)))
    sim.reconcile({"r1": commit()})
    sim.finish()
    assert kinds(sim) == [("picked_up", 3, "r1"), ("shift_ended", 4, "v1")]
    view = sim.vehicle_view("v1")
    assert view is not None and view["status"] == "off_shift" and view["position"] == [4, 0]
    assert sim.economics()["distance"] == 4 and sim.economics()["deliveries"] == 0


def test_traffic_zone_halves_speed_inside_its_rectangle_and_window() -> None:
    zone = Disruption(
        "disruption-01", "traffic_zone", 3, duration=100, bounds=(2, -1, 4, 1), factor=0.5
    )
    sim = WorldSimulator(world(request("r1", (6, 0), (7, 0)), disruptions=(zone,)))
    sim.reconcile({"r1": commit()})
    sim.finish()
    # 0→3 at full speed (zone not active yet), 3→4 at half speed, 4→6 at full speed.
    assert kinds(sim)[1:] == [("picked_up", 7, "r1"), ("delivered", 8, "r1")]
    assert sim.economics()["distance"] == pytest.approx(7)
    always = zones_of(
        (Disruption("z", "traffic_zone", 0, duration=100, bounds=(2, -1, 4, 1), factor=0.5),)
    )
    assert travel((0, 0), (6, 0), 0, math.inf, 1.0, always) == ((6, 0), 8, 6)
    position, time, moved = travel((0, 0), (6, 0), 0, 4, 1.0, always)
    assert position == pytest.approx((3, 0)) and time == 4 and moved == pytest.approx(3)


def test_estimate_uses_only_announced_zones() -> None:
    zone = Disruption(
        "disruption-01", "traffic_zone", 2, duration=100, bounds=(2, -1, 4, 1), factor=0.5
    )
    sim = WorldSimulator(world(request("r1", (6, 0), (7, 0)), disruptions=(zone,)))
    stops = [("r1", "pickup"), ("r1", "dropoff")]
    before = sim.estimate("v1", stops)
    assert before is not None and before["end_time"] == 7
    sim.advance(2)
    after = sim.estimate("v1", stops)
    assert after is not None and after["end_time"] == 2 + 9
    assert after["feasible"] is True and after["distance"] == 7


def test_customer_cancellation_before_pickup_is_silent_and_free() -> None:
    cancel = Disruption("disruption-01", "cancellation", 1, request_id="r1", silent=True)
    sim = WorldSimulator(world(disruptions=(cancel,)))
    sim.reconcile({"r1": commit()})
    events = sim.advance(10)
    assert [(e.kind, e.public) for e in events] == [("customer_cancelled", False)]
    assert sim.physical_status("r1") == "cancelled"
    view = sim.request_view("r1")
    assert view is not None and view["cancelled_by"] == "customer"
    economics = sim.economics()
    assert economics["distance"] == 1 and economics["cancellation_fees"] == 0
    assert economics["customer_cancelled"] == 1 and economics["u_hindsight"] == 0
    assert economics["reward"] == 0


def test_customer_cancellation_on_board_unloads_without_value() -> None:
    cancel = Disruption("disruption-01", "cancellation", 4, request_id="r1", silent=True)
    other = request("r2", (5, 0), (6, 0), load=2)
    sim = WorldSimulator(
        world(request(load=2), other, vehicles=(vehicle(2),), disruptions=(cancel,))
    )
    sim.reconcile({"r1": commit(ranks=(0, 3)), "r2": commit(ranks=(1, 2))})
    # Holding r1 (load 2) blocks r2 at capacity 2.
    sim.reconcile({"r1": commit(ranks=(0, 1)), "r2": commit(ranks=(2, 3))})
    sim.advance(4)
    view = sim.vehicle_view("v1")
    assert view is not None and view["onboard"] == [] and view["load"] == 0
    assert view["route"] == [
        {"request_id": "r2", "action": "pickup"},
        {"request_id": "r2", "action": "dropoff"},
    ]
    sim.finish()
    assert kinds(sim)[-2:] == [("picked_up", 5, "r2"), ("delivered", 6, "r2")]
    assert sim.economics()["on_time_value"] == 20 and sim.economics()["deliveries"] == 1


def test_dropoff_change_applies_to_the_route() -> None:
    change = Disruption(
        "disruption-01", "dropoff_change", 1, request_id="r1", point=(3, 4), silent=True
    )
    sim = WorldSimulator(world(disruptions=(change,)))
    sim.reconcile({"r1": commit()})
    events = sim.advance(2)
    assert [(e.kind, e.public, e.data["dropoff"]) for e in events] == [
        ("dropoff_changed", False, [3, 4])
    ]
    view = sim.request_view("r1")
    assert view is not None and view["dropoff"] == [3, 4]
    sim.finish()
    assert kinds(sim)[-1] == ("delivered", 7, "r1")


def test_late_value_decays_linearly_over_the_late_window() -> None:
    assert late_factor(10, 10, 30) == 1
    assert late_factor(25, 10, 30) == pytest.approx(0.5)
    assert late_factor(40, 10, 30) == 0 and late_factor(90, 10, 30) == 0
    sim = WorldSimulator(world(request(deadline=3, ready=15)))
    sim.reconcile({"r1": commit()})
    sim.finish()
    economics = sim.economics()
    assert kinds(sim)[-1] == ("delivered_late", 18, "r1")
    assert economics["on_time_value"] == 0 and economics["late"] == 1
    assert economics["late_value"] == pytest.approx(10)


def test_agent_cancellation_fee_only_after_dispatch_and_only_once() -> None:
    requests = (request("r1"), request("r2"), request("r3"))
    sim = WorldSimulator(world(*requests))
    sim.reconcile({"r1": commit(), "r2": commit(dispatched=False), "r3": commit()})
    # r3 is undispatched before its cancellation: it was still dispatched once.
    sim.reconcile(
        {"r1": commit(), "r2": commit(dispatched=False), "r3": commit(None, dispatched=False)}
    )
    cancelled = {
        "r1": commit(None, cancelled=True),
        "r2": commit(None, dispatched=False, cancelled=True),
        "r3": commit(None, dispatched=False, cancelled=True),
    }
    sim.reconcile(cancelled)
    sim.reconcile(cancelled)
    sim.finish()
    economics = sim.economics()
    assert economics["cancellation_fees"] == 2 * 8 and economics["fees_charged"] == 2
    assert economics["agent_cancelled"] == 3 and economics["utility"] == -16
    assert economics["reward"] == 0


def test_native_completion_cannot_earn_value() -> None:
    sim = WorldSimulator(world())
    sim.reconcile({"r1": {"vehicle_id": None, "dispatched": False, "status": "completed"}})
    sim.finish()
    assert sim.economics()["reward"] == 0
    assert not any(e.kind == "delivered" for e in sim.events)


def test_rejections_unreleased_unknown_and_irreversible_pickup() -> None:
    later = request("r2", release=50)
    other = Vehicle("v2", "d2", 4, (10, 0))
    sim = WorldSimulator(world(request(), later, vehicles=(vehicle(), other)))
    events = sim.reconcile({"r2": commit(), "rx": commit(), "r1": commit("v9")})
    assert sorted(e.detail for e in events) == [
        "demand is not released",
        "unknown demand",
        "unknown vehicle",
    ]
    assert sim.reconcile({"r2": commit(), "rx": commit(), "r1": commit("v9")}) == []
    sim.reconcile({"r1": commit()})
    sim.advance(3)
    assert sim.reconcile({"r1": commit("v2")})[0].detail == "pickup commitment is irreversible"
    assert sim.reconcile({})[0].detail == "pickup commitment is irreversible"
    sim.finish()
    delivery = next(e for e in sim.events if e.kind == "delivered")
    assert delivery.vehicle_id == "v1"


def test_reassignment_before_pickup_keeps_spent_distance() -> None:
    other = Vehicle("v2", "d2", 4, (10, 0))
    sim = WorldSimulator(world(request(deadline=10), vehicles=(vehicle(), other)))
    sim.reconcile({"r1": commit()})
    sim.advance(2)
    sim.reconcile({"r1": commit("v2")})
    sim.advance(20)
    assert kinds(sim) == [("picked_up", 9, "r1"), ("delivered_late", 12, "r1")]
    assert sim.economics()["distance"] == 12


def test_reposition_moves_an_idle_vehicle_only() -> None:
    sim = WorldSimulator(world())
    assert sim.reposition("v1", (0, 200)) is not None
    assert sim.reposition("v9", (1, 1)) == "Unknown vehicle"
    assert sim.reposition("v1", (0, 5)) is None
    sim.advance(10)
    view = sim.vehicle_view("v1")
    assert view is not None and view["position"] == [0, 5] and view["status"] == "idle"
    assert sim.economics()["driving_cost"] == pytest.approx(1.0)
    sim.reconcile({"r1": commit()})
    assert sim.reposition("v1", (1, 1)) is not None


def test_split_advances_match_a_single_advance() -> None:
    disruptions = (
        Disruption(
            "disruption-01", "traffic_zone", 1, duration=4, bounds=(2, -1, 4, 1), factor=0.5
        ),
        Disruption("disruption-02", "breakdown", 5, vehicle_id="v1", duration=2),
    )
    second = request("r2", (8, 0), (9, 0), ready=6)
    sims = [WorldSimulator(world(request(), second, disruptions=disruptions)) for _ in range(2)]
    for sim in sims:
        sim.reconcile({"r1": commit(ranks=(0, 1)), "r2": commit(ranks=(2, 3))})
    for minutes in (0.5, 1.7, 2.3, 3.1, 4.4, 8):
        sims[0].advance(minutes)
    sims[1].advance(20)
    assert sims[0].events == sims[1].events
    assert sims[0].economics() == sims[1].economics()
    assert sims[0].vehicle_view("v1") == sims[1].vehicle_view("v1")


def test_invalid_minutes_and_duplicate_ids_are_rejected() -> None:
    sim = WorldSimulator(world())
    for invalid in (-1, math.inf, math.nan):
        with pytest.raises(ValueError, match="finite and nonnegative"):
            sim.advance(invalid)
    with pytest.raises(ValueError, match="duplicate request"):
        WorldSimulator(world(request(), request()))


@pytest.mark.parametrize("seed", range(5))
def test_generated_worlds_hindsight_beats_a_naive_plan(seed: int) -> None:
    sim = WorldSimulator(make_world(seed))
    released = sim.released_ids()
    assert released and all(rid.startswith("request-") for rid in released)
    sim.reconcile({rid: commit(f"vehicle-{i % 6 + 1}") for i, rid in enumerate(released)})
    sim.finish()
    economics = sim.economics()
    assert economics["u_hindsight"] > max(0.0, economics["utility"])
    assert 0 <= economics["reward"] < 1

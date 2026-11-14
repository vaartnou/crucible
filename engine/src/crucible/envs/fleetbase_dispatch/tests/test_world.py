"""World v2: deterministic generation, salt sensitivity and config overrides."""

import math
from dataclasses import replace

import pytest

from crucible.envs.fleetbase_dispatch.world import (
    DISRUPTION_KINDS,
    WorldConfig,
    make_world,
)


def test_same_seed_and_salt_give_the_same_world() -> None:
    assert make_world(3) == make_world(3)
    assert make_world(3, "secret") == make_world(3, "secret")


def test_seed_and_salt_change_the_world() -> None:
    assert make_world(3) != make_world(4)
    assert make_world(3) != make_world(3, "secret")
    assert make_world(3, "secret") != make_world(3, "other")


@pytest.mark.parametrize("seed", range(10))
def test_default_world_matches_the_plan(seed: int) -> None:
    config = WorldConfig()
    world = make_world(seed)
    assert len(world.vehicles) == 6
    capacities = sorted(v.capacity for v in world.vehicles)
    assert capacities[0] == config.capacity_min and capacities[-1] == config.capacity_max
    assert len({v.cost_per_distance for v in world.vehicles}) >= 4
    assert all(
        config.cost_per_distance_min <= v.cost_per_distance <= config.cost_per_distance_max
        for v in world.vehicles
    )
    assert max(v.shift_end for v in world.vehicles) == world.horizon == 240
    assert all(v.shift_end >= config.shift_end_min for v in world.vehicles)
    assert 40 <= len(world.requests) <= 50
    assert [r.id for r in world.requests] == sorted(r.id for r in world.requests)
    assert [r.release for r in world.requests] == sorted(r.release for r in world.requests)
    waves = {r.release for r in world.requests if not r.urgent}
    assert waves <= {0.0, 40.0, 80.0, 120.0, 160.0, 200.0}
    urgent = [r for r in world.requests if r.urgent]
    assert len(urgent) == config.urgent_requests
    assert all(r.release not in waves for r in urgent)
    normal_values = [r.value for r in world.requests if not r.urgent]
    assert max(normal_values) / min(normal_values) <= config.value_spread + 0.1
    assert all(r.ready_time >= r.release for r in world.requests)
    for request in world.requests:
        direct = math.dist(request.pickup, request.dropoff)
        assert request.deadline >= request.ready_time + direct - 0.1
        assert request.load <= max(capacities)
    for point in [p for r in world.requests for p in (r.pickup, r.dropoff)]:
        assert 0 <= point[0] <= world.area and 0 <= point[1] <= world.area
    assert len(world.forecasts) == 6
    assert all(len(f.zones) == 9 for f in world.forecasts)
    assert world.late_window == 30 and world.cancellation_fee == config.cancellation_fee


def test_disruption_schedule_is_seeded_and_has_every_kind() -> None:
    kinds: set[str] = set()
    for seed in range(20):
        world = make_world(seed)
        times = [d.time for d in world.disruptions]
        assert times == sorted(times)
        assert len({d.id for d in world.disruptions}) == len(world.disruptions)
        for disruption in world.disruptions:
            kinds.add(disruption.kind)
            if disruption.kind == "traffic_zone":
                assert disruption.factor == 0.5 and disruption.bounds is not None
                assert not disruption.silent
            if disruption.kind == "breakdown":
                assert disruption.vehicle_id is not None and disruption.duration >= 20
                assert not disruption.silent
            if disruption.kind in {"cancellation", "dropoff_change"}:
                # Default: customer changes are silent.
                assert disruption.silent and disruption.request_id is not None
    assert kinds == set(DISRUPTION_KINDS)


def test_config_overrides_change_the_world() -> None:
    base = WorldConfig()
    calm = replace(
        base,
        breakdown_rate=0.0,
        cancellation_rate=0.0,
        dropoff_change_rate=0.0,
        traffic_rate=0.0,
    )
    assert make_world(1, config=calm).disruptions == ()
    small = replace(base, vehicles=3, requests_min=10, requests_max=10, urgent_requests=1)
    world = make_world(1, config=small)
    assert len(world.vehicles) == 3 and len(world.requests) == 10
    loud = replace(base, silent_share=0.0, cancellation_rate=0.5)
    assert not any(d.silent for d in make_world(1, config=loud).disruptions)
    tight = make_world(1, config=replace(base, demand_capacity_ratio=2.8))
    assert tight.area > make_world(1).area
    assert make_world(1, config=replace(base, late_window=10)).late_window == 10


@pytest.mark.parametrize(
    "change",
    [
        {"capacity_min": 9},
        {"breakdown_rate": 1.5},
        {"traffic_factor": 0.0},
        {"urgent_requests": 40},
        {"late_window": 0.0},
    ],
)
def test_invalid_config_is_rejected(change: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="invalid WorldConfig"):
        replace(WorldConfig(), **change)

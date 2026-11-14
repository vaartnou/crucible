"""The private hindsight denominator and the shared route solver."""

import pytest

from crucible.envs.fleetbase_dispatch.hindsight import hindsight_utility, solve_hindsight
from crucible.envs.fleetbase_dispatch.policies.solver import Job, Problem, Truck, plan
from crucible.envs.fleetbase_dispatch.world import (
    Disruption,
    Request,
    Vehicle,
    World,
    WorldConfig,
    make_world,
)


def one_vehicle_world(*requests: Request, disruptions: tuple[Disruption, ...] = ()) -> World:
    return World(
        requests=requests,
        vehicles=(Vehicle("v1", "d1", 2, (0.0, 0.0), 0.2, 100),),
        disruptions=disruptions,
        horizon=100,
        area=20,
    )


def test_hindsight_of_a_single_request_is_its_direct_service() -> None:
    world = one_vehicle_world(Request("r1", (3, 0), (6, 0), 1, 0, 50, 20))
    assert hindsight_utility(world) == pytest.approx(20 - 6 * 0.2)


def test_hindsight_skips_a_request_the_customer_cancels_first() -> None:
    cancel = Disruption("d1", "cancellation", 4, request_id="r1", silent=True)
    world = one_vehicle_world(Request("r1", (3, 0), (6, 0), 1, 0, 50, 20), disruptions=(cancel,))
    assert hindsight_utility(world) == 0


def test_hindsight_drives_ahead_to_a_request_released_later() -> None:
    # Released at 20 with deadline 31: only a truck sent ahead before the release is on
    # time (pickup at 20, delivery at 30).
    world = one_vehicle_world(Request("r1", (10, 0), (20, 0), 1, 20, 31, 50, 20))
    result = solve_hindsight(world)
    assert result.utility == pytest.approx(50 - 20 * 0.2)


def test_hindsight_is_reproducible_and_cached() -> None:
    world = make_world(11, None, WorldConfig())
    first = solve_hindsight(world)
    assert solve_hindsight(world).utility == first.utility > 0
    hindsight_utility.cache_clear()
    assert hindsight_utility(world) == first.utility
    hits = hindsight_utility.cache_info().hits
    assert hindsight_utility(make_world(11, None, WorldConfig())) == first.utility
    assert hindsight_utility.cache_info().hits == hits + 1


def test_plan_respects_capacity_and_pairs_stops() -> None:
    jobs = {
        f"j{i}": Job(f"j{i}", (float(i), 0.0), (float(i), 5.0), 2, 0.0, 100.0, 30.0)
        for i in range(4)
    }
    problem = Problem(
        now=0.0,
        horizon=200.0,
        jobs=jobs,
        trucks=(Truck("t", (0.0, 0.0), 0.0, 3, 0.1, 200.0),),
    )
    route = plan(problem)["t"]
    load = 0
    seen: set[str] = set()
    for job_id, action in route:
        if action == "pickup":
            seen.add(job_id)
            load += 2
        else:
            assert job_id in seen
            load -= 2
        assert load <= 3
    assert {job_id for job_id, _ in route} == set(jobs)

"""Seeded development worlds for the native Fleetbase workflow."""

from dataclasses import dataclass

from crucible.core.rng import RngStreams

Point = tuple[float, float]


@dataclass(frozen=True)
class Request:
    id: str
    pickup: Point
    dropoff: Point
    load: int
    release: float
    deadline: float
    value: float


@dataclass(frozen=True)
class Vehicle:
    id: str
    driver_id: str
    capacity: int
    position: Point


@dataclass(frozen=True)
class World:
    requests: tuple[Request, ...]
    vehicles: tuple[Vehicle, ...]
    horizon: float = 180.0
    speed: float = 1.0
    cost_per_distance: float = 0.2


def make_world(seed: int, salt: str | None = None) -> World:
    """Twelve heterogeneous demands in two waves, with three matching drivers.

    All coordinates represent abstract distance units. Each vehicle serves one
    order at a time; later orders require a new public dispatch decision.
    """
    rng = RngStreams(seed, salt).exogenous
    vehicles = tuple(
        Vehicle(f"vehicle-{i + 1}", f"driver-{i + 1}", capacity, (float(i * 6), 0.0))
        for i, capacity in enumerate((2, 4, 6))
    )
    requests: list[Request] = []
    for i in range(12):
        release = 0.0 if i < 6 else 60.0
        pickup = (float(rng.randint(0, 18)), float(rng.randint(0, 12)))
        dropoff = (float(rng.randint(0, 24)), float(rng.randint(0, 18)))
        if pickup == dropoff:
            dropoff = (dropoff[0] + 1.0, dropoff[1])
        requests.append(
            Request(
                id=f"request-{i + 1:02d}",
                pickup=pickup,
                dropoff=dropoff,
                load=(1, 2, 3, 4, 5, 2)[i % 6],
                release=release,
                deadline=release + (35, 48, 65, 80, 100, 115)[i % 6],
                value=float((35, 55, 75, 90, 110, 65)[i % 6] + rng.randint(-5, 5)),
            )
        )
    return World(tuple(requests), vehicles)

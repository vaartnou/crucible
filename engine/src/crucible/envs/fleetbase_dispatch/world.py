"""Seeded dispatch worlds: fleet, demand waves, forecast and a disruption schedule.

Every difficulty setting lives in one flat, frozen `WorldConfig`. Override settings with
`dataclasses.replace(WorldConfig(), ...)`. `make_world(seed, salt, config)` is deterministic:
fleet, demand and disruptions each draw from their own `RngStreams` stream.

Coordinates are abstract distance units in the square `[0, area]²`. All generated
coordinates and times are rounded, so that public records print them exactly.

Demand/capacity ratio: the side of the service area is chosen so that

    Σ_requests (direct trip + ½ · approach) / speed  =  ratio · Σ_vehicles shift minutes,

where "approach" is the distance from the previous request's dropoff to this request's
pickup, in generation order. A higher ratio means a larger area for the same fleet, so
more requests than the fleet can serve.
"""

import math
import random
from dataclasses import dataclass

from crucible.core.rng import RngStreams

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]

DISRUPTION_KINDS = ("breakdown", "cancellation", "dropoff_change", "traffic_zone")


@dataclass(frozen=True)
class WorldConfig:
    """Every difficulty setting, with the default `fleetbase_dispatch@1` values."""

    # Fleet
    vehicles: int = 6
    capacity_min: int = 2
    capacity_max: int = 8
    cost_per_distance_min: float = 0.15
    cost_per_distance_max: float = 0.45
    shift_end_min: float = 150.0
    # Demand
    requests_min: int = 40
    requests_max: int = 50
    waves: int = 6
    horizon: float = 240.0
    speed: float = 1.0
    demand_capacity_ratio: float = 1.4
    value_min: float = 10.0
    value_spread: float = 8.0
    urgent_requests: int = 3
    urgent_multiplier: float = 3.0
    urgent_slack: float = 20.0
    ready_delay_max: float = 25.0
    deadline_slack_min: float = 25.0
    deadline_slack_max: float = 80.0
    late_window: float = 30.0
    cancellation_fee: float = 8.0
    forecast_grid: int = 3
    # Disruptions
    breakdown_rate: float = 0.35
    breakdown_duration_min: float = 20.0
    breakdown_duration_max: float = 45.0
    cancellation_rate: float = 0.08
    dropoff_change_rate: float = 0.08
    traffic_rate: float = 0.4
    traffic_factor: float = 0.5
    traffic_duration: float = 45.0
    silent_share: float = 1.0

    def __post_init__(self) -> None:
        checks = (
            self.vehicles >= 1,
            1 <= self.capacity_min <= self.capacity_max,
            0 <= self.cost_per_distance_min <= self.cost_per_distance_max,
            0 < self.shift_end_min <= self.horizon,
            0 <= self.urgent_requests < self.requests_min <= self.requests_max,
            self.waves >= 1,
            self.horizon > 0 and self.speed > 0,
            self.demand_capacity_ratio > 0,
            self.value_min > 0 and self.value_spread >= 1 and self.urgent_multiplier >= 1,
            0 <= self.deadline_slack_min <= self.deadline_slack_max,
            self.ready_delay_max >= 0 and self.urgent_slack >= 0,
            self.late_window > 0 and self.cancellation_fee >= 0,
            self.forecast_grid >= 1,
            0 <= self.breakdown_duration_min <= self.breakdown_duration_max,
            0 < self.traffic_factor <= 1 and self.traffic_duration > 0,
        )
        rates = (
            self.breakdown_rate,
            self.cancellation_rate,
            self.dropoff_change_rate,
            self.traffic_rate,
            self.silent_share,
        )
        if not all(checks) or not all(0 <= rate <= 1 for rate in rates):
            raise ValueError("invalid WorldConfig")


@dataclass(frozen=True)
class Request:
    """A customer request. Pickup is possible from `max(release, ready_time)`."""

    id: str
    pickup: Point
    dropoff: Point
    load: int
    release: float
    deadline: float
    value: float
    ready_time: float = 0.0
    urgent: bool = False


@dataclass(frozen=True)
class Vehicle:
    id: str
    driver_id: str
    capacity: int
    position: Point
    cost_per_distance: float = 0.2
    shift_end: float = 10_000.0


@dataclass(frozen=True)
class Disruption:
    """One scheduled disruption. Which fields matter depends on `kind`.

    - ``breakdown``: `vehicle_id` is offline from `time` for `duration` minutes.
    - ``cancellation``: the customer cancels `request_id` at `time`.
    - ``dropoff_change``: `request_id` gets the dropoff `point` at `time`.
    - ``traffic_zone``: speed is multiplied by `factor` inside `bounds` from `time` for
      `duration` minutes.

    `silent` cancellations and dropoff changes change only the order record.
    """

    id: str
    kind: str
    time: float
    vehicle_id: str | None = None
    request_id: str | None = None
    duration: float = 0.0
    point: Point | None = None
    bounds: Bounds | None = None
    factor: float = 1.0
    silent: bool = False


@dataclass(frozen=True)
class WaveForecast:
    """Public expected demand of one wave: (zone name, bounds, expected requests)."""

    start: float
    zones: tuple[tuple[str, Bounds, float], ...]


@dataclass(frozen=True)
class World:
    requests: tuple[Request, ...]
    vehicles: tuple[Vehicle, ...]
    disruptions: tuple[Disruption, ...] = ()
    horizon: float = 240.0
    speed: float = 1.0
    late_window: float = 30.0
    cancellation_fee: float = 8.0
    area: float = 100.0
    forecasts: tuple[WaveForecast, ...] = ()


def _r(value: float, digits: int = 2) -> float:
    return round(value, digits)


def _cells(grid: int) -> list[tuple[int, int]]:
    return [(col, row) for row in range(grid) for col in range(grid)]


def zone_name(col: int, row: int) -> str:
    return f"{chr(ord('A') + row)}{col + 1}"


def _fleet(rng: random.Random, config: WorldConfig) -> list[tuple[int, float, float, Point]]:
    """(capacity, cost per distance, shift end, unit position) for each vehicle."""
    n = config.vehicles
    span = config.capacity_max - config.capacity_min
    capacities = [
        round(config.capacity_min + span * (i / (n - 1) if n > 1 else 0.5)) for i in range(n)
    ]
    rng.shuffle(capacities)
    full_shift = rng.randrange(n)
    fleet: list[tuple[int, float, float, Point]] = []
    for i, capacity in enumerate(capacities):
        size = (capacity - config.capacity_min) / span if span else 0.5
        cost_span = config.cost_per_distance_max - config.cost_per_distance_min
        cost = config.cost_per_distance_min + cost_span * (0.7 * size + 0.3 * rng.random())
        shift = config.horizon
        if i != full_shift:
            shift = rng.uniform(config.shift_end_min, config.horizon)
            shift = min(config.horizon, 5 * round(shift / 5))
        position = (rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8))
        fleet.append((capacity, _r(cost, 3), float(shift), position))
    return fleet


@dataclass
class _Draft:
    pickup: Point
    dropoff: Point
    load: int
    release: float
    ready_delay: float
    slack: float
    value: float
    urgent: bool


def make_world(seed: int, salt: str | None = None, config: WorldConfig | None = None) -> World:
    """A deterministic world for `seed`, `salt` and `config` (default `WorldConfig()`)."""
    config = config or WorldConfig()
    streams = RngStreams(seed, salt)
    fleet_rng = streams.stream("fleet")
    demand_rng = streams.stream("demand")
    disruption_rng = streams.stream("disruptions")

    fleet = _fleet(fleet_rng, config)
    grid = config.forecast_grid
    cells = _cells(grid)
    wave_length = config.horizon / config.waves
    wave_starts = [_r(i * wave_length, 1) for i in range(config.waves)]
    weights: list[list[float]] = []
    for _ in wave_starts:
        raw = [demand_rng.gammavariate(0.7, 1.0) + 1e-6 for _ in cells]
        total = sum(raw)
        weights.append([w / total for w in raw])

    count = demand_rng.randint(config.requests_min, config.requests_max)
    normal = count - config.urgent_requests
    per_wave = [normal // config.waves] * config.waves
    for wave in demand_rng.sample(range(config.waves), normal % config.waves):
        per_wave[wave] += 1
    max_capacity = max(capacity for capacity, _, _, _ in fleet)
    drafts: list[_Draft] = []
    for wave, start in enumerate(wave_starts):
        for _ in range(per_wave[wave]):
            col, row = demand_rng.choices(cells, weights[wave])[0]
            pickup = ((col + demand_rng.random()) / grid, (row + demand_rng.random()) / grid)
            dropoff = (demand_rng.random(), demand_rng.random())
            drafts.append(
                _Draft(
                    pickup,
                    dropoff,
                    min(max_capacity, demand_rng.choice((1, 1, 2, 2, 3, 4))),
                    start,
                    demand_rng.uniform(0, config.ready_delay_max),
                    demand_rng.uniform(config.deadline_slack_min, config.deadline_slack_max),
                    config.value_min * config.value_spread ** demand_rng.random(),
                    False,
                )
            )
    for _ in range(config.urgent_requests):
        start = demand_rng.choice(wave_starts)
        margin = min(5.0, wave_length / 4)
        release = start + demand_rng.uniform(margin, wave_length - margin)
        drafts.append(
            _Draft(
                (demand_rng.random(), demand_rng.random()),
                (demand_rng.random(), demand_rng.random()),
                1,
                min(config.horizon, release),
                0.0,
                config.urgent_slack,
                config.value_min
                * config.value_spread ** demand_rng.uniform(0.5, 1.0)
                * config.urgent_multiplier,
                True,
            )
        )

    # Scale the unit square so the demand/capacity ratio holds (see the module docstring).
    work = 0.0
    previous = drafts[0].dropoff
    for draft in drafts:
        work += math.dist(draft.pickup, draft.dropoff) + 0.5 * math.dist(previous, draft.pickup)
        previous = draft.dropoff
    supply = sum(shift for _, _, shift, _ in fleet) * config.speed
    area = _r(min(400.0, max(10.0, config.demand_capacity_ratio * supply / work)), 1)

    def scale(point: Point) -> Point:
        return (_r(point[0] * area), _r(point[1] * area))

    vehicles = tuple(
        Vehicle(f"vehicle-{i + 1}", f"driver-{i + 1}", capacity, scale(position), cost, shift)
        for i, (capacity, cost, shift, position) in enumerate(fleet)
    )
    built: list[Request] = []
    for draft in drafts:
        pickup, dropoff = scale(draft.pickup), scale(draft.dropoff)
        if pickup == dropoff:
            dropoff = (_r(min(area, dropoff[0] + 1.0)), dropoff[1])
        release = _r(draft.release, 1)
        ready = _r(release + draft.ready_delay, 1)
        direct = math.dist(pickup, dropoff) / config.speed
        built.append(
            Request(
                id="",
                pickup=pickup,
                dropoff=dropoff,
                load=draft.load,
                release=release,
                deadline=_r(ready + direct + draft.slack, 1),
                value=_r(draft.value, 1),
                ready_time=ready,
                urgent=draft.urgent,
            )
        )
    built.sort(key=lambda r: (r.release, r.urgent, r.pickup, r.dropoff))
    width = max(2, len(str(len(built))))
    requests = tuple(
        Request(
            f"request-{i + 1:0{width}d}",
            r.pickup,
            r.dropoff,
            r.load,
            r.release,
            r.deadline,
            r.value,
            r.ready_time,
            r.urgent,
        )
        for i, r in enumerate(built)
    )

    disruptions = _disruptions(disruption_rng, config, vehicles, requests, wave_starts, area)
    cell = area / grid
    expected_per_wave = (
        (config.requests_min + config.requests_max) / 2 - config.urgent_requests
    ) / config.waves
    forecasts = tuple(
        WaveForecast(
            start,
            tuple(
                (
                    zone_name(col, row),
                    (_r(col * cell), _r(row * cell), _r((col + 1) * cell), _r((row + 1) * cell)),
                    _r(expected_per_wave * weight, 1),
                )
                for (col, row), weight in zip(cells, weights[wave], strict=True)
            ),
        )
        for wave, start in enumerate(wave_starts)
    )
    return World(
        requests=requests,
        vehicles=vehicles,
        disruptions=disruptions,
        horizon=config.horizon,
        speed=config.speed,
        late_window=config.late_window,
        cancellation_fee=config.cancellation_fee,
        area=area,
        forecasts=forecasts,
    )


def _disruptions(
    rng: random.Random,
    config: WorldConfig,
    vehicles: tuple[Vehicle, ...],
    requests: tuple[Request, ...],
    wave_starts: list[float],
    area: float,
) -> tuple[Disruption, ...]:
    drafts: list[Disruption] = []
    for vehicle in vehicles:
        if rng.random() >= config.breakdown_rate:
            continue
        latest = min(vehicle.shift_end, config.horizon) - 30
        if latest <= 20:
            continue
        drafts.append(
            Disruption(
                "",
                "breakdown",
                _r(rng.uniform(20, latest), 1),
                vehicle_id=vehicle.id,
                duration=_r(
                    rng.uniform(config.breakdown_duration_min, config.breakdown_duration_max), 1
                ),
            )
        )
    for request in requests:
        window = max(1.0, request.deadline - request.release)
        if rng.random() < config.cancellation_rate:
            drafts.append(
                Disruption(
                    "",
                    "cancellation",
                    _r(min(config.horizon, request.release + rng.uniform(3, 3 + window)), 1),
                    request_id=request.id,
                    silent=rng.random() < config.silent_share,
                )
            )
        elif rng.random() < config.dropoff_change_rate:
            point = (_r(rng.uniform(0, area)), _r(rng.uniform(0, area)))
            if point == request.pickup:
                point = (_r(min(area, point[0] + 1.0)), point[1])
            drafts.append(
                Disruption(
                    "",
                    "dropoff_change",
                    _r(min(config.horizon, request.release + rng.uniform(3, 3 + 0.6 * window)), 1),
                    request_id=request.id,
                    point=point,
                    silent=rng.random() < config.silent_share,
                )
            )
    for start in wave_starts:
        if rng.random() >= config.traffic_rate:
            continue
        side = rng.uniform(0.25, 0.4) * area
        x, y = rng.uniform(0, area - side), rng.uniform(0, area - side)
        drafts.append(
            Disruption(
                "",
                "traffic_zone",
                _r(min(config.horizon, start + rng.uniform(0, 30)), 1),
                duration=config.traffic_duration,
                bounds=(_r(x), _r(y), _r(x + side), _r(y + side)),
                factor=config.traffic_factor,
            )
        )
    drafts.sort(
        key=lambda d: (
            d.time,
            DISRUPTION_KINDS.index(d.kind),
            d.vehicle_id or "",
            d.request_id or "",
        )
    )
    return tuple(
        Disruption(
            f"disruption-{i + 1:02d}",
            d.kind,
            d.time,
            d.vehicle_id,
            d.request_id,
            d.duration,
            d.point,
            d.bounds,
            d.factor,
            d.silent,
        )
        for i, d in enumerate(drafts)
    )


__all__ = [
    "DISRUPTION_KINDS",
    "Bounds",
    "Disruption",
    "Point",
    "Request",
    "Vehicle",
    "WaveForecast",
    "World",
    "WorldConfig",
    "make_world",
    "zone_name",
]

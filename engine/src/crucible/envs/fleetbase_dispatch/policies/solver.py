"""Pickup-and-delivery route planning on plain data.

The reference operator fills a `Problem` from public tool results. The private hindsight
bound fills it from the complete world. Nothing here reads a task, world or simulator.

Model
    A truck starts at `position` at time `start` with its `onboard` jobs. It drives its
    stops in order, in straight lines at `speed`. Inside a known traffic zone that is
    active when the leg starts or begins during it, the part of the leg inside the zone
    is slowed by the zone's factor. The truck stands still during its `downtime`
    windows and waits at a pickup until the job's ready time. A delivery earns the
    job's value, reduced linearly to 0 over `late_window` after the deadline, if it is
    made by the truck's shift end and the horizon, and before the job's `due` time.
    Every distance unit costs the truck's `cost`. Capacity holds at every stop.
    `Settings.time_cost` adds an opportunity cost per minute of truck time to the
    search (not to `route_profit`): a busy truck cannot take later, better jobs.

    With `early_departure`, a pickup may come before its job is released, but only when
    the truck is empty there: an operator can send an idle truck ahead, but a truck with
    cargo would drive its committed dropoffs first.

Search (`plan`)
    1. Keep the given routes. Remove jobs whose removal increases the profit.
    2. Insert the open jobs by regret: in each round, pick the job with the highest
       best gain plus `regret` times the gap to its second-best truck, and insert it at
       its best position (pickup and dropoff positions searched jointly).
    3. Improve: 2-opt (segment reversal) inside each route and relocation of single
       jobs between routes, then try the unserved jobs again. Repeat `passes` times.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

Point = tuple[float, float]
Stop = tuple[str, str]  # (job id, "pickup" | "dropoff")
PICKUP = "pickup"
DROPOFF = "dropoff"
EPS = 1e-9
_GAIN_EPS = 1e-6
_NEG = -math.inf


@dataclass(frozen=True)
class Job:
    id: str
    pickup: Point
    dropoff: Point
    load: int
    ready: float
    deadline: float
    value: float
    release: float = 0.0
    due: float = math.inf
    new_dropoff: Point | None = None
    change_time: float = math.inf


@dataclass(frozen=True)
class Zone:
    bounds: tuple[float, float, float, float]
    start: float
    end: float
    factor: float


@dataclass(frozen=True)
class Truck:
    id: str
    position: Point
    start: float
    capacity: int
    cost: float
    shift_end: float
    onboard: tuple[str, ...] = ()
    downtime: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Problem:
    now: float
    horizon: float
    jobs: Mapping[str, Job]
    trucks: tuple[Truck, ...]
    zones: tuple[Zone, ...] = ()
    speed: float = 1.0
    late_window: float = 30.0
    early_departure: bool = False


@dataclass(frozen=True)
class Settings:
    regret: float = 1.0
    passes: int = 2
    min_gain: float = 0.0
    time_cost: float = 0.0


Leg = tuple[Job, bool]  # (job, is pickup)


@dataclass
class _States:
    """State before each stop of a route: time, position, load and profit so far."""

    time: list[float] = field(default_factory=list[float])
    pos: list[Point] = field(default_factory=list[Point])
    load: list[int] = field(default_factory=list[int])
    acc: list[float] = field(default_factory=list[float])

    @property
    def total(self) -> float:
        return self.acc[-1]


def _inside(a: Point, b: Point, bounds: tuple[float, float, float, float]) -> float:
    """Fraction of segment a→b inside the rectangle (Liang-Barsky clipping)."""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in (
        (-dx, a[0] - bounds[0]),
        (dx, bounds[2] - a[0]),
        (-dy, a[1] - bounds[1]),
        (dy, bounds[3] - a[1]),
    ):
        if abs(p) < EPS:
            if q < 0:
                return 0.0
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return 0.0
            t0 = max(t0, r)
        else:
            if r < t0:
                return 0.0
            t1 = min(t1, r)
    return max(0.0, t1 - t0)


class _Model:
    def __init__(self, problem: Problem, time_cost: float = 0.0) -> None:
        self.time_cost = time_cost
        self.problem = problem
        self.jobs = problem.jobs
        self.zones = problem.zones
        self.speed = problem.speed
        self.lw = problem.late_window
        self.horizon = problem.horizon
        self.early = problem.early_departure

    # Physics ---------------------------------------------------------------------------

    def _move(self, truck: Truck, a: Point, b: Point, t: float) -> tuple[float, float]:
        """(arrival time, distance) of the leg a→b that starts no earlier than t."""
        for start, end in truck.downtime:
            if start <= t < end:
                t = end
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d <= EPS:
            return t, 0.0
        minutes = d / self.speed
        for zone in self.zones:
            if zone.start > t + minutes or zone.end <= t:
                continue
            fraction = _inside(a, b, zone.bounds)
            if fraction > 0:
                minutes += fraction * d / self.speed * (1.0 / zone.factor - 1.0)
        arrival = t + minutes
        for start, end in truck.downtime:
            if t < start < arrival:
                arrival += end - start
        return arrival, d

    def _visit(
        self, truck: Truck, leg: Leg, t: float, pos: Point, load: int, acc: float
    ) -> tuple[float, Point, int, float] | None:
        job, pickup = leg
        before = t
        if pickup:
            if job.release > t + EPS and (load > 0 or not self.early):
                return None
            target = job.pickup
            t, d = self._move(truck, pos, target, t)
            t = max(t, job.ready, job.release)
            load += job.load
            if load > truck.capacity:
                return None
            return t, target, load, acc - truck.cost * d - self.time_cost * (t - before)
        target = job.dropoff
        if job.new_dropoff is not None and t >= job.change_time:
            target = job.new_dropoff
        t, d = self._move(truck, pos, target, t)
        acc -= truck.cost * d + self.time_cost * (t - before)
        if t <= truck.shift_end + EPS and t <= self.horizon + EPS and t < job.due - EPS:
            if t <= job.deadline + EPS:
                acc += job.value
            elif t < job.deadline + self.lw:
                acc += job.value * (1.0 - (t - job.deadline) / self.lw)
        return t, target, load - job.load, acc

    def initial(self, truck: Truck) -> tuple[float, Point, int, float]:
        load = sum(self.jobs[j].load for j in truck.onboard if j in self.jobs)
        return truck.start, truck.position, load, 0.0

    def states(self, truck: Truck, route: Sequence[Leg]) -> _States | None:
        t, pos, load, acc = self.initial(truck)
        st = _States([t], [pos], [load], [acc])
        for leg in route:
            nxt = self._visit(truck, leg, t, pos, load, acc)
            if nxt is None:
                return None
            t, pos, load, acc = nxt
            st.time.append(t)
            st.pos.append(pos)
            st.load.append(load)
            st.acc.append(acc)
        return st

    def profit(self, truck: Truck, route: Sequence[Leg]) -> float:
        st = self.states(truck, route)
        return _NEG if st is None else st.total

    def rest(
        self,
        truck: Truck,
        route: Sequence[Leg],
        st: _States,
        start: int,
        state: tuple[float, Point, int, float],
    ) -> float:
        """Total profit when the base route continues from index `start` in `state`."""
        t, pos, load, acc = state
        n = len(route)
        for m in range(start, n):
            if (
                abs(t - st.time[m]) <= EPS
                and load == st.load[m]
                and pos[0] == st.pos[m][0]
                and pos[1] == st.pos[m][1]
            ):
                return acc + st.total - st.acc[m]
            nxt = self._visit(truck, route[m], t, pos, load, acc)
            if nxt is None:
                return _NEG
            t, pos, load, acc = nxt
        return acc

    def best_insertion(
        self, truck: Truck, route: Sequence[Leg], st: _States, job: Job
    ) -> tuple[float, int, int]:
        """(gain, pickup index, dropoff index) of the best insertion of `job`."""
        best: tuple[float, int, int] = (_NEG, -1, -1)
        if job.load > truck.capacity:
            return best
        limit = min(job.deadline + self.lw, job.due, truck.shift_end, self.horizon)
        # Lower bound on the delivery time from the truck's start.
        reach = (
            max(truck.start, job.ready, job.release)
            + math.dist(job.pickup, job.dropoff) / self.speed
        )
        if reach > limit:
            return best
        n = len(route)
        base = st.total
        pickup_leg: Leg = (job, True)
        dropoff_leg: Leg = (job, False)
        for i in range(n + 1):
            if st.time[i] > limit:
                break
            if st.load[i] + job.load > truck.capacity:
                continue
            state = self._visit(truck, pickup_leg, st.time[i], st.pos[i], st.load[i], st.acc[i])
            if state is None:
                continue
            for j in range(i, n + 1):
                t, pos, load, acc = state
                dropped = self._visit(truck, dropoff_leg, t, pos, load, acc)
                if dropped is not None:
                    gain = self.rest(truck, route, st, j, dropped) - base
                    if gain > best[0] + _GAIN_EPS:
                        best = (gain, i, j)
                if j == n or t > limit:
                    break
                nxt = self._visit(truck, route[j], t, pos, load, acc)
                if nxt is None:
                    break
                state = nxt
        return best


def _insert(route: list[Leg], job: Job, i: int, j: int) -> list[Leg]:
    return [*route[:i], (job, True), *route[i:j], (job, False), *route[j:]]


def _without(route: Sequence[Leg], job_id: str) -> list[Leg]:
    return [leg for leg in route if leg[0].id != job_id]


class _Search:
    def __init__(self, problem: Problem, settings: Settings) -> None:
        self.model = _Model(problem, settings.time_cost)
        self.problem = problem
        self.settings = settings
        self.trucks = {t.id: t for t in problem.trucks}
        self.onboard = {j for t in problem.trucks for j in t.onboard}
        self.routes: dict[str, list[Leg]] = {t.id: [] for t in problem.trucks}
        self.states: dict[str, _States] = {}

    def _refresh(self, truck_id: str) -> None:
        truck = self.trucks[truck_id]
        st = self.model.states(truck, self.routes[truck_id])
        if st is None:
            # Only possible for routes passed in: keep the cargo's dropoffs and plan
            # the other jobs again.
            self.routes[truck_id] = [
                leg for leg in self.routes[truck_id] if leg[0].id in truck.onboard
            ]
            st = self.model.states(truck, self.routes[truck_id])
            assert st is not None
        self.states[truck_id] = st

    def load(self, routes: Mapping[str, Sequence[Stop]]) -> None:
        jobs = self.problem.jobs
        placed: set[str] = set()
        for truck in self.problem.trucks:
            given = routes.get(truck.id, ())
            legs: list[Leg] = []
            seen: set[Stop] = set()
            for job_id, action in given:
                job = jobs.get(job_id)
                if job is None or (job_id, action) in seen or job_id in placed:
                    continue
                if action == PICKUP and job_id in truck.onboard:
                    continue
                seen.add((job_id, action))
                legs.append((job, action == PICKUP))
            ids = {leg[0].id for leg in legs}
            complete: list[Leg] = []
            for leg in legs:
                job = leg[0]
                has_pickup = (job.id, PICKUP) in seen
                has_dropoff = (job.id, DROPOFF) in seen
                if job.id in truck.onboard:
                    if not leg[1]:
                        complete.append(leg)
                elif has_pickup and has_dropoff:
                    complete.append(leg)
            for job_id in truck.onboard:
                if job_id in jobs and (job_id, DROPOFF) not in seen:
                    complete.append((jobs[job_id], False))
            # A dropoff listed before its pickup: move the dropoff right after the pickup.
            ordered: list[Leg] = []
            picked: set[str] = set(truck.onboard)
            pending: list[Leg] = []
            for leg in complete:
                job, pickup = leg
                if pickup:
                    ordered.append(leg)
                    picked.add(job.id)
                    for waiting in [p for p in pending if p[0].id == job.id]:
                        ordered.append(waiting)
                        pending.remove(waiting)
                elif job.id in picked:
                    ordered.append(leg)
                else:
                    pending.append(leg)
            self.routes[truck.id] = ordered
            placed |= ids
        for truck_id in self.routes:
            self._refresh(truck_id)

    def unserved(self) -> list[Job]:
        served = {leg[0].id for route in self.routes.values() for leg in route}
        return [
            job
            for job_id, job in sorted(self.problem.jobs.items())
            if job_id not in served and job_id not in self.onboard
        ]

    # Moves -----------------------------------------------------------------------------

    def drop_losers(self) -> None:
        for truck_id, route in self.routes.items():
            changed = True
            while changed:
                changed = False
                base = self.states[truck_id].total
                for job_id in sorted({leg[0].id for leg in route} - self.onboard):
                    shorter = _without(route, job_id)
                    if self.model.profit(self.trucks[truck_id], shorter) > base + _GAIN_EPS:
                        route = shorter
                        self.routes[truck_id] = route
                        self._refresh(truck_id)
                        changed = True
                        break

    def insert(self, pool: Iterable[Job]) -> None:
        pool = list(pool)
        cache: dict[tuple[str, str], tuple[float, int, int]] = {}

        def options(job: Job) -> list[tuple[float, str, int, int]]:
            found: list[tuple[float, str, int, int]] = []
            for truck in self.problem.trucks:
                key = (job.id, truck.id)
                if key not in cache:
                    cache[key] = self.model.best_insertion(
                        truck, self.routes[truck.id], self.states[truck.id], job
                    )
                gain, i, j = cache[key]
                if gain > self.settings.min_gain + _GAIN_EPS:
                    found.append((gain, truck.id, i, j))
            found.sort(key=lambda o: (-o[0], o[1]))
            return found

        while pool:
            choice: tuple[float, float, Job, str, int, int] | None = None
            for job in pool:
                found = options(job)
                if not found:
                    continue
                gain, truck_id, i, j = found[0]
                second = found[1][0] if len(found) > 1 else 0.0
                score = gain + self.settings.regret * (gain - max(second, 0.0))
                if choice is None or score > choice[0] + _GAIN_EPS:
                    choice = (score, gain, job, truck_id, i, j)
            if choice is None:
                return
            _, _, job, truck_id, i, j = choice
            self.routes[truck_id] = _insert(self.routes[truck_id], job, i, j)
            self._refresh(truck_id)
            pool.remove(job)
            for key in [k for k in cache if k[1] == truck_id]:
                del cache[key]

    def two_opt(self, truck_id: str) -> bool:
        truck = self.trucks[truck_id]
        improved = False
        route: list[Leg] = self.routes[truck_id]
        st = self.states[truck_id]
        n = len(route)
        a = 0
        while a < n - 1:
            done = False
            for b in range(a + 2, n + 1):
                segment: list[Leg] = route[a:b]
                ids = [leg[0].id for leg in segment]
                if len(set(ids)) != len(ids):
                    # Reversing would put a dropoff before its pickup.
                    break
                candidate: list[Leg] = [*route[:a], *reversed(segment), *route[b:]]
                total = self.model.rest(
                    truck,
                    candidate,
                    st,
                    a,
                    (st.time[a], st.pos[a], st.load[a], st.acc[a]),
                )
                if total > st.total + _GAIN_EPS:
                    self.routes[truck_id] = candidate
                    self._refresh(truck_id)
                    route = self.routes[truck_id]
                    st = self.states[truck_id]
                    improved = done = True
                    break
            if not done:
                a += 1
        return improved

    def relocate(self) -> bool:
        improved = False
        for truck_id in list(self.routes):
            for job_id in sorted({leg[0].id for leg in self.routes[truck_id]} - self.onboard):
                route = self.routes[truck_id]
                if not any(leg[0].id == job_id for leg in route):
                    continue
                job = self.problem.jobs[job_id]
                shorter = _without(route, job_id)
                truck = self.trucks[truck_id]
                shorter_st = self.model.states(truck, shorter)
                if shorter_st is None:
                    continue
                removal = shorter_st.total - self.states[truck_id].total
                best: tuple[float, str, int, int] | None = None
                for other in self.problem.trucks:
                    if other.id == truck_id:
                        gain, i, j = self.model.best_insertion(other, shorter, shorter_st, job)
                    else:
                        gain, i, j = self.model.best_insertion(
                            other, self.routes[other.id], self.states[other.id], job
                        )
                    if best is None or gain > best[0] + _GAIN_EPS:
                        best = (gain, other.id, i, j)
                if best is None or best[2] < 0:
                    continue
                gain, target, i, j = best
                if removal + gain <= _GAIN_EPS * 10:
                    continue
                self.routes[truck_id] = shorter
                self._refresh(truck_id)
                self.routes[target] = _insert(self.routes[target], job, i, j)
                self._refresh(target)
                improved = True
        return improved

    def run(self) -> None:
        self.drop_losers()
        self.insert(self.unserved())
        for _ in range(self.settings.passes):
            changed = False
            for truck_id in list(self.routes):
                changed = self.two_opt(truck_id) or changed
            changed = self.relocate() or changed
            before = len(self.unserved())
            self.insert(self.unserved())
            changed = changed or len(self.unserved()) != before
            if not changed:
                break

    def result(self) -> dict[str, list[Stop]]:
        return {
            truck_id: [(job.id, PICKUP if pickup else DROPOFF) for job, pickup in route]
            for truck_id, route in self.routes.items()
        }


def plan(
    problem: Problem,
    routes: Mapping[str, Sequence[Stop]] | None = None,
    settings: Settings | None = None,
) -> dict[str, list[Stop]]:
    """Improved routes for every truck, starting from `routes` (see the module docstring)."""
    search = _Search(problem, settings or Settings())
    search.load(routes or {})
    search.run()
    return search.result()


def route_profit(problem: Problem, truck: Truck, route: Sequence[Stop]) -> float:
    """Planned profit of `route` for `truck` under the model, or -inf if infeasible."""
    legs: list[Leg] = []
    for job_id, action in route:
        job = problem.jobs.get(job_id)
        if job is None:
            return _NEG
        legs.append((job, action == PICKUP))
    return _Model(problem).profit(truck, legs)


__all__ = [
    "DROPOFF",
    "PICKUP",
    "Job",
    "Point",
    "Problem",
    "Settings",
    "Stop",
    "Truck",
    "Zone",
    "plan",
    "route_profit",
]

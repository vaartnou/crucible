"""Seeded, causal service graphs and chronological production changes."""

import hashlib
import random
from dataclasses import dataclass, field

FAULTS = (
    "partial_rollout",
    "config_push",
    "memory_leak",
    "pool_exhaustion",
    "retry_storm",
    "queue_backlog",
)
HORIZON = 150


@dataclass(frozen=True)
class Service:
    name: str
    kind: str
    dependencies: tuple[str, ...]
    capacity: float = 120
    replicas: int = 3
    base_latency: float = 0.025
    version: int = 17


@dataclass(frozen=True)
class Edge:
    caller: str
    callee: str
    timeout_ms: float = 2000
    pool_size: float = 128
    retries: float = 0
    rate_multiplier: float = 1


@dataclass(frozen=True)
class Change:
    id: str
    minute: int
    service: str
    kind: str
    summary: str
    before: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    after: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    callee: str | None = None
    author: str = "release-bot"


@dataclass(frozen=True)
class Build:
    service: str
    version: int
    pod_error: float = 0
    bad_pods: int = 0
    leak_mib_min: float = 0
    consumer_running: float = 1


@dataclass(frozen=True)
class World:
    services: tuple[Service, ...]
    changes: tuple[Change, ...]
    culprit: str
    fault: str
    change_id: str
    fingerprint: str
    edges: tuple[Edge, ...] = ()
    entry_rate: float = 180
    builds: tuple[Build, ...] = ()


def make_world(seed: int, salt: str | None = None, difficulty: int = 0) -> World:
    digest = hashlib.sha256(f"oncall-v2:{seed}:{salt or ''}".encode()).hexdigest()
    rng = random.Random(digest)
    branches = 2 if difficulty == 1 else 7
    names = rng.sample(
        [
            "payments",
            "catalog",
            "identity",
            "search",
            "orders",
            "shipping",
            "billing",
            "inventory",
            "notify",
        ],
        branches,
    )
    services: list[Service] = []
    for name in names:
        suffix = rng.randrange(10, 99)
        db, worker, api = (f"{name}-{kind}-{suffix}" for kind in ("db", "worker", "api"))
        services.extend(
            (
                Service(db, "db", (), base_latency=0.12, version=rng.randrange(10, 80)),
                Service(worker, "worker", (db,), version=rng.randrange(10, 80)),
                Service(api, "http", (worker,), version=rng.randrange(10, 80)),
            )
        )
    services.append(Service("session-cache", "cache", (), version=rng.randrange(10, 80)))
    services.append(
        Service(
            "edge-gateway",
            "http",
            (*tuple(s.name for s in services if s.kind == "http"), "session-cache"),
            version=rng.randrange(10, 80),
        )
    )
    edges = tuple(Edge(s.name, callee) for s in services for callee in s.dependencies)
    fault = rng.choice(FAULTS[:2] if difficulty == 1 else FAULTS)
    candidates = [s for s in services if s.name != "edge-gateway" and s.kind != "cache"]
    if fault in ("config_push", "pool_exhaustion", "retry_storm"):
        candidates = [s for s in candidates if s.dependencies]
    elif fault == "queue_backlog":
        candidates = [s for s in candidates if s.kind == "worker"]
    culprit = rng.choice(candidates)
    changes: list[Change] = []
    used_ids: set[str] = set()

    def add(
        minute: int,
        service: Service,
        kind: str,
        before: dict[str, float],
        after: dict[str, float],
        callee: str | None = None,
    ) -> str:
        identifier = f"chg-{rng.randrange(100000, 999999)}"
        while identifier in used_ids:
            identifier = f"chg-{rng.randrange(100000, 999999)}"
        used_ids.add(identifier)
        summary = "; ".join(f"{key}: {before[key]:g} -> {value:g}" for key, value in after.items())
        changes.append(
            Change(
                identifier,
                minute,
                service.name,
                kind,
                summary,
                before,
                after,
                callee,
                rng.choice(("release-bot", "platform", "service-owner")),
            )
        )
        return identifier

    # Every change is an effective parameter update, including irrelevant deployments.
    for service in services:
        add(
            -25,
            service,
            "deploy",
            {"version": float(service.version)},
            {"version": float(service.version + 1)},
        )
    # Plausible changes before and after the incident, including on the actual owner.
    for service in [*rng.sample([s for s in services if s.name != culprit.name], 4), culprit]:
        add(
            rng.choice((-3, -2, -1, 0)),
            service,
            "config",
            {"capacity": service.capacity},
            {"capacity": service.capacity * 1.05},
        )
        add(
            rng.choice((1, 2, 3)),
            service,
            "config",
            {"capacity": service.capacity * 1.05},
            {"capacity": service.capacity * 1.1},
        )
        if service.dependencies:
            add(
                -8,
                service,
                "config",
                {"pool_size": 128.0},
                {"pool_size": 144.0},
                service.dependencies[-1],
            )
    # Safe reductions and retry increases are real production changes too. Their
    # values remain above healthy latency/occupancy, so direction alone is not evidence.
    owners = [s for s in services if s.dependencies and s.name != "edge-gateway"]
    safe_owners = rng.sample(
        [s for s in owners if s.name != culprit.name],
        min(4, len(owners) - int(bool(culprit.dependencies))),
    )
    if culprit.dependencies:
        safe_owners.append(culprit)
    for service in safe_owners:
        target = service.dependencies[0]
        times = sorted(rng.sample(range(-7, -1), 3))
        pool_before = (
            144.0
            if any(
                c.service == service.name and c.callee == target and "pool_size" in c.after
                for c in changes
            )
            else 128.0
        )
        for minute, key, old, new in zip(
            times,
            ("timeout_ms", "pool_size", "retries"),
            (2000.0, pool_before, 0.0),
            (1200.0, 96.0, 1.0),
            strict=True,
        ):
            add(minute, service, "config", {key: old}, {key: new}, target)
    before: dict[str, float]
    after: dict[str, float]
    callee = culprit.dependencies[0] if culprit.dependencies else None
    kind = "config" if fault in ("config_push", "pool_exhaustion", "retry_storm") else "deploy"
    if fault == "config_push":
        before, after = {"timeout_ms": 1200.0}, {"timeout_ms": 90.0}
    elif fault == "pool_exhaustion":
        before, after = {"pool_size": 96.0}, {"pool_size": 8.0}
    elif fault == "retry_storm":
        before, after = (
            {"retries": 1.0, "timeout_ms": 1200.0, "pool_size": 96.0},
            {"retries": 5.0, "timeout_ms": 220.0, "pool_size": 4096.0},
        )
    else:
        before, after = (
            {"version": float(culprit.version + 1)},
            {"version": float(culprit.version + 2)},
        )
        callee = None
    builds: tuple[Build, ...] = ()
    if kind == "deploy":
        builds = (
            Build(
                culprit.name,
                culprit.version + 2,
                pod_error=0.6 if fault == "partial_rollout" else 0.0,
                bad_pods=1 if fault == "partial_rollout" else 0,
                leak_mib_min=40.0 if fault == "memory_leak" else 0.0,
                consumer_running=0.0 if fault == "queue_backlog" else 1.0,
            ),
        )
    early = rng.sample([s for s in services if s.name != culprit.name], 2)
    for service in early:
        add(
            rng.randint(-24, -22),
            service,
            "deploy",
            {"version": float(service.version + 1)},
            {"version": float(service.version + 2)},
        )
    for service in rng.sample(
        [s for s in services if s.name != culprit.name and s not in early], 4
    ):
        add(
            rng.choice((-1, 0, 1)),
            service,
            "deploy",
            {"version": float(service.version + 1)},
            {"version": float(service.version + 2)},
        )
    change_id = add(
        rng.randint(-24, -22) if fault == "memory_leak" else 0, culprit, kind, before, after, callee
    )
    return World(
        tuple(services),
        tuple(sorted(changes, key=lambda c: (c.minute, c.id))),
        culprit.name,
        fault,
        change_id,
        digest,
        edges,
        builds=builds,
    )

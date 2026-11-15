"""Seeded, causal service graphs and chronological production changes."""

import hashlib
import math
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

STEMS = (
    "ledger",
    "vault",
    "fraud",
    "quote",
    "tariff",
    "cart",
    "checkout",
    "routing",
    "scoring",
    "profile",
    "audit",
    "coupon",
    "parcel",
    "invoice",
    "loyalty",
    "webhook",
    "dispatch",
    "catalog",
    "wallet",
    "rebate",
    "settlement",
    "customs",
    "inventory",
    "fulfill",
    "pricing",
    "beacon",
    "nexus",
    "relay",
)


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


def kind_of(name: str, services_map: dict[str, Service]) -> str:
    if name == "session-cache":
        return "cache"
    if name == "edge-gateway":
        return "http"
    return services_map[name].kind


def make_world(seed: int, salt: str | None = None, difficulty: int = 0) -> World:
    digest = hashlib.sha256(f"oncall-v2:{seed}:{salt or ''}".encode()).hexdigest()
    rng = random.Random(digest)
    branches = 2 if difficulty == 1 else 7
    _legacy = rng.sample(
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
    for _ in _legacy:
        rng.randrange(10, 99)
        for _ in range(3):
            rng.randrange(10, 80)
    rng.randrange(10, 80)
    rng.randrange(10, 80)
    fault = rng.choice(FAULTS[:2] if difficulty == 1 else FAULTS)

    num_internal = 6 if difficulty == 1 else 21
    sampled_stems = rng.sample(STEMS, num_internal)
    internal_names = [f"{stem}-{rng.randrange(10, 99)}" for stem in sampled_stems]
    seen_names: set[str] = set()
    unique_internal_names: list[str] = []
    for name in internal_names:
        while name in seen_names:
            name = f"{name.rsplit('-', 1)[0]}-{rng.randrange(10, 99)}"
        seen_names.add(name)
        unique_internal_names.append(name)
    internal_names = unique_internal_names

    services_list: list[Service] = []
    services_map: dict[str, Service] = {}

    if difficulty == 1:
        t0 = internal_names[:2]
        for name in t0:
            srv = Service(name, "db", (), replicas=3, version=rng.randrange(10, 80))
            services_list.append(srv)
            services_map[name] = srv

        t1 = internal_names[2:4]
        w0, w1 = t1[0], t1[1]
        srv_w0 = Service(w0, "worker", (t0[0], t0[1]), replicas=3, version=rng.randrange(10, 80))
        srv_w1 = Service(w1, "worker", (t0[1], t0[0]), replicas=3, version=rng.randrange(10, 80))
        services_list.extend([srv_w0, srv_w1])
        services_map[w0] = srv_w0
        services_map[w1] = srv_w1

        t2 = internal_names[4:6]
        a0, a1 = t2[0], t2[1]
        srv_a0 = Service(a0, "http", (w0, w1), replicas=3, version=rng.randrange(10, 80))
        srv_a1 = Service(a1, "http", (w1, w0), replicas=3, version=rng.randrange(10, 80))
        services_list.extend([srv_a0, srv_a1])
        services_map[a0] = srv_a0
        services_map[a1] = srv_a1

        session_cache = Service(
            "session-cache", "cache", (), replicas=3, version=rng.randrange(10, 80)
        )
        edge_gateway = Service(
            "edge-gateway",
            "http",
            (a0, a1, "session-cache"),
            replicas=3,
            version=rng.randrange(10, 80),
        )
        services_list.extend([session_cache, edge_gateway])
        services_map["session-cache"] = session_cache
        services_map["edge-gateway"] = edge_gateway
    else:
        tier0_names = internal_names[0:5]
        for name in tier0_names:
            srv = Service(name, "db", (), replicas=3, version=rng.randrange(10, 80))
            services_list.append(srv)
            services_map[name] = srv

        tier1_names = internal_names[5:11]
        second_indices_t1 = set(rng.sample(range(6), 4))
        for i, name in enumerate(tier1_names):
            primary = tier0_names[i % 5]
            deps = [primary]
            if i in second_indices_t1:
                candidates_sec = [d for d in tier0_names if d != primary]
                secondary = rng.choice(candidates_sec)
                deps.append(secondary)
            srv = Service(name, "worker", tuple(deps), replicas=3, version=rng.randrange(10, 80))
            services_list.append(srv)
            services_map[name] = srv

        tier2_names = internal_names[11:16]
        second_indices_t2 = set(rng.sample(range(1, 5), 3))
        for i, name in enumerate(tier2_names):
            if i == 0:
                deps = (tier1_names[0], tier1_names[5])
            else:
                primary = tier1_names[i]
                deps = [primary]
                if i in second_indices_t2:
                    candidates_sec = [d for d in tier1_names if d != primary]
                    secondary = rng.choice(candidates_sec)
                    deps.append(secondary)
                deps = tuple(deps)
            srv = Service(name, "http", deps, replicas=3, version=rng.randrange(10, 80))
            services_list.append(srv)
            services_map[name] = srv

        tier3_names = internal_names[16:21]
        second_indices_t3 = set(rng.sample(range(5), 3))
        for i, name in enumerate(tier3_names):
            primary = tier2_names[i]
            deps = [primary]
            if i in second_indices_t3:
                candidates_sec = [d for d in tier2_names if d != primary]
                secondary = rng.choice(candidates_sec)
                deps.append(secondary)
            deps = tuple(deps)
            srv = Service(name, "http", deps, replicas=3, version=rng.randrange(10, 80))
            services_list.append(srv)
            services_map[name] = srv

        session_cache = Service(
            "session-cache", "cache", (), replicas=3, version=rng.randrange(10, 80)
        )
        edge_gateway = Service(
            "edge-gateway",
            "http",
            (*tuple(tier3_names), "session-cache"),
            replicas=3,
            version=rng.randrange(10, 80),
        )
        services_list.extend([session_cache, edge_gateway])
        services_map["session-cache"] = session_cache
        services_map["edge-gateway"] = edge_gateway

    for s in services_list:
        if s.name == "session-cache":
            bl = 0.015
        elif s.name == "edge-gateway":
            bl = 0.012
        elif s.kind == "db":
            bl = rng.choice((0.045, 0.07, 0.10, 0.14, 0.18))
        elif s.kind == "worker":
            bl = rng.choice((0.02, 0.035, 0.05, 0.07))
        elif s.kind == "http":
            bl = rng.choice((0.012, 0.02, 0.035))
        else:
            bl = 0.025
        idx = services_list.index(s)
        services_list[idx] = Service(
            name=s.name,
            kind=s.kind,
            dependencies=s.dependencies,
            capacity=s.capacity,
            replicas=s.replicas,
            base_latency=bl,
            version=s.version,
        )
        services_map[s.name] = services_list[idx]

    entry_rate = 180.0

    temp_edges: list[tuple[str, str, float]] = []
    for s in services_list:
        if s.name == "edge-gateway":
            for dep in s.dependencies:
                if dep == "session-cache":
                    rm = 0.60
                else:
                    rm = rng.choice((0.40, 0.55, 0.70, 0.85))
                temp_edges.append((s.name, dep, rm))
        else:
            if len(s.dependencies) == 1:
                rm = rng.choice((0.75, 0.90, 1.0))
                temp_edges.append((s.name, s.dependencies[0], rm))
            elif len(s.dependencies) == 2:
                rm1 = rng.choice((0.55, 0.70))
                rm2 = rng.choice((0.35, 0.50))
                temp_edges.append((s.name, s.dependencies[0], rm1))
                temp_edges.append((s.name, s.dependencies[1], rm2))

    outgoing_rms: dict[str, list[tuple[str, float]]] = {s.name: [] for s in services_list}
    for caller, callee, rm in temp_edges:
        outgoing_rms[caller].append((callee, rm))

    base_flow = {s.name: 0.0 for s in services_list}
    base_flow["edge-gateway"] = entry_rate
    for s in reversed(services_list):
        f = base_flow[s.name]
        for callee, rm in outgoing_rms[s.name]:
            base_flow[callee] += f * rm

    candidates = [s for s in services_list if s.name != "edge-gateway" and s.kind != "cache"]
    if fault in ("config_push", "pool_exhaustion"):
        candidates = [s for s in candidates if s.dependencies]
    elif fault == "retry_storm":
        candidates = [
            s
            for s in candidates
            if s.dependencies and kind_of(s.dependencies[0], services_map) == "db"
        ]
        if not candidates:
            candidates = [
                s
                for s in candidates
                if s.dependencies and kind_of(s.dependencies[0], services_map) == "worker"
            ]
        if not candidates:
            candidates = [
                s
                for s in services_list
                if s.name != "edge-gateway" and s.kind != "cache" and s.dependencies
            ]
    elif fault == "queue_backlog":
        candidates = [s for s in candidates if s.kind == "worker"]
        if not candidates:
            candidates = [
                s for s in services_list if s.name != "edge-gateway" and s.kind != "cache"
            ]
    culprit = rng.choice(candidates)

    high_util_set: set[str] = set()
    if fault in ("config_push", "retry_storm") and culprit.dependencies:
        d0 = culprit.dependencies[0]
        high_util_set.add(d0)
        if d0 in services_map and services_map[d0].dependencies:
            high_util_set.add(services_map[d0].dependencies[0])

    capacities: dict[str, float] = {}
    for s in services_list:
        s_name = s.name
        if fault == "config_push" and s_name in high_util_set:
            u_1 = 0.62
        elif fault == "retry_storm" and s_name in high_util_set:
            u_1 = 0.65
        elif fault == "queue_backlog" and s_name == culprit.name:
            u_1 = 0.52
        else:
            u_1 = rng.choice((0.36, 0.44, 0.52))

        bf = base_flow[s_name]
        if s_name == "edge-gateway" or bf == 0.0:
            cap = max(40.0, round(max(bf * 1.5, 40.0), 1))
        else:
            cap = max(40.0, round(bf / (3.0 * u_1), 1))
        capacities[s_name] = cap

    for i, s in enumerate(services_list):
        services_list[i] = Service(
            name=s.name,
            kind=s.kind,
            dependencies=s.dependencies,
            capacity=capacities[s.name],
            replicas=s.replicas,
            base_latency=s.base_latency,
            version=s.version,
        )
        services_map[s.name] = services_list[i]
    culprit = services_map[culprit.name]

    def compute_lat_and_conc(
        scale: float,
    ) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
        lats: dict[str, float] = {}
        for s in services_list:
            s_name = s.name
            s_obj = services_map[s_name]
            total_cap = s_obj.capacity * s_obj.replicas
            rate = scale * base_flow[s_name]
            util = rate / total_cap if total_cap > 0 else 0.0
            util = min(0.999, util)
            own_l = s_obj.base_latency / (1.0 - util)
            dep_lats = [s_obj.base_latency + lats[dep] for dep in s_obj.dependencies]
            lats[s_name] = max([own_l, *dep_lats])

        peak_concs: dict[tuple[str, str], float] = {}
        for caller, callee, rm in temp_edges:
            caller_flow = scale * base_flow[caller]
            attempt_flow = caller_flow * rm
            peak_concs[(caller, callee)] = attempt_flow * lats[callee]
        return lats, peak_concs

    peak_lats, peak_concs = compute_lat_and_conc(1.03)

    edges_list: list[Edge] = []
    for caller, callee, rm in temp_edges:
        e_key = (caller, callee)
        p_lat = peak_lats[callee]
        p_conc = peak_concs[e_key]
        init_timeout = round(max(300.0, p_lat * 1000 * rng.choice((2.4, 2.8, 3.2))), -1)
        init_pool = float(max(24, math.ceil(p_conc * rng.choice((2.2, 2.6, 3.0)))))
        edges_list.append(
            Edge(
                caller=caller,
                callee=callee,
                timeout_ms=init_timeout,
                pool_size=init_pool,
                retries=0.0,
                rate_multiplier=rm,
            )
        )
    edges_tuple = tuple(edges_list)

    reachable_from_culprit: set[str] = set()
    queue_stack = list(culprit.dependencies)
    while queue_stack:
        dep = queue_stack.pop()
        if dep not in reachable_from_culprit:
            reachable_from_culprit.add(dep)
            if dep in services_map:
                queue_stack.extend(services_map[dep].dependencies)

    raw_specs: list[
        tuple[
            int,
            str,
            str,
            str,
            str,
            dict[str, float],
            str | None,
            str,
            bool,
        ]
    ] = []
    used_ids: set[str] = set()

    def schedule_spec(
        minute: int,
        service: str,
        kind: str,
        spec_type: str,
        payload: dict[str, float],
        callee: str | None = None,
        is_trigger: bool = False,
    ) -> str:
        identifier = f"chg-{rng.randrange(100000, 999999)}"
        while identifier in used_ids:
            identifier = f"chg-{rng.randrange(100000, 999999)}"
        used_ids.add(identifier)
        author = rng.choice(("release-bot", "platform", "service-owner"))
        raw_specs.append(
            (
                minute,
                identifier,
                service,
                kind,
                spec_type,
                payload,
                callee,
                author,
                is_trigger,
            )
        )
        return identifier

    for s in services_list:
        schedule_spec(-25, s.name, "deploy", "deploy", {}, None)

    non_culpritts = [s.name for s in services_list if s.name != culprit.name]
    second_wave_services = rng.sample(non_culpritts, min(5, len(non_culpritts)))
    if fault in ("partial_rollout", "queue_backlog"):
        if culprit.name not in second_wave_services:
            second_wave_services.append(culprit.name)
    for s_name in second_wave_services:
        schedule_spec(rng.randint(-20, -15), s_name, "deploy", "deploy", {}, None)

    non_culprit_callers = [
        s
        for s in services_list
        if s.dependencies and s.name != culprit.name and s.name != "edge-gateway"
    ]
    sampled_safe_callers = rng.sample(non_culprit_callers, min(6, len(non_culprit_callers)))

    def apply_safe_edge_specs(service_name: str, callee_name: str, times: list[int]) -> None:
        e_key = (service_name, callee_name)
        p_lat = peak_lats[callee_name]
        p_conc = peak_concs[e_key]
        target_timeout = round(p_lat * 1000 * 1.55, 1)
        target_pool = float(max(12, math.ceil(p_conc * 1.45)))
        if len(times) >= 1:
            schedule_spec(
                times[0],
                service_name,
                "config",
                "edge_set",
                {"timeout_ms": target_timeout},
                callee_name,
            )
        if len(times) >= 2:
            schedule_spec(
                times[1],
                service_name,
                "config",
                "edge_set",
                {"pool_size": target_pool},
                callee_name,
            )
        if len(times) >= 3:
            schedule_spec(
                times[2],
                service_name,
                "config",
                "edge_set",
                {"retries": 1.0},
                callee_name,
            )

    for sc in sampled_safe_callers:
        if sc.dependencies:
            callee = sc.dependencies[0]
            t_list = sorted(rng.sample(range(-20, -2), 3))
            apply_safe_edge_specs(sc.name, callee, t_list)

    if culprit.dependencies:
        callee0 = culprit.dependencies[0]
        if fault == "config_push":
            schedule_spec(
                -20,
                culprit.name,
                "config",
                "edge_set",
                {"timeout_ms": round(peak_lats[callee0] * 1000 * 1.65, 1)},
                callee0,
            )
            schedule_spec(
                -16,
                culprit.name,
                "config",
                "edge_set",
                {
                    "pool_size": float(
                        max(12, math.ceil(peak_concs[(culprit.name, callee0)] * 1.55))
                    )
                },
                callee0,
            )
            schedule_spec(
                -2,
                culprit.name,
                "config",
                "edge_set",
                {
                    "pool_size": float(
                        max(12, math.ceil(peak_concs[(culprit.name, callee0)] * 1.40))
                    )
                },
                callee0,
            )
            if len(culprit.dependencies) > 1:
                schedule_spec(
                    1,
                    culprit.name,
                    "config",
                    "edge_set",
                    {"retries": 1.0},
                    culprit.dependencies[1],
                )
        elif fault == "pool_exhaustion":
            schedule_spec(
                -20,
                culprit.name,
                "config",
                "edge_set",
                {
                    "pool_size": float(
                        max(12, math.ceil(peak_concs[(culprit.name, callee0)] * 1.55))
                    )
                },
                callee0,
            )
            schedule_spec(
                -16,
                culprit.name,
                "config",
                "edge_set",
                {"timeout_ms": round(peak_lats[callee0] * 1000 * 1.65, 1)},
                callee0,
            )
            schedule_spec(
                -2,
                culprit.name,
                "config",
                "edge_set",
                {"timeout_ms": round(peak_lats[callee0] * 1000 * 1.50, 1)},
                callee0,
            )
            schedule_spec(
                1,
                culprit.name,
                "config",
                "edge_set",
                {"retries": 1.0},
                callee0,
            )
        elif fault == "retry_storm":
            schedule_spec(
                -20,
                culprit.name,
                "config",
                "edge_set",
                {"timeout_ms": round(peak_lats[callee0] * 1000 * 1.70, 1)},
                callee0,
            )
            schedule_spec(
                -17,
                culprit.name,
                "config",
                "edge_set",
                {
                    "pool_size": float(
                        max(12, math.ceil(peak_concs[(culprit.name, callee0)] * 1.50))
                    )
                },
                callee0,
            )
            schedule_spec(
                -15,
                culprit.name,
                "config",
                "edge_set",
                {"retries": 1.0},
                callee0,
            )
            if len(culprit.dependencies) > 1:
                schedule_spec(
                    1,
                    culprit.name,
                    "config",
                    "edge_set",
                    {"timeout_ms": round(peak_lats[culprit.dependencies[1]] * 1000 * 1.55, 1)},
                    culprit.dependencies[1],
                )
        else:
            schedule_spec(
                -18,
                culprit.name,
                "config",
                "edge_set",
                {"timeout_ms": round(peak_lats[callee0] * 1000 * 1.60, 1)},
                callee0,
            )
            schedule_spec(
                -12,
                culprit.name,
                "config",
                "edge_set",
                {
                    "pool_size": float(
                        max(12, math.ceil(peak_concs[(culprit.name, callee0)] * 1.45))
                    )
                },
                callee0,
            )
            schedule_spec(
                1,
                culprit.name,
                "config",
                "edge_set",
                {"retries": 1.0},
                callee0,
            )

    capacity_change_services = [
        s.name
        for s in services_list
        if s.name != "edge-gateway"
        and s.name != "session-cache"
        and s.name not in reachable_from_culprit
    ]
    if fault == "queue_backlog":
        capacity_change_services = [s for s in capacity_change_services if s != culprit.name]
    elif fault in ("config_push", "retry_storm") and culprit.dependencies:
        callee0 = culprit.dependencies[0]
        capacity_change_services = [s for s in capacity_change_services if s != callee0]

    sampled_cap_services = rng.sample(
        capacity_change_services, min(5, len(capacity_change_services))
    )
    if (
        culprit.name not in sampled_cap_services
        and culprit.name not in reachable_from_culprit
        and fault != "queue_backlog"
    ):
        if culprit.name != "edge-gateway" and culprit.name != "session-cache":
            sampled_cap_services.append(culprit.name)

    for s_name in sampled_cap_services:
        schedule_spec(rng.randint(-4, 0), s_name, "config", "cap_bump", {"mult": 1.05})
        schedule_spec(rng.randint(1, 4), s_name, "config", "cap_bump", {"mult": 1.10})

    late_deploys_services = rng.sample(
        [s.name for s in services_list if s.name != culprit.name],
        min(5, len(services_list) - 1),
    )
    for s_name in late_deploys_services:
        schedule_spec(rng.randint(-2, 2), s_name, "deploy", "deploy", {}, None)

    if fault in ("config_push", "pool_exhaustion", "retry_storm"):
        schedule_spec(rng.choice((0, 1, 2)), culprit.name, "deploy", "deploy", {}, None)
    else:
        schedule_spec(1, culprit.name, "config", "cap_bump", {"mult": 1.02})
        schedule_spec(3, culprit.name, "config", "cap_bump", {"mult": 1.04})

    callee0 = culprit.dependencies[0] if culprit.dependencies else None
    kind = "config" if fault in ("config_push", "pool_exhaustion", "retry_storm") else "deploy"

    trigger_fault_minute = 0
    trigger_spec_type = ""
    trigger_payload: dict[str, float] = {}
    trigger_callee: str | None = None

    if fault == "config_push":
        assert callee0 is not None
        trigger_fault_minute = rng.randint(-10, -8)
        f_scale = 0.55 + 0.45 * math.exp(-((trigger_fault_minute / 11.0) ** 2))
        lat_at_f, _ = compute_lat_and_conc(f_scale)
        lat_at_t, _ = compute_lat_and_conc(0.97)
        l_fault = lat_at_f[callee0]
        l_trough = lat_at_t[callee0]
        if l_fault * 1.06 < l_trough * 0.82:
            timeout_val = round(0.5 * (l_fault * 1.06 + l_trough * 0.82) * 1000, 1)
        else:
            trigger_fault_minute = rng.randint(-3, -1)
            timeout_val = round(0.58 * l_trough * 1000, 1)
        trigger_spec_type = "edge_set"
        trigger_payload = {"timeout_ms": timeout_val}
        trigger_callee = callee0
    elif fault == "pool_exhaustion":
        assert callee0 is not None
        trigger_fault_minute = rng.randint(-11, -7)
        _, conc_at_t_dict = compute_lat_and_conc(0.97)
        c_trough = conc_at_t_dict[(culprit.name, callee0)]
        # This is fluid connection capacity: an absolute two-connection floor
        # can exceed all offered concurrency on a lightly loaded edge.
        pool_target = max(0.01, round(0.45 * c_trough, 4))
        trigger_spec_type = "edge_set"
        trigger_payload = {"pool_size": pool_target}
        trigger_callee = callee0
    elif fault == "retry_storm":
        assert callee0 is not None
        trigger_fault_minute = rng.randint(-10, -8)
        f_scale = 0.55 + 0.45 * math.exp(-((trigger_fault_minute / 11.0) ** 2))
        lat_at_f, _ = compute_lat_and_conc(f_scale)
        lat_at_t, _ = compute_lat_and_conc(0.97)
        l_fault = lat_at_f[callee0]
        l_trough = lat_at_t[callee0]
        if l_fault * 1.06 < l_trough * 0.84:
            timeout_ms = round(0.5 * (l_fault * 1.06 + l_trough * 0.84) * 1000, 1)
        else:
            trigger_fault_minute = rng.randint(-3, -1)
            timeout_ms = round(0.62 * l_trough * 1000, 1)
        trigger_spec_type = "edge_set"
        trigger_payload = {
            "retries": 5.0,
            "timeout_ms": timeout_ms,
            "pool_size": 2048.0,
        }
        trigger_callee = callee0
    elif fault == "partial_rollout":
        trigger_fault_minute = rng.randint(-4, -1)
        trigger_spec_type = "deploy"
        trigger_payload = {}
    elif fault == "memory_leak":
        trigger_fault_minute = rng.randint(-21, -19)
        trigger_spec_type = "deploy"
        trigger_payload = {}
    elif fault == "queue_backlog":
        trigger_fault_minute = rng.randint(-5, -3)
        trigger_spec_type = "deploy"
        trigger_payload = {}

    trigger_id = schedule_spec(
        trigger_fault_minute,
        culprit.name,
        kind,
        trigger_spec_type,
        trigger_payload,
        trigger_callee,
        is_trigger=True,
    )

    raw_specs.sort(key=lambda x: (x[0], x[1]))

    current_service_params = {
        s.name: {
            "capacity": s.capacity,
            "version": float(s.version),
        }
        for s in services_list
    }
    current_edge_params = {
        (e.caller, e.callee): {
            "timeout_ms": e.timeout_ms,
            "pool_size": e.pool_size,
            "retries": e.retries,
        }
        for e in edges_tuple
    }

    changes: list[Change] = []
    trigger_version = 0.0

    for (
        minute,
        identifier,
        service,
        kind_val,
        spec_type,
        payload,
        callee,
        author,
        is_trigger,
    ) in raw_specs:
        before: dict[str, float] = {}
        after: dict[str, float] = {}

        if spec_type == "deploy":
            cur_v = current_service_params[service]["version"]
            before = {"version": cur_v}
            after = {"version": cur_v + 1.0}
            current_service_params[service]["version"] = cur_v + 1.0
            if is_trigger:
                trigger_version = cur_v + 1.0
        elif spec_type == "cap_bump":
            mult = float(payload["mult"])
            cur_c = current_service_params[service]["capacity"]
            new_c = round(cur_c * mult, 2)
            before = {"capacity": cur_c}
            after = {"capacity": new_c}
            current_service_params[service]["capacity"] = new_c
        elif spec_type == "edge_set":
            assert callee is not None
            e_key = (service, callee)
            before = {k: current_edge_params[e_key][k] for k in payload}
            after = {k: float(v) for k, v in payload.items()}
            current_edge_params[e_key].update(after)

        summary = "; ".join(f"{k}: {before[k]:g} -> {after[k]:g}" for k in after)
        changes.append(
            Change(
                id=identifier,
                minute=minute,
                service=service,
                kind=kind_val,
                summary=summary,
                before=before,
                after=after,
                callee=callee,
                author=author,
            )
        )

    builds: tuple[Build, ...] = ()
    if fault == "partial_rollout":
        builds = (
            Build(
                culprit.name,
                int(trigger_version),
                pod_error=0.6,
                bad_pods=1,
            ),
        )
    elif fault == "memory_leak":
        builds = (
            Build(
                culprit.name,
                int(trigger_version),
                bad_pods=1,
                leak_mib_min=44.0,
            ),
        )
    elif fault == "queue_backlog":
        # The capacity floor can leave a lightly loaded worker healthy even at
        # 30% throughput. Bound faulty processing by 65% of the lowest offered
        # demand, including the later capacity increases in the denominator.
        healthy_capacity = current_service_params[culprit.name]["capacity"] * culprit.replicas
        consumer_fraction = min(0.30, 0.65 * 0.97 * base_flow[culprit.name] / healthy_capacity)
        builds = (
            Build(
                culprit.name,
                int(trigger_version),
                consumer_running=consumer_fraction,
            ),
        )

    return World(
        services=tuple(services_list),
        changes=tuple(changes),
        culprit=culprit.name,
        fault=fault,
        change_id=trigger_id,
        fingerprint=digest,
        edges=edges_tuple,
        entry_rate=entry_rate,
        builds=builds,
    )

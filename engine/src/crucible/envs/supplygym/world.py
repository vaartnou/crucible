# ruff: noqa
# pyright: basic, reportAssignmentType=false
"""Static supplier graph (the "world") and the per-episode exogenous tape.

Everything random is drawn from named, independent PCG64 streams keyed by the episode seed.
Agents never consume randomness, so every agent (and the oracle) faces the identical crisis timeline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

REGIONS = ["NA", "EU", "APAC-N", "APAC-S", "LATAM"]
REGION_XY = {
    "NA": (16, 36),
    "EU": (50, 24),
    "APAC-N": (84, 30),
    "APAC-S": (80, 68),
    "LATAM": (30, 74),
}
HQ_XY = (
    50,
    52,
)  # synthetic company location (the project's real map is NOT provided; this is a stand-in)
EVENT_TYPES = ["strike", "port_closure", "fire", "sanction", "bankruptcy"]
N_DIRECT, N_ALT, N_T2, N_T3 = 10, 2, 12, 8
N_NODES = N_DIRECT + N_ALT + N_T2 + N_T3
HORIZON = 365
LOOKAHEAD = 90  # tape is generated past the horizon so oracles can look ahead
GAMMA_PROP = 0.8  # share of a parent's lost output that propagates to a child

# demand / economics (single SKU; lost sales)
DEMAND_MEAN = 100.0
PRICE_SELL = 15.0
HOLD_COST = 0.2
STOCKOUT_PEN = 10.0
EXPEDITE_COST = 3.0
SWITCH_COST = 2000.0
ONBOARD_DAYS = 14

_NAME_A = [
    "Kes",
    "Nor",
    "Vel",
    "Ar",
    "Zan",
    "Bol",
    "Cor",
    "Dun",
    "Eld",
    "Fer",
    "Gal",
    "Hal",
    "Ish",
    "Jor",
    "Kal",
    "Lum",
]
_NAME_B = ["trel", "dex", "mar", "tor", "vin", "sco", "lan", "pex", "rik", "sol", "tan", "wyn"]
_NAME_C = [
    "Components",
    "Metals",
    "Plastics",
    "Textiles",
    "Chemicals",
    "Electronics",
    "Logistics",
    "Foundry",
]


@dataclass
class World:
    seed: int
    nodes: list  # list of dict
    parents: dict  # node idx -> list[(parent idx, weight)]

    @property
    def n(self):
        return len(self.nodes)

    def region_nodes(self, region):
        return [i for i, n in enumerate(self.nodes) if n["region"] == region]

    def public(self):
        """What an agent may know: direct + alternative suppliers only (tier 2/3 stay hidden)."""
        keep = ("id", "name", "tier", "kind", "region", "x", "y", "cap", "lead", "price")
        return [{k: n[k] for k in keep} for n in self.nodes[: N_DIRECT + N_ALT]]


def make_world(seed: int = 7, price_lo: float = 9.6, price_hi: float = 10.4) -> World:
    rng = np.random.default_rng(np.random.SeedSequence([seed, 101]))
    nodes = []
    names = set()

    def name():
        while True:
            s = rng.choice(_NAME_A) + rng.choice(_NAME_B) + " " + rng.choice(_NAME_C)
            if s not in names:
                names.add(s)
                return s

    def add(kind, tier, region, cap, lead, price, base_fail, idprefix, k):
        x0, y0 = REGION_XY[region]
        nodes.append(
            dict(
                id=f"{idprefix}{k:02d}",
                name=name(),
                tier=tier,
                kind=kind,
                region=region,
                x=round(float(x0 + rng.uniform(-9, 9)), 1),
                y=round(float(y0 + rng.uniform(-9, 9)), 1),
                cap=int(cap),
                lead=int(lead),
                price=round(float(price), 2),
                base_fail=round(float(base_fail), 3),
                port=bool(rng.random() < 0.6),
            )
        )

    # direct suppliers: 2 per region, deliberately spread; capacity sums to ~300 vs demand 100
    for k in range(N_DIRECT):
        reg = REGIONS[k % 5]
        add(
            "direct",
            1,
            reg,
            rng.integers(22, 40),
            rng.integers(4, 21),
            rng.uniform(price_lo, price_hi),
            rng.uniform(0.02, 0.1),
            "S",
            k + 1,
        )
    for k in range(N_ALT):  # alternative suppliers: pricier, needs onboarding, other regions
        reg = REGIONS[(k * 2 + 1) % 5]
        add(
            "alt",
            1,
            reg,
            rng.integers(28, 40),
            rng.integers(6, 14),
            rng.uniform(12, 13),
            rng.uniform(0.02, 0.06),
            "A",
            k + 1,
        )
    for k in range(N_T2):
        add(
            "tier2",
            2,
            REGIONS[int(rng.integers(0, 5))],
            0,
            0,
            0,
            rng.uniform(0.03, 0.15),
            "T2-",
            k + 1,
        )
    for k in range(N_T3):
        add(
            "tier3",
            3,
            REGIONS[int(rng.integers(0, 5))],
            0,
            0,
            0,
            rng.uniform(0.03, 0.15),
            "T3-",
            k + 1,
        )
    t2 = list(range(N_DIRECT + N_ALT, N_DIRECT + N_ALT + N_T2))
    t3 = list(range(N_DIRECT + N_ALT + N_T2, N_NODES))
    parents = {}

    def wts(m):
        w = rng.uniform(0.4, 1.0, size=m)
        return w / w.sum()

    for i in range(N_DIRECT + N_ALT):
        m = int(rng.integers(1, 4))
        par = list(rng.choice(t2, size=m, replace=False))
        parents[i] = list(zip(map(int, par), map(float, wts(m))))
    # concentration risk: two tier-2 nodes feed >= 4 direct suppliers each
    for hub in (t2[0], t2[1]):
        for i in rng.choice(N_DIRECT, size=4, replace=False):
            if hub not in [p for p, _ in parents[int(i)]]:
                par = [p for p, _ in parents[int(i)]][:2] + [hub]
                w = wts(len(par))
                parents[int(i)] = list(zip(map(int, par), map(float, w)))
    for j in t2:
        m = int(rng.integers(1, 3))
        par = list(rng.choice(t3, size=m, replace=False))
        parents[j] = list(zip(map(int, par), map(float, wts(m))))
    return World(seed, nodes, parents)


_TEMPLATES = {
    "strike": [
        "Workers at {w} walk out over wage dispute",
        "Union announces strike action affecting {w}",
        "Labor action disrupts output at {w}",
    ],
    "port_closure": [
        "Port congestion and closure reported near {w}",
        "Authorities shut harbor serving {w}",
        "Shipping halted at port handling cargo for {w}",
    ],
    "fire": [
        "Fire reported at facility of {w}",
        "Blaze forces evacuation at {w}",
        "Plant of {w} damaged in fire",
    ],
    "sanction": [
        "New sanctions announced affecting firms in {w}",
        "Trade restrictions imposed on suppliers in {w}",
        "Export ban could hit companies in {w}",
    ],
    "bankruptcy": [
        "{w} reportedly files for insolvency",
        "Creditors move against {w}",
        "{w} halts payments, administrators called in",
    ],
}
_RUMOR = [
    "Unconfirmed: sources say {t} may be looming at {w}",
    "Chatter on trade forums about a possible {t} at {w}",
    "Reportedly, {t} risk rising around {w}, not verified",
]
_TYPE_WORD = {
    "strike": "strike",
    "port_closure": "port closure",
    "fire": "fire",
    "sanction": "sanctions",
    "bankruptcy": "bankruptcy",
}


@dataclass
class Tape:
    seed: int
    rate_scale: float
    events: list  # hidden truth
    rumors: list  # rumor items; outcome is truth
    news: list  # real-event news + false alarms (public items)
    demand: np.ndarray
    H: np.ndarray  # (T+LOOKAHEAD, N_NODES) health in [0,1]  -- hidden truth
    risk_noise: np.ndarray
    risk_stale: np.ndarray
    lateness_noise: np.ndarray = None


def _health(world: World, S: np.ndarray) -> np.ndarray:
    H = 1.0 - S
    for lvl in (range(N_DIRECT + N_ALT, N_DIRECT + N_ALT + N_T2), range(N_DIRECT + N_ALT)):
        for i in lvl:
            loss = np.zeros(S.shape[0])
            for p, w in world.parents[i]:
                loss += w * (1.0 - H[:, p])
            H[:, i] = (1.0 - S[:, i]) * np.clip(1.0 - GAMMA_PROP * loss, 0.0, 1.0)
    return H


def effective_seed(seed: int, salt: str = "") -> int:
    """Seed actually fed to the tape. An empty salt keeps the plain seed (public tasks, offline eval);
    a per-task salt that only the sidecar knows makes the tape unrecoverable from the public engine code."""
    if not salt:
        return int(seed)
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def make_tape(
    world: World,
    seed: int,
    rate_scale: float = 1.0,
    horizon: int = HORIZON,
    salt: str = "",
    *,
    demand_shocks: bool = False,
) -> Tape:
    ss = np.random.SeedSequence([effective_seed(seed, salt), 202])
    r_ev, r_dem, r_news, r_fa, r_rum, r_risk = [
        np.random.Generator(np.random.PCG64(s)) for s in ss.spawn(6)
    ]
    T = horizon + LOOKAHEAD
    events = []

    def add_event(typ, nodes, region, sev, dmin, dmax, start, node=None):
        dur = int(r_ev.integers(dmin, dmax + 1))
        events.append(
            dict(
                id=len(events),
                type=typ,
                region=region,
                node=node,
                nodes=[int(n) for n in nodes],
                sev=sev,
                start=int(start),
                end=int(start + dur),
            )
        )

    def poisson_starts(rate_per_year):
        n = r_ev.poisson(rate_per_year * rate_scale * horizon / 365.0)
        return sorted(int(r_ev.integers(0, horizon)) for _ in range(n))

    for reg in REGIONS:
        rn = world.region_nodes(reg)
        for s in poisson_starts(1.5):
            add_event("strike", rn, reg, 0.8, 7, 21, s)
        pn = [i for i in rn if world.nodes[i]["port"]]
        for s in poisson_starts(0.7):
            if pn:
                add_event("port_closure", pn, reg, 1.0, 5, 15, s)
        sn = [i for i in rn if world.nodes[i]["tier"] <= 2]
        for s in poisson_starts(0.2):
            add_event("sanction", sn, reg, 1.0, 45, 90, s)
    for i, nd in enumerate(world.nodes):
        for s in poisson_starts(1.5 * nd["base_fail"]):
            add_event("fire", [i], nd["region"], 1.0, 30, 60, s, node=i)
        if nd["kind"] in ("direct", "tier2"):
            for s in poisson_starts(0.04):
                add_event("bankruptcy", [i], nd["region"], 1.0, 150, 365, s, node=i)
    events.sort(key=lambda e: (e["start"], e["id"]))
    for k, e in enumerate(events):
        e["id"] = k
    S = np.zeros((T, N_NODES))
    for e in events:
        a, b = e["start"], min(e["end"], T)
        for n in e["nodes"]:
            S[a:b, n] = np.maximum(S[a:b, n], e["sev"])
    H = _health(world, S)

    def where(region, node):
        return world.nodes[node]["name"] if node is not None else region

    news = []
    p_news = {"strike": 0.7, "port_closure": 0.7, "fire": 0.6, "sanction": 0.7, "bankruptcy": 0.5}
    for e in events:
        if r_news.random() < p_news[e["type"]]:
            day = e["start"] + int(r_news.integers(0, 3))
            if day >= horizon:
                continue
            typ = e["type"] if r_news.random() < 0.8 else str(r_news.choice(EVENT_TYPES))
            reg = e["region"] if r_news.random() < 0.9 else str(r_news.choice(REGIONS))
            node = e["node"] if (e["node"] is not None and r_news.random() < 0.4) else None
            sev = "major" if e["sev"] >= 1.0 else "minor"
            if r_news.random() > 0.75:
                sev = "minor" if sev == "major" else "major"
            tpl = _TEMPLATES[typ][int(r_news.integers(0, 3))]
            news.append(
                dict(
                    day=day,
                    kind="news",
                    type=typ,
                    region=reg,
                    node=node,
                    severity=sev,
                    text=tpl.format(w=where(reg, node)),
                    _event=e["id"],
                )
            )
    n_fa = r_fa.poisson(0.025 * horizon)
    for _ in range(n_fa):
        day = int(r_fa.integers(0, horizon))
        typ = str(r_fa.choice(EVENT_TYPES))
        reg = str(r_fa.choice(REGIONS))
        node = int(r_fa.integers(0, N_DIRECT + N_ALT)) if r_fa.random() < 0.4 else None
        if node is not None:
            reg = world.nodes[node]["region"]
        tpl = _TEMPLATES[typ][int(r_fa.integers(0, 3))]
        news.append(
            dict(
                day=day,
                kind="news",
                type=typ,
                region=reg,
                node=node,
                severity=str(r_fa.choice(["minor", "major"])),
                text=tpl.format(w=where(reg, node)),
                _event=None,
            )
        )
    news.sort(key=lambda x: x["day"])

    rumors = []

    def rumor_text(typ, reg, node):
        return _RUMOR[int(r_rum.integers(0, 3))].format(t=_TYPE_WORD[typ], w=where(reg, node))

    for e in events:  # rumors that precede a real event (it later resolves "real")
        if e["start"] >= 2 and r_rum.random() < 0.5:
            day = max(0, e["start"] - int(r_rum.integers(1, 11)))
            node = e["node"] if (e["node"] is not None and r_rum.random() < 0.5) else None
            cred = "medium" if r_rum.random() < 0.6 else "low"
            rumors.append(
                dict(
                    day=day,
                    kind="rumor",
                    type=e["type"],
                    region=e["region"],
                    node=node,
                    credibility=cred,
                    resolve_day=e["start"],
                    outcome="real",
                    text=rumor_text(e["type"], e["region"], node),
                    _event=e["id"],
                )
            )
    for _ in range(r_rum.poisson(4.0)):  # rumors that fizzle
        day = int(r_rum.integers(0, horizon))
        typ = str(r_rum.choice(EVENT_TYPES))
        reg = str(r_rum.choice(REGIONS))
        node = int(r_rum.integers(0, N_DIRECT + N_ALT)) if r_rum.random() < 0.4 else None
        if node is not None:
            reg = world.nodes[node]["region"]
        cred = "medium" if r_rum.random() < 0.3 else "low"
        rumors.append(
            dict(
                day=day,
                kind="rumor",
                type=typ,
                region=reg,
                node=node,
                credibility=cred,
                resolve_day=day + int(r_rum.integers(5, 13)),
                outcome="fizzled",
                text=rumor_text(typ, reg, node),
                _event=None,
            )
        )
    rumors.sort(key=lambda x: (x["day"], x["resolve_day"]))
    for k, r in enumerate(rumors):
        r["id"] = k

    demand = r_dem.poisson(DEMAND_MEAN, size=T).astype(np.int64)
    if demand_shocks:
        # A separate stream leaves all existing draws (including baseline demand) untouched.
        # One 2-4 week episode per 90-day window, alternating surges and dips.
        r_shock = np.random.default_rng([effective_seed(seed, salt), 203])
        surge = bool(r_shock.integers(0, 2))
        for window in range(0, horizon, 90):
            if window + 14 >= horizon:
                break
            start = int(r_shock.integers(window + 14, min(window + 60, horizon)))
            end = min(start + int(r_shock.integers(14, 29)), T)
            factor = 1.5 if surge else 0.65
            demand[start:end] = np.rint(demand[start:end] * factor).astype(np.int64)
            surge = not surge
    risk_noise = r_risk.normal(0, 0.12, size=(T, N_DIRECT + N_ALT))
    risk_stale = r_risk.random((T, N_DIRECT + N_ALT)) < 0.10
    return Tape(int(seed), rate_scale, events, rumors, news, demand, H, risk_noise, risk_stale)


def tape_digest(t: Tape) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(t.demand.tobytes())
    h.update(np.round(t.H, 6).tobytes())
    for e in t.events:
        h.update(repr((e["type"], e["nodes"], e["start"], e["end"])).encode())
    for n in t.news:
        h.update(n["text"].encode())
    return h.hexdigest()[:16]

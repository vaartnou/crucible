# ruff: noqa
# pyright: basic, reportOperatorIssue=false, reportOptionalMemberAccess=false
"""SupplyGym simulator (numpy only), ported from the standalone SupplyGym gym.

One step = `decision_interval` days (default 7). The action is a *standing control* held for the whole interval:
orders are placed automatically every day from it (so "reorder / split / safety stock / expedite / approve alt / wait" are all
expressible, and an LLM may act weekly without being starved of daily ordering).

Canonical action, Box(17) in [-1, 1]  (zeros == classic (s,S) policy):
  a[0:12]  supplier share logits (10 direct + 2 alternatives; alternatives only usable once onboarded)
  a[12]    order multiplier exp(0.5*a)            a[13] safety-stock days 3*max(0,a)
  a[14]    expedite fraction 0.3*max(0,a)           a[15] >0.5: approve next alternative
  a[16]    >0.5: pass-through reorder mode (replace expected demand each day; a[13] = on-hand target in days). a[13]<=0 => pure 'do nothing'
"""

from __future__ import annotations

import math

import numpy as np

from crucible.envs.supplygym.world import *  # noqa: F403
from crucible.envs.supplygym.world import _TYPE_WORD

ACT_DIM = 17
SHARE_SCALE = 4.0
ACK_DAYS = 7  # supplier acknowledges the shipped quantity this many days after ordering (shortfall becomes visible)
REVIEW = 3  # review period R (days) in the base-stock level
Z = 1.65
TERMINAL_VALUE = (
    10.0  # stock on hand and already paid for in transit at the horizon is credited at about cost
)
GAP_DAYS = 0.5  # order-up-to level S sits this many days of demand above the reorder point s


def _sig(x):
    return 1.0 / (1.0 + math.exp(-x))


class SupplyEnv:
    """Plain-numpy simulator (the Gymnasium shell of the original SupplyGym is dropped)."""

    def __init__(
        self,
        world_seed: int = 7,
        seed: int | None = None,
        rate_scale: float = 1.0,
        decision_interval: int = 7,
        signals: str = "full",
        horizon: int = HORIZON,
        record: bool = False,
        truth_in_info: bool = False,
        salt: str = "",
        price_lo: float = 9.6,
        price_hi: float = 10.4,
        world: World | None = None,
    ):
        assert signals in ("full", "no_news", "none", "placebo")
        self.world = world if world is not None else make_world(world_seed, price_lo, price_hi)
        self.salt = salt
        self.rate_scale, self.k, self.signals, self.horizon = (
            rate_scale,
            decision_interval,
            signals,
            horizon,
        )
        self.record, self.truth_in_info = record, truth_in_info
        self._default_seed = seed
        self.NS = N_DIRECT + N_ALT
        self.obs_dim = self.NS * 7 + len(REGIONS) * 4 + 8
        self.cap = np.array([n["cap"] for n in self.world.nodes[: self.NS]], float)
        self.lead = np.array([n["lead"] for n in self.world.nodes[: self.NS]], int)
        self.price = np.array([n["price"] for n in self.world.nodes[: self.NS]], float)
        self.reg_idx = {r: i for i, r in enumerate(REGIONS)}
        self.sup_region = [self.reg_idx[n["region"]] for n in self.world.nodes[: self.NS]]
        self.tape = None

    # ------------------------------------------------------------------ reset
    def reset(self, *, seed=None, options=None):
        s = seed if seed is not None else self._default_seed
        if s is None:
            raise ValueError("seed required")
        self.seed_ = int(s)
        self.tape = make_tape(self.world, self.seed_, self.rate_scale, self.horizon, self.salt)
        self._signal_view()
        self.day = 0
        self.onhand = float(7 * DEMAND_MEAN)
        self.orders = []  # dicts: sup, arr, nom, qty, ordered
        self.approved = {}  # alt supplier idx -> ready day
        self.risk = np.full(self.NS, 0.15)
        self.fill = np.ones(self.NS)
        self.news_ewma = np.zeros(len(REGIONS))
        self.last_news = np.full(len(REGIONS), -60.0)
        self.rum_active = []  # rumors visible & unresolved: dict
        self.ctrl = np.zeros(ACT_DIM)
        self.tot = dict(
            profit=0.0,
            revenue=0.0,
            purchase=0.0,
            holding=0.0,
            expedite=0.0,
            switching=0.0,
            stockout=0.0,
            demand=0,
            sales=0,
            lost=0,
        )
        self._terminal_done = False
        self.decisions, self.days_log = [], []
        self._new_text = []
        return self._obs(), self._info([])

    def _signal_view(self):
        """Apply the signal ablation arm to the PUBLIC view of the tape (events themselves are never changed)."""
        t = self.tape
        mode = self.signals
        news, rum = t.news, t.rumors
        if mode in ("no_news", "none"):
            news, rum = [], []
        elif mode == "placebo":
            rng = np.random.default_rng(np.random.SeedSequence([self.seed_, 303]))

            def shuffle(items):
                out = []
                for it in items:
                    it = dict(it)
                    typ = str(rng.choice(EVENT_TYPES))
                    reg = str(rng.choice(REGIONS))
                    it.update(
                        type=typ,
                        region=reg,
                        node=None,
                        text=it["text"].split(" at ")[0] + " in " + reg
                        if it["kind"] == "rumor"
                        else f"{typ.replace('_', ' ')} reported in {reg}",
                        _event=None,
                    )
                    out.append(it)
                return out

            news, rum = shuffle(news), shuffle(rum)
        self.pub_news, self.pub_rum = news, rum
        self.use_ops = mode != "none"

    # ------------------------------------------------------------------ helpers
    def active(self):
        return [i for i in range(N_DIRECT)] + [
            i for i, rd in self.approved.items() if self.day >= rd
        ]

    def _base_w(self, act):
        w = np.zeros(self.NS)
        w[act] = self.cap[act]
        return w / w.sum()

    def _pipeline(self):
        # the buyer believes the full ORDERED quantity is coming until the nominal ETA passes; afterwards only what really ships
        d = self.day
        return sum(o["ordered"] if d < o["rev"] else o["qty"] for o in self.orders)

    def lbar(self, act):
        bw = self._base_w(act)
        return float((bw * self.lead).sum())

    def levels(self, act, ss_days):
        L = self.lbar(act) + REVIEW
        s_lvl = DEMAND_MEAN * L + Z * math.sqrt(DEMAND_MEAN) * math.sqrt(L) + ss_days * DEMAND_MEAN
        return s_lvl, s_lvl + DEMAND_MEAN * GAP_DAYS

    def parse(self, a):
        a = np.clip(np.asarray(a, float).reshape(-1), -1, 1)
        if a.size < ACT_DIM:
            a = np.concatenate([a, -np.ones(ACT_DIM - a.size)])
        return a

    # ------------------------------------------------------------------ daily dynamics
    def regular_receipt(self, supplier, nominal, delay):
        return nominal, nominal + delay

    def reported_arrival(self, order):
        return order["arr"]

    def _place_orders(self, ctrl, day_costs):
        act = self.active()
        bw = self._base_w(act)
        d = self.day
        if ctrl[16] > 0.5:
            ss_days = 3.0 * max(0.0, ctrl[13])
            Q = DEMAND_MEAN + max(0.0, ss_days * DEMAND_MEAN - self.onhand) / 3.0
            Q = min(Q, 3 * DEMAND_MEAN)
            logits = np.log(bw[act]) + SHARE_SCALE * ctrl[: self.NS][act]
            e = np.exp(logits - logits.max())
            shares = e / e.sum()
            exp_f = 0.3 * max(0.0, ctrl[14])
        else:
            ss_days = 3.0 * max(0.0, ctrl[13])
            s_lvl, S_up = self.levels(act, ss_days)
            ip = self.onhand + self._pipeline()
            Q = (S_up - ip) if ip <= s_lvl else 0.0
            Q = max(0.0, Q) * math.exp(0.5 * ctrl[12])
            Q = min(Q, 3 * DEMAND_MEAN)
            logits = np.log(bw[act]) + SHARE_SCALE * ctrl[: self.NS][act]
            e = np.exp(logits - logits.max())
            shares = e / e.sum()
            exp_f = 0.3 * max(0.0, ctrl[14])
        if Q < 0.5:
            return
        H = self.tape.H[d]
        for j, i in enumerate(act):
            q = int(round(Q * shares[j]))
            if q <= 0:
                continue
            h = H[i]
            shipped = min(q, int(math.floor(self.cap[i] * h)))
            nominal = d + int(self.lead[i])
            delay = int(math.ceil((1 - h) * self.lead[i] * 0.5)) if h < 0.8 else 0
            qe = int(round(exp_f * shipped))
            qn = shipped - qe
            cost = self.price[i] * shipped
            day_costs["purchase"] += cost
            if qe:
                day_costs["expedite"] += EXPEDITE_COST * qe
                self.orders.append(
                    dict(
                        sup=int(i),
                        arr=d + int(math.ceil(self.lead[i] / 2)) + delay // 2,
                        nom=d + int(math.ceil(self.lead[i] / 2)),
                        rev=d,
                        qty=qe,
                        ordered=0,
                    )
                )
            booked, arrival = self.regular_receipt(i, nominal, delay)
            self.orders.append(
                dict(
                    sup=int(i),
                    arr=arrival,
                    nom=booked,
                    rev=min(nominal, d + ACK_DAYS),
                    qty=qn,
                    ordered=q - qe if qe else q,
                )
            )
            if self.record:
                day_costs["_orders"].append((int(i), q, shipped, qe))

    def _day(self, ctrl):
        d, t = self.day, self.tape
        c = dict(purchase=0.0, expedite=0.0, switching=0.0, _orders=[])
        self._place_orders(ctrl, c)
        arrived = 0
        keep = []
        if self.use_ops:
            for o in self.orders:
                if o["rev"] == d and o["ordered"] > 0:
                    self.fill[o["sup"]] = 0.6 * self.fill[o["sup"]] + 0.4 * min(
                        1.0, o["qty"] / o["ordered"]
                    )
        for o in self.orders:
            if o["arr"] <= d:
                self.onhand += o["qty"]
                arrived += o["qty"]
            else:
                keep.append(o)
        self.orders = keep
        # fill-rate tracking at the *nominal* ETA (delayed signal): evaluate against what was shipped
        dem = int(t.demand[d])
        sales = min(dem, int(self.onhand))
        lost = dem - sales
        self.onhand -= sales
        rev = PRICE_SELL * sales
        hold = HOLD_COST * self.onhand
        pen = STOCKOUT_PEN * lost
        profit = rev - c["purchase"] - c["expedite"] - hold - pen
        T = self.tot
        T["profit"] += profit
        T["revenue"] += rev
        T["purchase"] += c["purchase"]
        T["holding"] += hold
        T["expedite"] += c["expedite"]
        T["stockout"] += pen
        T["demand"] += dem
        T["sales"] += sales
        T["lost"] += lost
        if self.record:
            self.days_log.append(
                dict(
                    t=d,
                    onhand=round(self.onhand, 1),
                    demand=dem,
                    sales=sales,
                    profit=round(profit, 2),
                    arrived=arrived,
                    orders=c["_orders"],
                )
            )
        return profit

    def _update_signals(self):
        """Signals that become visible at the end of day self.day (before increment). Returns new text items."""
        d, t = self.day, self.tape
        new = []
        for it in self.pub_news:
            if it["day"] == d:
                new.append({k: v for k, v in it.items() if not k.startswith("_")})
                ri = self.reg_idx[it["region"]]
                self.news_ewma[ri] = min(
                    3.0, self.news_ewma[ri] + (1.0 if it["severity"] == "major" else 0.6)
                )
                self.last_news[ri] = d
        for r in self.pub_rum:
            if r["day"] == d:
                self.rum_active.append(r)
                new.append(
                    {
                        k: v
                        for k, v in r.items()
                        if k not in ("outcome", "resolve_day") and not k.startswith("_")
                    }
                )
            if r["resolve_day"] == d and r["outcome"] == "fizzled":
                new.append(
                    dict(
                        kind="update",
                        day=d,
                        region=r["region"],
                        node=r["node"],
                        text=f"Earlier reports of {_TYPE_WORD[r['type']]} around {r['region']} were not substantiated",
                    )
                )
        self.rum_active = [r for r in self.rum_active if r["resolve_day"] > d]
        self.news_ewma *= 0.9
        return new

    def _risk_update(self):
        d = self.day
        if not self.use_ops:
            return
        if d % 7 == 0:
            ref = max(0, d - 3)
            for i in range(self.NS):
                if self.tape.risk_stale[d, i]:
                    continue
                self.risk[i] = float(
                    np.clip(
                        0.15 + 0.7 * (1 - self.tape.H[ref, i]) + self.tape.risk_noise[d, i], 0, 1
                    )
                )

    # ------------------------------------------------------------------ step
    def step(self, action):
        a = self.parse(action)
        d0 = self.day
        # control + approvals (approval is an event, not standing)
        approved_now = None
        if a[15] > 0.5:
            for i in range(N_DIRECT, self.NS):
                if i not in self.approved:
                    self.approved[i] = d0 + ONBOARD_DAYS
                    approved_now = i
                    self.tot["switching"] += SWITCH_COST
                    self.tot["profit"] -= SWITCH_COST
                    break
        ctrl = a.copy()
        ctrl[15] = -1.0
        act = self.active()
        # mask logits for inactive alternatives is handled by active(); store effective shares for metrics
        bw = self._base_w(act)
        logits = np.log(bw[act]) + SHARE_SCALE * ctrl[: self.NS][act]
        e = np.exp(logits - logits.max())
        sh = np.zeros(self.NS)
        sh[act] = e / e.sum()
        self.decisions.append(
            dict(
                day=d0,
                action=[round(float(x), 5) for x in a],
                shares=[round(float(x), 4) for x in sh],
                base_shares=[round(float(x), 4) for x in bw],
                expedite=float(0.3 * max(0.0, a[14])),
                ss_days=float(3 * max(0, a[13])),
                naive=bool(a[16] > 0.5),
                approved=approved_now,
            )
        )
        self.ctrl = ctrl
        profit0 = self.tot["profit"]
        texts = []
        for _ in range(self.k):
            if self.day >= self.horizon:
                break
            self._day(self.ctrl)
            self._risk_update()
            # fill-rate: at nominal eta compare shipped vs ordered (delayed observation)
            texts += self._update_signals()
            self.day += 1
        truncated = self.day >= self.horizon
        if truncated and not self._terminal_done:
            self._terminal_done = True
            self.tot["profit"] += TERMINAL_VALUE * (
                self.onhand + sum(o["qty"] for o in self.orders)
            )
        r = float(np.clip((self.tot["profit"] - profit0) / (1500.0 * self.k), -5, 5))
        self._new_text = texts
        return self._obs(), r, False, truncated, self._info(texts)

    # ------------------------------------------------------------------ observation
    def _obs(self):
        d = self.day
        act = set(self.active())
        f = []
        dl = DEMAND_MEAN
        for i in range(self.NS):
            on = 1.0 if i in act else 0.0
            pend = [o for o in self.orders if o["sup"] == i]
            units = sum(o["ordered"] if d < o["rev"] else o["qty"] for o in pend) / (
                dl * max(1, self.lead[i]) / 3.0
            )
            nxt = min([self.reported_arrival(o) - d for o in pend], default=30) / 30.0
            late = sum(1 for o in pend if o["nom"] < d) / 5.0 if self.use_ops else 0.0
            risk = self.risk[i] if self.use_ops else 0.0
            # fill signal: share of nominal-ETA-passed orders still outstanding is exposed via `late`
            f += [
                on,
                units * on,
                nxt * on,
                risk * on,
                late * on,
                (self.lead[i] / 30.0),
                (self.fill[i] if self.use_ops else 1.0) * on,
            ]
        for r in range(len(REGIONS)):
            low = sum(
                1
                for x in self.rum_active
                if self.reg_idx[x["region"]] == r and x["credibility"] == "low"
            )
            med = sum(
                1
                for x in self.rum_active
                if self.reg_idx[x["region"]] == r and x["credibility"] == "medium"
            )
            f += [
                math.tanh(self.news_ewma[r]),
                min(60, d - self.last_news[r]) / 30.0,
                min(low, 3) / 3.0,
                min(med, 3) / 3.0,
            ]
        act_l = list(act)
        s_lvl, _ = self.levels(act_l, 0)
        ip = self.onhand + self._pipeline()
        f += [
            self.onhand / (7 * dl),
            ip / s_lvl,
            math.sin(2 * math.pi * d / 365),
            math.cos(2 * math.pi * d / 365),
            d / 365.0,
            len([i for i in self.approved if d >= self.approved[i]]) / 2.0,
            max(0.0, float(self.ctrl[13])),
            float(self.ctrl[12]),
        ]
        return np.clip(np.array(f, dtype=np.float32), -5, 5)

    def _info(self, texts):
        info = {
            "day": self.day,
            "news": [t for t in texts if t["kind"] == "news"],
            "rumors": [t for t in texts if t["kind"] == "rumor"],
            "updates": [t for t in texts if t["kind"] == "update"],
            "onhand": float(self.onhand),
            "inventory_position": float(self.onhand + self._pipeline()),
            "active_suppliers": self.active(),
            "profit_so_far": float(self.tot["profit"]),
        }
        if self.truth_in_info:
            info["truth"] = dict(events=self.tape.events)
        return info

    def public_info(self):
        return {
            "suppliers": self.world.public(),
            "demand_mean": DEMAND_MEAN,
            "price_sell": PRICE_SELL,
            "horizon": self.horizon,
            "decision_interval": self.k,
            "onboarding_days": ONBOARD_DAYS,
            "hq": HQ_XY,
        }

    def summary(self):
        T = self.tot
        return dict(
            profit=T["profit"],
            service=T["sales"] / max(1, T["demand"]),
            revenue=T["revenue"],
            purchase=T["purchase"],
            holding=T["holding"],
            expedite=T["expedite"],
            switching=T["switching"],
            stockout=T["stockout"],
            lost_units=T["lost"],
        )

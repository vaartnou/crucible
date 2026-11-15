"""Certified optimistic surplus bound for an initial Cala scenario."""

import heapq
import math
from typing import Any

from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.world import EXPEDITE_COST, HOLD_COST, PRICE_SELL, STOCKOUT_PEN


def surplus_upper_bound(play: Play) -> int:
    """Bound surplus in cents by relaxing the weekly action and shipment capacity limits."""
    env: Any = play.env
    penalty = round(100 * STOCKOUT_PEN)
    price = round(100 * PRICE_SELL)
    holding = round(100 * HOLD_COST)
    expedite = round(100 * EXPEDITE_COST)
    # Each unit of demand can be lost, or bought at the cheapest available arrival
    # price plus holding. Allow unlimited purchases through any route that can ship
    # at least one unit. Also allow immediate onboarding and unlimited expediting.
    cost = [price + penalty] * env.horizon
    for day in range(env.horizon):
        for supplier in range(env.NS):
            health = float(env.tape.H[day, supplier])
            if math.floor(env.cap[supplier] * health) < 1:
                continue
            lead = int(env.lead[supplier])
            delay = math.ceil((1 - health) * lead * 0.5) if health < 0.8 else 0
            _, regular = env.regular_receipt(supplier, day + lead, delay)
            express = day + (lead + 1) // 2 + delay // 2
            unit_price = round(100 * float(env.price[supplier]))
            if regular < env.horizon:
                cost[regular] = min(cost[regular], unit_price)
            if express < env.horizon:
                cost[express] = min(cost[express], unit_price + expedite)
    for day in range(1, env.horizon):
        cost[day] = min(cost[day], cost[day - 1] + holding)

    demand_by_day = [int(demand) for demand in env.tape.demand[: env.horizon]]
    prepaid = [0] * env.horizon
    prepaid[0] = int(env.onhand)
    for order in env.orders:
        if order["arr"] < env.horizon:
            prepaid[order["arr"]] += order["qty"]

    # Match each prepaid unit to a distinct future demand unit. A release on
    # day a can save cost[t] - holding * (t - a) on day t >= a. For any two
    # arrivals, swapping their assigned eligible days preserves total savings.
    # Assigning later arrivals first preserves all earlier arrivals' options.
    opportunities: list[tuple[int, int]] = []
    opening_credit = 0
    for day in reversed(range(env.horizon)):
        if demand_by_day[day]:
            heapq.heappush(opportunities, (holding * day - cost[day], demand_by_day[day]))
        available = prepaid[day]
        while available and opportunities and holding * day > opportunities[0][0]:
            priority, demand = heapq.heappop(opportunities)
            units = min(available, demand)
            opening_credit += units * (holding * day - priority)
            available -= units
            if units < demand:
                heapq.heappush(opportunities, (priority, demand - units))
    return (
        sum(
            demand * (price - unit_cost)
            for demand, unit_cost in zip(demand_by_day, cost, strict=True)
        )
        + opening_credit
    )

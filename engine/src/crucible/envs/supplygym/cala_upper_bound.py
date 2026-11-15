"""Certified terminal-inclusive surplus bound for an initial Cala scenario."""

from typing import Any

from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.sim import TERMINAL_VALUE
from crucible.envs.supplygym.world import PRICE_SELL


def surplus_upper_bound(play: Play) -> int:
    """Return an integer-cent certificate using conservation of physical units.

    Let O be prepaid opening on-hand plus pipeline, Q purchased/shipped units,
    S sales and I terminal on-hand plus pipeline. Then I = O + Q - S.
    With minimum purchase price c >= terminal value v, and nonnegative other
    costs, surplus <= (p-c) S + c O - (c-v) I <= (p-c) D + c O.
    Opening units (including arrivals beyond the horizon) occur exactly once.
    Capacity, timing, holding, stockout, expedite and switching are relaxed.
    No number of late purchases can create positive salvage arbitrage.
    """
    env: Any = play.env
    if env.day != 0:
        raise ValueError("The certificate must be computed at reset")
    price = round(100 * PRICE_SELL)
    terminal = round(100 * TERMINAL_VALUE)
    cheapest = min(round(100 * float(value)) for value in env.price)
    if not terminal <= cheapest <= price:
        raise ValueError("Certificate requires terminal value <= purchase price <= sale price")
    opening = int(env.onhand) + sum(int(order["qty"]) for order in env.orders)
    demand = sum(int(value) for value in env.tape.demand[: env.horizon])
    return (price - cheapest) * demand + cheapest * opening

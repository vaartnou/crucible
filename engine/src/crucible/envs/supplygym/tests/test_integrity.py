# pyright: basic
"""Integrity gate: degenerate strategies must not earn reward.

Run on its own with `pytest src/crucible/envs/supplygym/tests/test_integrity.py`. Each strategy is
played through the same `Play` that the tools drive and graded by the real grader.
"""

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from crucible.envs.supplygym.grader import grade_play
from crucible.envs.supplygym.play import SPLITS, Play

SEEDS = list(range(SPLITS["dev"][0], SPLITS["dev"][0] + 6))
REGIMES = (0, 1)
Strategy = Callable[[Play], None]


def _constant(args: dict[str, Any]) -> Strategy:
    def run(p: Play) -> None:
        while not p.done:
            p.apply(dict(args))

    return run


def _hoard_at_end(p: Play) -> None:
    """Buy as much as allowed in the last weeks, hoping leftover stock counts."""
    while not p.done:
        late = p.status()["weeks_left"] <= 6
        p.apply({"order_multiplier": 1.64, "safety_stock_days": 3.0} if late else {})


def _drain_at_end(p: Play) -> None:
    while not p.done:
        late = p.status()["weeks_left"] <= 6
        p.apply({"order_multiplier": 0.61, "safety_stock_days": 0.0} if late else {})


def _approve_both(p: Play) -> None:
    p.apply({"approve_alternative": "A01"})
    p.apply({"approve_alternative": "A02"})
    while not p.done:
        p.apply({})


def _finish_early(p: Play) -> None:
    p.finished = True


STRATEGIES: dict[str, Strategy] = {
    "no_calls": lambda p: None,
    "finish_at_once": _finish_early,
    "all_zero_controls": _constant({}),
    "always_expedite": _constant({"expedite_fraction": 0.3}),
    "approve_both_backups": _approve_both,
    "max_safety_stock": _constant({"safety_stock_days": 3.0}),
    "min_orders": _constant({"order_multiplier": 0.61}),
    "end_hoard": _hoard_at_end,
    "end_drain": _drain_at_end,
}
MUST_BE_ZERO = {"no_calls", "finish_at_once", "all_zero_controls"}
# Holes the gate currently finds in the world as it stands. They are expected failures, not
# retuned away: a larger safety stock beats the default rule because the default is calibrated
# too low for this world. Remove an entry once the world or default changes and it passes.
KNOWN_HOLES = {("max_safety_stock", 0), ("max_safety_stock", 1), ("end_hoard", 0)}


def _score(name: str, seed: int, regime: int):
    p = Play(seed, regime)
    STRATEGIES[name](p)
    return grade_play(p)


@pytest.mark.parametrize("regime", REGIMES)
@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_degenerate_strategy_earns_nothing(name: str, regime: int) -> None:
    if (name, regime) in KNOWN_HOLES:
        pytest.xfail("known hole: see KNOWN_HOLES")
    scores = [_score(name, s, regime) for s in SEEDS]
    totals = [s.total for s in scores]
    if name in MUST_BE_ZERO:
        assert totals == [0.0] * len(SEEDS), totals
    deltas = [s.breakdown["delta_vs_ss"] for s in scores]
    # mean over seeds: no degenerate strategy beats the default rule by more than noise
    assert float(np.mean(deltas)) < 300.0, (name, deltas)
    assert float(np.mean(totals)) < 0.2, (name, totals)


def test_profitable_incomplete_year_earns_zero_but_keeps_profit_delta() -> None:
    play = Play(1000)
    for _ in range(4):
        play.apply({})
    play.env.tot["profit"] += 10_000
    result = grade_play(play)
    assert result.total == 0
    assert result.breakdown["delta_vs_ss"] == pytest.approx(10_000)
    assert result.flags["finished_early"]
    assert grade_play(play) == result

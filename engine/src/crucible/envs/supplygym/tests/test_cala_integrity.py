"""Run the existing integrity contract and extra tape-blind probes on the Cala world.

No known-hole skips: a failure is a calibration finding. Anchors are real grader
replays cached per seed, since every strategy faces the same exogenous tape.
"""

import copy
import math
from collections.abc import Callable, Iterator
from functools import cache
from typing import Any

import pytest

from crucible.core.scoring import Score
from crucible.envs.supplygym import grader
from crucible.envs.supplygym.play import CALA_REGIME, Play
from crucible.envs.supplygym.policies.reference import CALA_REFERENCE, make_decider
from crucible.envs.supplygym.tests.test_integrity import MUST_BE_ZERO, SEEDS, STRATEGIES

Strategy = Callable[[Play], None]


@pytest.fixture(scope="module", autouse=True)
def cached_anchors() -> Iterator[None]:
    """Cache only immutable completed replay anchors; retain the actual grader."""
    default = grader.replay_default
    reference = grader.replay_reference

    @cache
    def default_at(seed: int, regime: int, salt: str, signals: str) -> Play:
        return default(Play(seed, regime, salt, signals))

    @cache
    def reference_at(seed: int, regime: int, salt: str, signals: str, name: str | None) -> Play:
        return reference(Play(seed, regime, salt, signals), name)

    def replay_default(play: Play) -> Play:
        return default_at(play.seed, play.regime, play.env.salt, play.env.signals)

    def replay_reference(play: Play, name: str | None = None) -> Play:
        return reference_at(play.seed, play.regime, play.env.salt, play.env.signals, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(grader, "replay_default", replay_default)
        patch.setattr(grader, "replay_reference", replay_reference)
        yield


def _scores(strategy: Strategy) -> list[Score]:
    scores: list[Score] = []
    for seed in SEEDS:
        play = Play(seed, CALA_REGIME)
        strategy(play)
        scores.append(grader.grade_play(play))
    return scores


def _gate(name: str, scores: list[Score], *, zero: bool = False) -> None:
    """Same seed bank and strict thresholds as test_integrity, without xfails."""
    totals = [score.total for score in scores]
    deltas = [score.breakdown["delta_vs_ss"] for score in scores]
    mean_delta = sum(deltas) / len(deltas)
    mean_reward = sum(totals) / len(totals)
    detail = {
        "strategy": name,
        "seeds": SEEDS,
        "mean_delta_vs_ss": mean_delta,
        "mean_reward": mean_reward,
        "deltas": deltas,
        "rewards": totals,
    }
    if zero:
        assert totals == [0.0] * len(SEEDS), detail
    assert mean_delta < 300.0, detail
    assert mean_reward < 0.2, detail


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_existing_gate_on_cala(name: str) -> None:
    _gate(name, _scores(STRATEGIES[name]), zero=name in MUST_BE_ZERO)


def _constant(args: dict[str, Any]) -> Strategy:
    def run(play: Play) -> None:
        while not play.done:
            assert play.refusal(args) is None
            play.apply(dict(args))

    return run


@pytest.mark.parametrize("multiplier", [math.exp(-0.5), 0.8, 0.9, 1.1, 1.2, math.exp(0.5)])
@pytest.mark.parametrize("safety_days", [0.0, 3.0])
def test_static_multiplier_grid(multiplier: float, safety_days: float) -> None:
    name = f"multiplier={multiplier:g},safety={safety_days:g}"
    args = {"order_multiplier": multiplier, "safety_stock_days": safety_days}
    _gate(name, _scores(_constant(args)))


@pytest.mark.parametrize("supplier", [f"S{i:02d}" for i in range(1, 11)])
def test_static_supplier_concentration(supplier: str) -> None:
    weights = {f"S{i:02d}": math.exp(-4) for i in range(1, 11)}
    weights[supplier] = math.exp(4)
    _gate(f"concentrate_{supplier}", _scores(_constant({"supplier_weights": weights})))


@pytest.mark.parametrize("alternative_count", [1, 2])
@pytest.mark.parametrize("favor_alternatives", [False, True])
def test_approve_and_weight_backups(alternative_count: int, favor_alternatives: bool) -> None:
    def strategy(play: Play) -> None:
        weights = {f"S{i:02d}": math.exp(-4) for i in range(1, 11)}
        weights.update({"A01": math.exp(4), "A02": math.exp(4)})
        for index in range(1, alternative_count + 1):
            args: dict[str, Any] = {"approve_alternative": f"A{index:02d}"}
            if favor_alternatives:
                args["supplier_weights"] = weights
            assert play.refusal(args) is None
            play.apply(args)
        while not play.done:
            play.apply({})

    _gate(f"backups={alternative_count},favor={favor_alternatives}", _scores(strategy))


@pytest.mark.parametrize("weeks", [1, 4, 26, 52])
def test_reference_prefix_then_finish(weeks: int) -> None:
    """Finishing delegates only the remaining days; prior honest gains are retained."""
    for seed in SEEDS:
        play = Play(seed, CALA_REGIME)
        reference = make_decider(CALA_REFERENCE)
        for _ in range(weeks):
            args = reference.decide(play.status())
            assert play.refusal(args) is None
            play.apply(args)
        explicit = copy.deepcopy(play)
        prefix_profit = play.env.summary()["profit"]
        explicit.run_default()
        continuation_profit = explicit.env.summary()["profit"] - prefix_profit
        play.finished = True
        actual = grader.grade_play(play)
        expected = grader.grade_play(explicit)
        assert actual.total == expected.total
        assert actual.breakdown == expected.breakdown
        assert actual.breakdown["profit"] == pytest.approx(prefix_profit + continuation_profit)
        if actual.breakdown["delta_vs_ss"] <= 0:
            assert actual.total == 0.0


def test_grading_is_repeatable_and_default_completes_without_control_calls() -> None:
    play = Play(SEEDS[0], CALA_REGIME)
    play.apply({})
    play.finished = True
    first = grader.grade_play(play)
    assert play.env.day == play.env.horizon
    assert play.weeks == 1
    assert first.total == 0.0
    assert first.breakdown["delta_vs_ss"] == 0.0
    assert grader.grade_play(play) == first


@pytest.mark.parametrize("attribute", ["price", "lead_days"])
def test_static_public_supplier_ranking(attribute: str) -> None:
    """Favor cheap or fast suppliers using static public facts, without signals."""

    def strategy(play: Play) -> None:
        suppliers = [s for s in play.status()["suppliers"] if s["kind"] == "direct"]
        ordered = sorted(suppliers, key=lambda supplier: (supplier[attribute], supplier["id"]))
        favored = {supplier["id"] for supplier in ordered[:3]}
        weights = {
            supplier["id"]: 3.0 if supplier["id"] in favored else 0.3 for supplier in suppliers
        }
        _constant({"supplier_weights": weights})(play)

    _gate(f"rank_by_{attribute}", _scores(strategy))


def test_inventory_only_rule() -> None:
    """A fixed inventory-position rule ignores news, risk and hidden disruptions."""

    def strategy(play: Play) -> None:
        while not play.done:
            status = play.status()
            below_target = status["inventory_position"] < status["base_stock_level"]
            play.apply({"order_multiplier": 1.2 if below_target else 0.8})

    _gate("inventory_only_rule", _scores(strategy))


@pytest.mark.parametrize("weeks", [1, 2])
def test_opening_expedite(weeks: int) -> None:
    def strategy(play: Play) -> None:
        while not play.done:
            play.apply({"expedite_fraction": 0.3 if play.weeks < weeks else 0.0})

    _gate(f"opening_expedite_{weeks}", _scores(strategy))

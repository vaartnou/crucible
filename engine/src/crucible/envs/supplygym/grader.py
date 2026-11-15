# pyright: basic
"""Scoring of `supplygym@1`.

reward = clip((profit - profit_ss) / max(profit_ref - profit_ss, floor), 0, 1)

`profit_ss` is what the default controls earn on the same tape; every week the agent did not play
runs on them, so doing nothing, stopping early and all-zero controls score exactly 0. The
reference is a public-information policy replayed on the same tape. Raw profit and the deltas go
into the breakdown, because the clipped reward hides small differences.
"""

from importlib.resources import as_file, files
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from crucible.core.scoring import Score, load_scoring_config
from crucible.envs.supplygym.play import CALA_REGIME, Play

__all__ = ["SCORING_CONFIG", "SCORING_HASH", "ScoringConfig", "grade_play", "replay_reference"]


class ScoringConfig(BaseModel):
    """Scoring constants, loaded from `scoring.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headroom_floor: float = Field(gt=0)
    reference: str


def _load() -> tuple[ScoringConfig, str]:
    with as_file(files("crucible.envs.supplygym") / "scoring.yaml") as path:
        return load_scoring_config(path, ScoringConfig)


SCORING_CONFIG, SCORING_HASH = _load()
_COST_KEYS: Final = ("expedite", "switching", "stockout", "holding", "purchase")


def _fresh(play: Play) -> Play:
    return Play(play.seed, play.regime, play.env.salt, play.env.signals)


def replay_default(play: Play) -> Play:
    """Same tape, default controls every week."""
    p = _fresh(play)
    p.run_default()
    return p


def replay_reference(play: Play, name: str | None = None) -> Play:
    """Same tape, the named reference policy driven through the same status view and controls."""
    from crucible.envs.supplygym.policies.reference import CALA_REFERENCE, make_decider

    p = _fresh(play)
    reference = CALA_REFERENCE if play.regime == CALA_REGIME else SCORING_CONFIG.reference
    decider = make_decider(name or reference)
    while not p.done and p.env.day < p.env.horizon:
        p.apply(decider.decide(p.status()))
    return p


def grade_play(play: Play) -> Score:
    """Finish the episode on default controls, then score it against the replays."""
    weeks_played = play.weeks
    play.run_default()
    me = play.env.summary()
    ss = replay_default(play).env.summary()
    ref = replay_reference(play).env.summary()
    headroom = max(ref["profit"] - ss["profit"], SCORING_CONFIG.headroom_floor)
    reward = min(1.0, max(0.0, (me["profit"] - ss["profit"]) / headroom))
    breakdown = {
        "profit": me["profit"],
        "profit_ss": ss["profit"],
        "profit_ref": ref["profit"],
        "delta_vs_ss": me["profit"] - ss["profit"],
        "delta_vs_ref": me["profit"] - ref["profit"],
        "service": me["service"],
        "lost_units": me["lost_units"],
        "weeks_played": float(weeks_played),
    }
    breakdown.update({k: me[k] for k in _COST_KEYS})
    return Score(
        total=reward,
        breakdown=breakdown,
        flags={"finished_early": play.finished, "beat_ss": me["profit"] > ss["profit"]},
    )

"""Seeded draw from the offline-certified layout bank; no solver on the play path."""

import json
from functools import cache
from pathlib import Path

from crucible.core.rng import RngStreams
from crucible.envs.last_siding.yard import Layout

DEMO_SEED = 1000000
CONTROL_SEED = 1000001


@cache
def bank() -> tuple[Layout, ...]:
    raw = json.loads(Path(__file__).with_name("layouts.json").read_text(encoding="utf8"))
    return tuple(Layout(tuple(b), tuple(c), tuple(m), opt) for b, c, m, opt in raw)


def draw(seed: int, salt: str | None) -> Layout:
    if salt is None and seed in (DEMO_SEED, CONTROL_SEED):
        return Layout(("ED", "BC", "A"), (3, 3 + int(seed == CONTROL_SEED), 3), ("AB", "CDE"), 6)
    layouts = bank()
    epoch, index = divmod(seed, len(layouts))
    order = list(range(len(layouts)))
    RngStreams(epoch, salt).exogenous.shuffle(order)
    layout = layouts[order[index]]
    if RngStreams(seed, salt).stream("siding-names").randrange(2):
        d, left, right = layout.board
        cd, cl, cr = layout.caps
        return Layout((d, right, left), (cd, cr, cl), layout.manifests, layout.optimal)
    return layout

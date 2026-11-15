"""Counterfactual impact and a private thirty-minute recovery probe."""

import json
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, Field

from crucible.core.scoring import Score, load_scoring_config
from crucible.envs.oncall.model import PodSample, State, advance
from crucible.envs.oncall.world import HORIZON, World


class Weights(BaseModel):
    base: float = Field(ge=0, le=1)
    verified: float = Field(ge=0, le=1)
    diagnosis: float = Field(ge=0, le=1)


WEIGHTS, SCORING_HASH = load_scoring_config(Path(__file__).with_name("scoring.yaml"), Weights)


def loss(frame: tuple[PodSample, ...]) -> float:
    return sum(
        p.request_rate * max(p.error_ratio, 0.1 if p.p99_latency > 2 else 0)
        for p in frame
        if p.service == "edge-gateway"
    )


# World contains mutable nested change dictionaries, so its fingerprint (or object identity)
# is insufficient. Snapshot all fields into an immutable canonical key on every lookup.
_BASELINE_CACHE_LIMIT = 256
_BASELINE_CACHE: OrderedDict[tuple[str, int], float] = OrderedDict()


def _baseline_loss(world: World, horizon: int) -> float:
    key = (
        json.dumps(asdict(world), sort_keys=True, separators=(",", ":"), allow_nan=False),
        horizon,
    )
    if key in _BASELINE_CACHE:
        _BASELINE_CACHE.move_to_end(key)
        return _BASELINE_CACHE[key]
    # Preserve the exact chronological replay, including queue carry and pre-incident history.
    baseline_state = State(minute=-31)
    advance(world, baseline_state, horizon + 31)
    total = sum(loss(frame) for frame in baseline_state.history if frame[0].minute > 5)
    _BASELINE_CACHE[key] = total
    if len(_BASELINE_CACHE) > _BASELINE_CACHE_LIMIT:
        _BASELINE_CACHE.popitem(last=False)
    return total


def grade(world: World, state: State) -> Score:
    probe = deepcopy(state)
    advance(world, probe, 30)
    verified = (
        all(loss(frame) < 0.01 for frame in probe.history[-30:])
        and probe.minute >= state.stampede_until
    )
    # Fill a common horizon so early resolve cannot hide ongoing impact.
    advance(world, probe, max(0, HORIZON - probe.minute))
    baseline = _baseline_loss(world, max(HORIZON, probe.minute))
    actual = sum(loss(f) for f in probe.history if f[0].minute > 5)
    impact = max(0.0, min(1.0, 1 - actual / baseline)) if baseline else 0.0
    report = state.report or {}
    expected = {
        "service": world.culprit,
        "fault_kind": world.fault,
        "change_id": world.change_id,
        "remediation": "config_revert"
        if world.fault in ("config_push", "pool_exhaustion", "retry_storm")
        else "rollback",
    }
    if world.fault == "queue_backlog" and report.get("remediation") == "restart":
        expected["remediation"] = "restart"
    diagnosis = sum(report.get(key) == value for key, value in expected.items()) / 4
    reward = max(
        0.0,
        min(
            1.0,
            impact * (WEIGHTS.base + WEIGHTS.verified * verified + WEIGHTS.diagnosis * diagnosis)
            - state.penalty,
        ),
    )
    return Score(
        total=reward,
        breakdown={
            "impact": impact,
            "verified": float(verified),
            "diagnosis": diagnosis,
            "penalty": state.penalty,
        },
        flags={"verified": verified, "diagnosis_correct": diagnosis == 1},
    )

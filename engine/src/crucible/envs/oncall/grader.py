"""Counterfactual impact and a private thirty-minute recovery probe."""

import json
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, Field

from crucible.core.scoring import Score, load_scoring_config
from crucible.envs.oncall.model import PodSample, State, advance
from crucible.envs.oncall.world import HORIZON, Incident, World


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
    incidents = world.incidents or (Incident(world.culprit, world.fault, world.change_id),)
    reports = state.incident_reports if world.incidents else [state.report or {}]
    diagnosis = _diagnosis(incidents, reports or [])
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


def _diagnosis(incidents: tuple[Incident, ...], reports: list[dict[str, str]]) -> float:
    """Maximum one-to-one field matching, normalized to penalize missing/extra reports."""

    def credit(incident: Incident, report: dict[str, str]) -> int:
        remediation = (
            "config_revert"
            if incident.fault_kind in ("config_push", "pool_exhaustion", "retry_storm")
            else "rollback"
        )
        if incident.fault_kind == "queue_backlog" and report.get("remediation") == "restart":
            remediation = "restart"
        expected = {
            "service": incident.service,
            "fault_kind": incident.fault_kind,
            "change_id": incident.change_id,
            "remediation": remediation,
        }
        return sum(report.get(key) == value for key, value in expected.items())

    # At most two causes and eight reports in the overlap task. Each report and cause
    # can contribute only once, regardless of order or repeated guesses.
    scores = {0: 0}
    for incident in incidents:
        updated = dict(scores)
        for mask, value in scores.items():
            for index, report in enumerate(reports):
                bit = 1 << index
                if not mask & bit:
                    updated[mask | bit] = max(
                        updated.get(mask | bit, 0), value + credit(incident, report)
                    )
        scores = updated
    return max(scores.values()) / (4 * max(len(incidents), len(reports)))

"""Reference deciders for `supplygym@1`: status view in, `set_controls` arguments out.

A decider sees exactly what an agent sees through the tools. The grader replays one in-process on
the same tape and the Harbor oracle plays the same code over MCP, so the two give the same score.
"""

import hashlib
import math
from importlib.resources import as_file, files
from typing import Any, Protocol

import numpy as np

from crucible.envs.supplygym.policies.views import StatusView, to_vector

__all__ = ["Decider", "make_decider"]

CALA_REFERENCE = "cala_ppo"
CALA_CHECKPOINT = "cala_ppo.npz"
CONTROL_SCALES = {
    "supplier_weight_log_scale": 4.0,
    "order_multiplier_log_scale": 0.5,
    "safety_stock_days_scale": 3.0,
    "expedite_fraction_scale": 0.3,
    "alternative_approval_threshold": 0.5,
}


def cala_policy_identity() -> dict[str, Any]:
    """Bind the Cala denominator to the shipped checkpoint and inference contract."""
    checkpoint = files("crucible.envs.supplygym.policies") / "data" / CALA_CHECKPOINT
    return {
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "config": {
            "reference": CALA_REFERENCE,
            "checkpoint": CALA_CHECKPOINT,
            "observation": "public-status-vector-112-v1",
            "architecture": "112-64-64-16-tanh-linear",
            "dtype": "float64",
            "action": "deterministic-mean-clip-minus1-plus1",
            **CONTROL_SCALES,
        },
    }


class Decider(Protocol):
    def decide(self, status: dict[str, Any]) -> dict[str, Any]: ...


class DefaultRule:
    """Default controls every week: the (s,S) anchor."""

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        return {}


class AvailabilityRule:
    """Deterministic public-observation reference for the version 2 rubric."""

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        weights = {}
        for supplier in status["suppliers"]:
            if supplier["kind"] == "direct":
                fill = max(0.1, min(1.0, supplier["fill_ratio"]))
                risk_estimate = 1.0 - (supplier["risk"] - 0.15) / 0.7
                availability = (fill + max(0.1, min(1.0, risk_estimate))) / 2.0
                weights[supplier["id"]] = max(math.exp(-4), availability**2)
        return {"supplier_weights": weights}


def _next_alt(view: StatusView) -> str | None:
    for s in view.suppliers:
        if s.kind == "alt" and s.state == "not_approved":
            return s.id
    return None


def action_to_args(a: np.ndarray, view: StatusView) -> dict[str, Any]:
    """16-dim PPO action in [-1, 1] -> `set_controls` arguments."""
    ids = [s.id for s in view.suppliers]
    args: dict[str, Any] = {
        "supplier_weights": {
            ids[i]: math.exp(CONTROL_SCALES["supplier_weight_log_scale"] * float(a[i]))
            for i in range(12)
        },
        "safety_stock_days": CONTROL_SCALES["safety_stock_days_scale"] * max(0.0, float(a[13])),
        "order_multiplier": math.exp(CONTROL_SCALES["order_multiplier_log_scale"] * float(a[12])),
        "expedite_fraction": CONTROL_SCALES["expedite_fraction_scale"] * max(0.0, float(a[14])),
    }
    if a[15] > CONTROL_SCALES["alternative_approval_threshold"] and (alt := _next_alt(view)):
        args["approve_alternative"] = alt
    return args


class PpoDecider:
    """The trained PPO policy as a numpy MLP (64-64 tanh), deterministic mean action."""

    def __init__(self, path: str = "supplygym_ppo.npz") -> None:
        checkpoint = files("crucible.envs.supplygym.policies") / "data" / path
        with as_file(checkpoint) as p, np.load(p) as w:
            self.w = {k: np.asarray(w[k], dtype=np.float64) for k in w.files}

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        view = StatusView.model_validate(status)
        x = to_vector(view).astype(np.float64)
        w = self.w
        h = np.tanh(w["W1"] @ x + w["b1"])
        h = np.tanh(w["W2"] @ h + w["b2"])
        a = np.clip(w["Wa"] @ h + w["ba"], -1.0, 1.0)
        return action_to_args(a, view)


def make_decider(name: str) -> Decider:
    if name == "availability":
        return AvailabilityRule()
    if name == "ss":
        return DefaultRule()
    if name == "ppo":
        return PpoDecider()
    if name == CALA_REFERENCE:
        return PpoDecider(CALA_CHECKPOINT)
    raise ValueError(f"unknown reference {name!r}")

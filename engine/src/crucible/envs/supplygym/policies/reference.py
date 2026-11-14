"""Reference deciders for `supplygym@1`: status view in, `set_controls` arguments out.

A decider sees exactly what an agent sees through the tools. The grader replays one in-process on
the same tape and the Harbor oracle plays the same code over MCP, so the two give the same score.
"""

import math
from importlib.resources import as_file, files
from typing import Any, Protocol

import numpy as np

from crucible.envs.supplygym.policies.views import StatusView, to_vector

__all__ = ["Decider", "make_decider"]


class Decider(Protocol):
    def decide(self, status: dict[str, Any]) -> dict[str, Any]: ...


class DefaultRule:
    """Default controls every week: the (s,S) anchor."""

    def decide(self, status: dict[str, Any]) -> dict[str, Any]:
        return {}


def _next_alt(view: StatusView) -> str | None:
    for s in view.suppliers:
        if s.kind == "alt" and s.state == "not_approved":
            return s.id
    return None


def action_to_args(a: np.ndarray, view: StatusView) -> dict[str, Any]:
    """16-dim PPO action in [-1, 1] -> `set_controls` arguments."""
    ids = [s.id for s in view.suppliers]
    args: dict[str, Any] = {
        "supplier_weights": {ids[i]: math.exp(4.0 * float(a[i])) for i in range(12)},
        "safety_stock_days": 3.0 * max(0.0, float(a[13])),
        "order_multiplier": math.exp(0.5 * float(a[12])),
        "expedite_fraction": 0.3 * max(0.0, float(a[14])),
    }
    if a[15] > 0.5 and (alt := _next_alt(view)) is not None:
        args["approve_alternative"] = alt
    return args


class PpoDecider:
    """The trained PPO policy as a numpy MLP (64-64 tanh), deterministic mean action."""

    def __init__(self, path: str = "supplygym_ppo.npz") -> None:
        with as_file(files("crucible.envs.supplygym.policies") / "data" / path) as p:
            w = np.load(p)
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
    if name == "ss":
        return DefaultRule()
    if name == "ppo":
        return PpoDecider()
    raise ValueError(f"unknown reference {name!r}")

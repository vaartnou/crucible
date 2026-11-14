"""Operators that use only the public tools and their results.

Every operator is registered for `fleetbase_dispatch` and for `fleetbase_sim`, which share
tools and results.

Ladder (weakest to strongest): `do_nothing` (generic), `stop_early`, `fifo`,
`greedy_no_replan`, `greedy_nearest` (alias `greedy`) in `baselines.py`, and
`reference` in `reference.py` (online regret insertion with 2-opt from `solver.py`).
Exploits are in `exploits.py`. The private hindsight denominator runs the same solver
with the whole world known; it is not a policy.
"""

from crucible.core.registry import register_policy
from crucible.envs.fleetbase_dispatch.policies.baselines import (
    FifoOperator,
    GreedyNoReplanOperator,
    GreedyOperator,
)
from crucible.envs.fleetbase_dispatch.policies.common import Operator
from crucible.envs.fleetbase_dispatch.policies.exploits import (
    AssignBrokenVehicleOperator,
    CreateThenCancelOperator,
    EstimateSpamOperator,
    FinishImmediatelyOperator,
    OneVehicleOperator,
    RepositionSpamOperator,
    RerouteAfterPickupOperator,
)
from crucible.envs.fleetbase_dispatch.policies.reference import ReferenceOperator

EXPLOITS = (
    "one_vehicle",
    "create_then_cancel",
    "reposition_spam",
    "estimate_spam",
    "finish_immediately",
    "assign_broken_vehicle",
    "reroute_after_pickup",
)
LADDER = ("do_nothing", "stop_early", "fifo", "greedy_no_replan", "greedy_nearest", "reference")


@register_policy("stop_early", "fleetbase_dispatch")
@register_policy("stop_early", "fleetbase_sim")
class StopEarlyOperator(ReferenceOperator):
    """Runs the reference until its first successful dispatch, then finishes."""

    name = "stop_early"
    stop_after_dispatch = True


__all__ = [
    "EXPLOITS",
    "LADDER",
    "AssignBrokenVehicleOperator",
    "CreateThenCancelOperator",
    "EstimateSpamOperator",
    "FifoOperator",
    "FinishImmediatelyOperator",
    "GreedyNoReplanOperator",
    "GreedyOperator",
    "OneVehicleOperator",
    "Operator",
    "ReferenceOperator",
    "RepositionSpamOperator",
    "RerouteAfterPickupOperator",
    "StopEarlyOperator",
]

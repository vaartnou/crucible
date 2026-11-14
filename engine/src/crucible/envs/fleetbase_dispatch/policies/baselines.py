"""Baseline operators of the policy ladder, below the reference.

- `fifo`: like `greedy_nearest`, but each vehicle takes the oldest open order.
- `greedy_nearest` (alias `greedy`): each vehicle whose route ends within 10 minutes
  gets the open order whose pickup is nearest to its route end. It ignores value,
  deadlines, cost and shift ends. Every epoch it reads the vehicles again, and also
  ``created`` orders, so orders a breakdown unassigned are served again.
- `greedy_no_replan`: `greedy_nearest` without feedback after dispatch: it reads the
  vehicles once and then predicts their availability from its own dispatches.
"""

import math

from crucible.core.registry import register_policy
from crucible.core.tools import ToolCall, ToolResult
from crucible.envs.fleetbase_dispatch.policies.common import (
    LOOKAHEAD_MINUTES,
    Operator,
    OrderView,
    Point,
    text,
)


@register_policy("greedy_nearest", "fleetbase_dispatch")
@register_policy("greedy_nearest", "fleetbase_sim")
@register_policy("greedy", "fleetbase_dispatch")
@register_policy("greedy", "fleetbase_sim")
class GreedyOperator(Operator):
    """Each idle vehicle takes the open order with the nearest pickup."""

    name = "greedy_nearest"

    def _plan(self) -> list[tuple[OrderView, str]]:
        return self._idle_plan("nearest")


@register_policy("greedy_no_replan", "fleetbase_dispatch")
@register_policy("greedy_no_replan", "fleetbase_sim")
class GreedyNoReplanOperator(GreedyOperator):
    """`greedy_nearest` without feedback after dispatch.

    It reads the vehicles once at the start. After that it predicts when and where each
    vehicle finishes from its own dispatches (straight-line driving, waiting for ready
    times) and never reads vehicles, events or dispatched orders again. Breakdowns,
    traffic, silent cancellations and dropoff changes therefore go unnoticed, and orders
    a breakdown unassigned are never served again.
    """

    name = "greedy_no_replan"
    reads_created = False

    def _reset_state(self) -> None:
        super()._reset_state()
        self._free: dict[str, tuple[Point, float]] = {}
        self._assigned: dict[str, str] = {}

    def _reads(self) -> list[ToolCall]:
        reads = super()._reads()
        if self._vehicles:
            reads = [c for c in reads if c.name != "list_vehicles"]
        return reads

    def _absorb(self, call: ToolCall, result: ToolResult) -> None:
        super()._absorb(call, result)
        if not result.ok:
            return
        request_id = text(call.args.get("request_id"))
        if call.name == "assign_order" and request_id is not None:
            vehicle_id = text(call.args.get("vehicle_id"))
            if vehicle_id is not None:
                self._assigned[request_id] = vehicle_id
        elif call.name == "dispatch_order" and request_id in self._assigned:
            vehicle_id = self._assigned[request_id]
            order = self._orders.get(request_id)
            vehicle = self._vehicles.get(vehicle_id)
            if order is None or vehicle is None:
                return
            position, time = self._free.get(vehicle_id, (vehicle.position, self._now))
            time = max(time, self._now)
            time = max(time + math.dist(position, order.pickup), order.ready)
            self._free[vehicle_id] = (
                order.dropoff,
                time + math.dist(order.pickup, order.dropoff),
            )

    def _plan(self) -> list[tuple[OrderView, str]]:
        remaining = sorted(self._open.values(), key=lambda o: o.id)
        plans: list[tuple[OrderView, str]] = []
        for vehicle in sorted(self._vehicles.values(), key=lambda v: v.id):
            position, time = self._free.get(vehicle.id, (vehicle.position, self._now))
            if time > self._now + LOOKAHEAD_MINUTES or self._now >= vehicle.shift_end:
                continue
            fits = [o for o in remaining if o.load <= vehicle.capacity]
            if not fits:
                continue
            order = min(fits, key=lambda o: (math.dist(position, o.pickup), o.id))
            plans.append((order, vehicle.id))
            remaining.remove(order)
        return plans


@register_policy("fifo", "fleetbase_dispatch")
@register_policy("fifo", "fleetbase_sim")
class FifoOperator(Operator):
    """Each idle vehicle takes the oldest open order that fits."""

    name = "fifo"

    def _plan(self) -> list[tuple[OrderView, str]]:
        return self._idle_plan("oldest")


__all__ = ["FifoOperator", "GreedyNoReplanOperator", "GreedyOperator"]

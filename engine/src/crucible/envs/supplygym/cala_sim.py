# pyright: basic
"""Scheduled regular freight for the Cala scenario."""

from typing import Any

from crucible.envs.supplygym.cala_world import ASSUMPTIONS
from crucible.envs.supplygym.sim import ACK_DAYS, DEMAND_MEAN, SupplyEnv


class CalaSupplyEnv(SupplyEnv):
    def reset(self, *, seed: int | None = None, options: Any = None):
        super().reset(seed=seed, options=options)
        # An operating electronics buyer starts with prepaid freight already booked.
        # Each opening unit belongs to exactly one shipment. No future tape is read.
        active = self.active()
        quantity = int(DEMAND_MEAN) // len(active)
        cycle = ASSUMPTIONS["receipt_cycle_days"]
        for i in active:
            lead = int(self.lead[i])
            for day in range(-lead - cycle + 1, 0):
                nominal = day + lead
                booked, _ = self.regular_receipt(i, nominal, 0)
                if booked >= 0:
                    self.orders.append(
                        dict(
                            sup=i,
                            arr=booked,
                            nom=booked,
                            rev=min(nominal, day + ACK_DAYS),
                            qty=quantity,
                            ordered=quantity,
                        )
                    )
        reorder, _ = self.levels(active, 0)
        self.onhand = float(max(0, round(reorder - self._pipeline())))
        return self._obs(), self._info([])

    def regular_receipt(self, supplier: int, nominal: int, delay: int) -> tuple[int, int]:
        slot = self.world.nodes[supplier]["receipt_slot"]
        period = ASSUMPTIONS["receipt_cycle_days"]
        booked = nominal + (slot - nominal) % period
        ready = nominal + delay
        return booked, ready + (slot - ready) % period

    def reported_arrival(self, order: dict[str, Any]) -> int:
        # Once a booking is overdue the buyer has no revised promise.
        return max(self.day, order["nom"])

# pyright: basic
"""Weekly procurement for a single electronics unit across disclosed supplier sites."""

import hashlib
import json
from typing import ClassVar

from pydantic import JsonValue

from crucible.core.registry import register_task
from crucible.core.task import TaskSpec
from crucible.envs.supplygym.cala_world import ASSUMPTIONS, cala_world_identity
from crucible.envs.supplygym.grader import SCORING_HASH
from crucible.envs.supplygym.play import CALA_REGIME, Play
from crucible.envs.supplygym.task import SupplyGymTask

_TRANSFER_SCORING = (
    "Scoring (0 to 1): beat the default rule on this same year. Your profit gain is divided "
    "by the larger of $1,000 and the frozen PPO's gain, then clipped to [0, 1]. The PPO "
    "plays with public information and was trained on a different world."
)


@register_task
class SupplyGymCalaTask(SupplyGymTask):
    spec: ClassVar[TaskSpec] = SupplyGymTask.spec.model_copy(
        update={
            "id": "supplygym-cala",
            "title": "Crucible: electronics procurement across supplier sites",
            "description": "Manage weekly replenishment from multi-site electronics suppliers "
            "with regional disruptions and delayed operating signals.",
        }
    )
    regime: ClassVar[int] = CALA_REGIME
    scoring_hash: ClassVar[str] = hashlib.sha256(
        json.dumps(
            {"scoring": SCORING_HASH, "world": cala_world_identity()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    def instruction(self) -> str:
        return super().instruction() + (
            "\n\nYour product is a standardized electronics unit. Each supplier can draw on "
            "several operating sites. Its listed region is its largest modeled exposure. "
            "A regional disruption can reduce deliveries from suppliers listed elsewhere. "
            "The quoted terms and product are simulated. The supplier and site relationships "
            "come from historical disclosures. You start with stock and prepaid freight "
            "already in transit. Regular freight arrives at scheduled slots. "
            "Expediting bypasses consolidation. The briefing lists each supplier's receipt "
            "day modulo the cycle. An overdue booking shows zero days to its next arrival."
        )

    def _scoring_instruction(self) -> str:
        return _TRANSFER_SCORING

    def _briefing(self, play: Play) -> dict[str, JsonValue]:
        briefing = super()._briefing(play)
        briefing["supplier_aliases"] = {
            str(n["id"]): str(n["name"]) for n in play.env.world.public()
        }
        briefing["receipt_cycle_days"] = ASSUMPTIONS["receipt_cycle_days"]
        briefing["regular_receipt_slots"] = {
            str(n["id"]): int(n["receipt_slot"]) for n in play.env.world.nodes[: play.env.NS]
        }
        return briefing

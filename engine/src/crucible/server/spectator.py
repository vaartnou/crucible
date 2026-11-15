"""Opt-in private JSONL feed of completed Cala outcome decisions."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from crucible.core.scoring import Score
from crucible.core.task import Task
from crucible.core.tools import ToolCall, ToolResult
from crucible.envs.supplygym.cala_world import cala_world_identity
from crucible.envs.supplygym.outcomes import CalaSpectatorSnapshot, SupplyGymCalaOutcomes


def _simulator_version() -> dict[str, str]:
    """Bind the world identity to the source of the public simulator implementation."""
    directory = Path(__file__).parents[1] / "envs" / "supplygym"
    digest = hashlib.sha256()
    for name in ("sim.py", "cala_sim.py", "play.py", "state.py"):
        digest.update(name.encode() + b"\0")
        digest.update((directory / name).read_bytes())
    return {**cala_world_identity(), "implementation_sha256": digest.hexdigest()}


class Spectator:
    """Append only public observations and verified accounting, never the hidden tape or salt."""

    def __init__(
        self,
        path: Path,
        task: Task,
        seed: int,
        salt: str,
        run_id: str = "",
        strategy: str = "external",
    ) -> None:
        if not isinstance(task, SupplyGymCalaOutcomes):
            raise ValueError("Spectator feed requires a Cala outcome task")
        if not salt:
            raise ValueError("Spectator feed requires a private sidecar salt")
        self.path, self.task = Path(path), task
        self.finalized = False
        self.metadata = {
            "schema_version": 1,
            "run_id": run_id or uuid4().hex,
            "strategy": strategy,
            "scenario_comparison_id": hmac.new(
                salt.encode(), f"{task.spec.ref}:{seed}".encode(), hashlib.sha256
            ).hexdigest(),
            "task": task.spec.ref,
            "simulator_version": _simulator_version(),
            "scorer_version": task.scoring_hash,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event: dict[str, Any]) -> None:
        record = {**self.metadata, "timestamp": datetime.now(UTC).isoformat(), **event}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
            stream.flush()

    def before_step(self, call: ToolCall) -> CalaSpectatorSnapshot | None:
        if call.name != "set_controls" or self.task.done():
            return None
        return self.task.spectator_snapshot()

    def after_step(
        self, call: ToolCall, result: ToolResult, before: CalaSpectatorSnapshot | None
    ) -> None:
        if not result.ok or call.name != "set_controls" or before is None:
            return
        current = self.task.spectator_snapshot()
        cumulative = current["operating_surplus"]
        self._write(
            {
                "event": "week",
                "week": current["week"],
                "end_day": current["end_day"],
                "observation": before["observation"],
                "requested_controls": call.args,
                "applied_controls": current["observation"]["controls"],
                "weekly_result": result.content,
                "weekly_operating_surplus": cumulative - before["operating_surplus"],
                "cumulative_operating_surplus": cumulative,
            }
        )

    def finalize(self, reason: str, score: Score) -> None:
        if self.finalized:
            return
        current = self.task.spectator_snapshot()
        self._write(
            {
                "event": "final",
                "week": current["week"],
                "end_day": current["end_day"],
                "stop_reason": reason,
                "completed": score.flags.get("completed", False),
                "score": score.model_dump(mode="json"),
                "verified_accounting": score.breakdown,
            }
        )
        self.finalized = True

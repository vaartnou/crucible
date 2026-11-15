"""Local, private SupplyGym simulation controller (loaded only with engine dependencies)."""

import asyncio
import json
import os
import secrets
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, ToolCall, result_text
from crucible.core.transcript import TranscriptWriter
from crucible.envs.supplygym.outcomes import OutcomeControls
from crucible.server.spectator import Spectator

os.environ["CRUCIBLE_ENVIRONMENTS"] = "supplygym"

TASK = "supplygym-cala@3"
T = TypeVar("T")

BASELINES = ("ss", "base_stock", "fixed_lot", "fill_adjusted_base_stock")


class SimulationError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_controls(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(OutcomeControls.model_fields):
        raise SimulationError("controls must contain only recognized fields")
    # JSON booleans and strings must not be silently coerced to numbers.
    for key in ("safety_stock_days", "order_multiplier", "expedite_fraction"):
        if key in value and value[key] is not None and type(value[key]) not in (int, float):
            raise SimulationError(f"{key} must be a number")
    weights = value.get("supplier_weights")
    if weights is not None:
        if not isinstance(weights, dict) or any(
            key not in {*(f"S{i:02d}" for i in range(1, 11)), "A01", "A02"}
            or type(number) not in (int, float)
            for key, number in weights.items()
        ):
            raise SimulationError("invalid supplier weights")
    try:
        return OutcomeControls.model_validate(value, strict=True).model_dump(exclude_none=True)
    except Exception as exc:
        raise SimulationError("invalid controls: " + str(exc)) from exc


class Controller:
    def __init__(self, output: Path) -> None:
        self.output = Path(output).resolve()
        # Stable only for this server lifetime, enabling same-seed comparisons.
        self.salt = secrets.token_hex(32)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.run = None
        self.session = self.feed = None
        self.auto = None
        self.preparation = None
        self.mutation_lock = asyncio.Lock()

    def call(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def snapshot(self) -> dict[str, Any]:
        return {"available": True, "run": dict(self.run) if self.run else None}

    async def get(self) -> dict[str, Any]:
        return self.snapshot()

    async def create(self, seed: int, controls: dict[str, Any]) -> dict[str, Any]:
        async with self.mutation_lock:
            return await self._create(seed, controls)

    async def _create(self, seed: int, controls: dict[str, Any]) -> dict[str, Any]:
        if type(seed) is not int or not 0 <= seed <= 2147483647:
            raise SimulationError("seed must be an integer between 0 and 2147483647")
        controls = validate_controls(controls)
        if self.run and self.run["status"] in ("preparing", "ready", "running"):
            raise SimulationError("a simulation is already active", 409)
        ident = uuid4().hex
        self.run = {
            "id": ident,
            "seed": seed,
            "status": "preparing",
            "week": 0,
            "observation": None,
            "draft_controls": controls,
            "manifest_url": f"/simulations/{ident}/spectator-manifest.json",
        }
        self.session = self.feed = None
        result = self.snapshot()
        self.preparation = asyncio.create_task(self.prepare(ident, seed, controls))
        return result

    async def baseline(self, root: Path, seed: int, salt: str, name: str, ident: str) -> None:
        task = get_task(TASK)
        policy = get_policy(name, TASK)
        session = EpisodeSession(task, seed, TranscriptWriter(root / "transcript.jsonl"), salt=salt)
        feed = None
        try:
            obs = await session.start(policy_name=name, job=Job(task=TASK, policy=name, seed=seed))
            feed = Spectator(root / "events.jsonl", task, seed, salt, f"{ident}-{name}", name)
            await policy.reset(task.spec, task.tools(), task.instruction())
            while not task.done():
                call = await policy.act(obs)
                if call is None:
                    raise RuntimeError(f"{name} stopped early")
                before = feed.before_step(call)
                result = await session.step(call)
                feed.after_step(call, result, before)
                if not result.ok:
                    raise RuntimeError(f"{name}: {result.error}")
                obs = Observation(text=result_text(result), last_result=result)
                await asyncio.sleep(0)
            reason, score = await session.close()
            feed.finalize(reason, score)
        except BaseException:
            if not session.closed:
                reason, score = await session.close("baseline_error")
                if feed:
                    feed.finalize(reason, score)
            raise

    async def prepare(self, ident: str, seed: int, controls: dict[str, Any]) -> None:
        root = self.output / ident
        salt = self.salt
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
            for name in BASELINES:
                await self.baseline(
                    root / "baselines" / f"seed-{seed}" / name, seed, salt, name, ident
                )
            task = get_task(TASK)
            session = EpisodeSession(
                task,
                seed,
                TranscriptWriter(root / "live" / f"seed-{seed}" / "transcript.jsonl"),
                salt=salt,
            )
            self.session = session
            await session.start(
                policy_name="manual", job=Job(task=TASK, policy="manual", seed=seed)
            )
            self.feed = Spectator(
                root / "live" / f"seed-{seed}" / "events.jsonl",
                task,
                seed,
                salt,
                f"{ident}-manual",
                "manual",
            )
            # Create an empty feed so the viewer can poll before the first action.
            self.feed.path.touch()
            events = {name: f"baselines/seed-{seed}/{name}/events.jsonl" for name in BASELINES}
            events["manual"] = f"live/seed-{seed}/events.jsonl"
            manifest = [
                {
                    "seed": seed,
                    "scenario_comparison_id": self.feed.metadata["scenario_comparison_id"],
                    "events": events,
                }
            ]
            (root / "spectator-manifest.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8"
            )
            self.run.update(status="ready", observation=task.spectator_snapshot()["observation"])
        except Exception as exc:
            if self.session and not self.session.closed:
                await self.session.close("preparation_error")
            self.run.update(status="error", error=str(exc))

    def require(self, ident: str, statuses: tuple[str, ...]) -> None:
        if not self.run or self.run["id"] != ident:
            raise SimulationError("simulation not found", 404)
        if self.run["status"] not in statuses:
            raise SimulationError("simulation is not in a suitable state", 409)

    async def do_step(self, controls: dict[str, Any]) -> None:
        task = self.session.task
        refusal = task._play.refusal(controls)
        if refusal:
            raise SimulationError(refusal)
        call = ToolCall(name="set_controls", args=controls)
        before = self.feed.before_step(call)
        result = await self.session.step(call)
        if not result.ok:
            raise RuntimeError(result.error)
        self.feed.after_step(call, result, before)
        snap = task.spectator_snapshot()
        self.run.update(week=snap["week"], observation=snap["observation"])
        if task.done():
            reason, score = await self.session.close()
            self.feed.finalize(reason, score)
            self.run["status"] = "completed"

    async def action(
        self, ident: str, action: str, controls: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self.mutation_lock:
            return await self._action(ident, action, controls)

    async def _action(
        self, ident: str, action: str, controls: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.require(
            ident,
            (
                ("preparing", "ready", "running")
                if action == "cancel"
                else ("ready", "running")
                if action == "pause"
                else ("ready",)
            ),
        )
        if action in ("step", "run"):
            controls = validate_controls(controls)
            refusal = self.session.task._play.refusal(controls)
            if refusal:
                raise SimulationError(refusal)
        if action == "step":
            try:
                await self.do_step(controls)
            except Exception as exc:
                if not self.session.closed:
                    reason, score = await self.session.close("simulation_error")
                    self.feed.finalize(reason, score)
                self.run.update(status="error", error=str(exc))
                raise SimulationError("simulation failed", 500) from exc
        elif action == "run":
            self.run["status"] = "running"
            self.auto = asyncio.create_task(self.run_auto(ident, controls))
        elif action == "pause":
            self.run["status"] = "ready"
            if self.auto:
                await self.auto
                self.auto = None
        elif action == "cancel":
            was_preparing = self.run["status"] == "preparing"
            self.run["status"] = "cancelled"
            if was_preparing and self.preparation:
                self.preparation.cancel()
                await asyncio.gather(self.preparation, return_exceptions=True)
            if self.auto:
                await self.auto
                self.auto = None
            if self.session and not self.session.closed:
                reason, score = await self.session.close()
                if self.feed:
                    self.feed.finalize(reason, score)
        else:
            raise SimulationError("unknown action", 404)
        return self.snapshot()

    async def run_auto(self, ident: str, controls: dict[str, Any]) -> None:
        try:
            while self.run and self.run["id"] == ident and self.run["status"] == "running":
                await self.do_step(controls)
                # Approval is a one-off action, not a standing instruction.
                controls = {k: v for k, v in controls.items() if k != "approve_alternative"}
                await asyncio.sleep(0.03)
        except Exception as exc:
            if self.session and not self.session.closed:
                reason, score = await self.session.close("simulation_error")
                self.feed.finalize(reason, score)
            self.run.update(status="error", error=str(exc))

    async def shutdown(self) -> None:
        if self.run and self.run["status"] in ("preparing", "ready", "running"):
            await self.action(self.run["id"], "cancel")
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.session and not self.session.closed:
            await self.session.close("shutdown")

    def close(self) -> None:
        self.call(self.shutdown())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()

"""Native Fleetbase workflow backed by private deterministic physical evidence."""

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from crucible.backends import (
    BackendConnection,
    BackendError,
    ExclusiveLeaseProvider,
    HttpBackendRuntime,
)
from crucible.core.registry import register_task
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskChecks, TaskSpec
from crucible.core.tools import (
    Observation,
    ToolCall,
    ToolResult,
    ToolSpec,
    make_tool,
    tool_map,
    validate_call,
)
from crucible.envs.fleetbase_dispatch.adapter import FleetbaseAdapter, FleetbaseOperationError
from crucible.envs.fleetbase_dispatch.deployment import make_bundle
from crucible.envs.fleetbase_dispatch.sim import WorldSimulator
from crucible.envs.fleetbase_dispatch.world import make_world
from crucible.export.deployment import DeploymentBundle


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_task
class FleetbaseDispatch(Task):
    spec = TaskSpec(
        id="fleetbase_dispatch",
        version=1,
        max_steps=128,
        title="Fleetbase Dispatch",
        description="Operate native Fleetbase orders against a deterministic delivery world.",
        keywords=("logistics", "fleetbase", "external-backend"),
        cpus=2,
        memory_mb=4096,
        timeout_sec=1800,
    )
    external_backend: ClassVar[bool] = True
    scoring_hash: ClassVar[str] = "fleetbase-physical-economics-v1"
    # Generic offline tests cannot start application services. Explicit simulator,
    # adapter and clean-deployment integration tests cover this environment.
    checks = TaskChecks(
        seeds=(),
        determinism=False,
        do_nothing_zero=False,
        reward_bounds=False,
        no_hidden_state=False,
        reference_solves=False,
        exploits_below_reference=False,
        oracle_perfect=False,
    )

    def __init__(self) -> None:
        self._runtime: HttpBackendRuntime | None = None
        self._adapter: FleetbaseAdapter | None = None
        self._sim: WorldSimulator | None = None
        self._score: Score | None = None
        self._ended = False
        self._evidence: dict[str, JsonValue] | None = None
        self._rejected_operations = 0
        self._episode_id = ""
        self._resources_closed = False
        self._cleanup_failed = False

    def instruction(self) -> str:
        return (
            "Manage customer requests using native Fleetbase orders. Observe released demand and "
            "vehicle positions. For each request create_order, assign_order to a suitable vehicle "
            "with its matching driver, then dispatch_order. Assignment alone does not dispatch. "
            "A vehicle can physically serve one order at a time. Capacity must cover the load. "
            "Reassignment before physical pickup is legal; cargo cannot move after pickup. "
            "Actions use no virtual time. advance(minutes) reconciles committed native work, "
            "moves vehicles at one distance unit per minute, and synchronizes verified progress. "
            "There are two demand waves and a 180-minute horizon. Public observations show only "
            "released requests. Value is earned only by physical delivery by the deadline. "
            "Driving costs 0.2 per distance unit, including unsuccessful trips. Reward is "
            "clip((earned value - driving cost) / total generated request value, 0, 1). "
            "finish, budget exhaustion, and policy stop continue already dispatched work to the "
            "horizon without creating or dispatching additional work. Rejected operations have "
            "no artificial reward penalty. Every successful tool returns the full observation. "
            "Use observe after a rejected or ambiguous operation before trying again. "
            "You have 128 tool calls, allowing three native mutations per order plus observation, "
            "time management and recovery. Do not claim completion from application status."
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="observe",
                description="Read released demand, physics and native orders.",
                kind="read",
                args_model=EmptyArgs,
            ),
            make_tool(
                "create_order",
                "Create one native order for a released request, idempotently.",
                "action",
                request_id=(str, ...),
            ),
            make_tool(
                "assign_order",
                "Assign its native driver and vehicle before physical pickup.",
                "action",
                request_id=(str, ...),
                vehicle_id=(str, ...),
            ),
            make_tool(
                "dispatch_order",
                "Dispatch a created, assigned native order.",
                "action",
                request_id=(str, ...),
            ),
            make_tool(
                "advance",
                "Reconcile native commitments, advance and settle physical events.",
                "clock",
                minutes=(float, Field(ge=0, le=180, allow_inf_nan=False)),
            ),
            ToolSpec(
                name="finish",
                description="Continue committed work to the horizon and stop.",
                kind="end",
                args_model=EmptyArgs,
            ),
        ]

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        if difficulty != 0:
            raise ValueError("Fleetbase currently supports difficulty=0 only")
        if self._runtime is not None:
            raise RuntimeError("Create a fresh task instance for every Fleetbase episode")
        url = os.environ.get("FLEETBASE_URL")
        token = os.environ.get("FLEETBASE_TOKEN")
        deployment = os.environ.get("FLEETBASE_DEPLOYMENT_ID")
        if not url or not token or not deployment:
            raise BackendError("Fleetbase requires private URL, token and deployment configuration")
        if os.environ.get("FLEETBASE_EXCLUSIVE") != "1":
            raise BackendError("Fleetbase requires an exclusively owned test installation")
        connection = BackendConnection(
            deployment_id=deployment,
            base_url=SecretStr(url),
            headers={
                "Authorization": SecretStr(f"Bearer {token}"),
                "Accept": SecretStr("application/json"),
            },
            ownership="managed" if os.environ.get("FLEETBASE_MANAGED") == "1" else "attached",
            exclusive_access=True,
        )
        self._runtime = HttpBackendRuntime(
            connection,
            ExclusiveLeaseProvider(
                Path(os.environ.get("FLEETBASE_LOCK_DIR", "/tmp/crucible-backend-leases"))
            ),
            readiness_path="/v1/orders",
            readiness_attempts=90,
            readiness_interval=1,
        )
        episode = await self._runtime.acquire()
        self._episode_id = episode.resource.episode_id
        self._sim = WorldSimulator(make_world(seed, salt))
        self._adapter = FleetbaseAdapter(episode)
        await self._adapter.prepare(self._sim.world.vehicles)
        return Observation(data=await self._observation())

    async def _observation(self) -> dict[str, JsonValue]:
        assert self._sim is not None and self._adapter is not None
        committed = await self._adapter.committed()
        self._sim.reconcile(committed)
        view = self._sim.observation()
        snapshot = await self._adapter.snapshot()
        orders = snapshot.get("orders", {})
        if not isinstance(orders, dict):
            raise BackendError("Invalid private native snapshot")
        view["orders"] = orders
        requests = view["requests"]
        if isinstance(requests, list):
            for request in requests:
                if not isinstance(request, dict):
                    continue
                request_id = str(request["id"])
                record = orders.get(request_id)
                if isinstance(record, dict):
                    request["native_id"] = record.get("id")
                native = committed.get(request_id, {})
                request["dispatched"] = native.get("dispatched", False)
        return view

    async def _advance(self, minutes: float) -> None:
        assert self._sim is not None and self._adapter is not None
        self._sim.reconcile(await self._adapter.committed())
        self._sim.advance(minutes)
        await self._adapter.sync_events(self._sim.events)

    async def step(self, call: ToolCall) -> ToolResult:
        if self.done():
            return ToolResult(ok=False, error="Episode is over")
        parsed = validate_call(tool_map(self.tools()), call)
        if isinstance(parsed, ToolResult):
            self._rejected_operations += 1
            return parsed
        assert self._sim is not None and self._adapter is not None
        args = parsed.model_dump()
        try:
            if call.name == "create_order":
                request = next(
                    (r for r in self._sim.world.requests if r.id == args["request_id"]), None
                )
                if request is None or request.release > self._sim.now:
                    self._rejected_operations += 1
                    return ToolResult(ok=False, error="Request is not released")
                await self._adapter.create_order(request)
            elif call.name == "assign_order":
                states = self._sim.observation()["requests"]
                if isinstance(states, list) and any(
                    isinstance(r, dict)
                    and r.get("id") == args["request_id"]
                    and r.get("status") in {"picked_up", "delivered", "late"}
                    for r in states
                ):
                    self._rejected_operations += 1
                    return ToolResult(ok=False, error="Physical pickup commitment is irreversible")
                await self._adapter.assign_order(str(args["request_id"]), str(args["vehicle_id"]))
            elif call.name == "dispatch_order":
                await self._adapter.dispatch_order(str(args["request_id"]))
            elif call.name == "advance":
                await self._advance(float(args["minutes"]))
            elif call.name == "finish":
                await self._advance(self._sim.world.horizon - self._sim.now)
                self._ended = True
            return ToolResult(ok=True, content=await self._observation())
        except FleetbaseOperationError:
            self._rejected_operations += 1
            return ToolResult(
                ok=False, error="Native operation rejected; observe committed state before retrying"
            )

    def done(self) -> bool:
        return self._ended or (self._sim is not None and self._sim.now >= self._sim.world.horizon)

    async def grade(self) -> Score:
        if self._score is not None:
            return self._score
        assert self._sim is not None and self._adapter is not None
        await self._advance(self._sim.world.horizon - self._sim.now)
        self._ended = True
        economics = self._sim.economics()
        snapshot = await self._adapter.snapshot()
        orders = snapshot.get("orders", {})
        if not isinstance(orders, dict):
            raise BackendError("Invalid private native snapshot")
        economics.update(
            {
                "uncreated_orders": float(len(self._sim.world.requests) - len(orders)),
                "undispatched_orders": float(
                    sum(
                        isinstance(order, dict) and order.get("dispatched") is not True
                        for order in orders.values()
                    )
                ),
                "rejected_operations": float(self._rejected_operations),
                "duplicate_orders": float(str(snapshot.get("duplicate_orders", 0))),
            }
        )
        self._evidence = {
            "events": [event.to_dict() for event in self._sim.events],
            "economics": dict(economics),
            "native_snapshot": snapshot,
            "world": json.loads(json.dumps(asdict(self._sim.world))),
            "rejected_operations": self._rejected_operations,
        }
        evidence_dir = os.environ.get("FLEETBASE_ARTIFACT_DIR")
        if evidence_dir:
            target = Path(evidence_dir) / self._episode_id
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / "fleetbase-evidence.json"
            path.write_text(json.dumps(self._evidence, sort_keys=True, allow_nan=False) + "\n")
            path.chmod(0o600)
        self._score = Score(total=economics["reward"], breakdown=economics)
        return self._score

    def grading_evidence(self) -> dict[str, JsonValue]:
        return copy.deepcopy(self._evidence or {})

    async def aclose(self) -> None:
        if self._resources_closed:
            if self._cleanup_failed:
                raise BackendError("Fleetbase resources could not be fully cleaned")
            return
        self._resources_closed = True
        try:
            if self._adapter is not None:
                await self._adapter.cleanup()
        except Exception:
            self._cleanup_failed = True
            raise
        finally:
            if self._runtime is not None:
                await self._runtime.aclose()

    def deployment_bundle(self) -> DeploymentBundle:
        return make_bundle()

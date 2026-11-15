"""Interactive UI workbench endpoints: human play, live agent runner, replay, and Harbor tailing."""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import mcp.types as mcp_types
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task, list_policies, list_tasks
from crucible.core.scoring import Score
from crucible.core.session import EpisodeSession
from crucible.core.task import Task
from crucible.core.tools import Observation, ToolCall, ToolKind, ToolResult, result_text
from crucible.core.transcript import StepRecord, StopReason, TranscriptWriter, now
from crucible.envs.oncall.world import FAULTS
from crucible.server.harbor_jobs import HarborJobs, LaunchHarborRequest
from crucible.server.inspector import build_fog_state, build_god_state
from crucible.server.settings import Settings
from crucible.server.sources import (
    list_harbor_containers,
    list_saved_runs,
    load_and_replay_file,
    run_baseline_comparison,
    serialize_steps,
    tail_harbor_container,
)

logger = logging.getLogger(__name__)


class WorkbenchToolInfo(BaseModel):
    name: str
    description: str
    kind: ToolKind
    input_schema: dict[str, JsonValue]


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = "oncall@1"
    seed: int = Field(default=0, ge=0)
    difficulty: int = Field(default=0, ge=0, le=1)
    mode: Literal["human", "policy", "mcp"] = "human"
    policy: str = "graph_walk"
    step_delay_ms: int = Field(default=450, ge=0, le=10000)
    auto_run: bool = True
    use_eval_salt: bool = False


class StepSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    include_god: bool = False
    reveal_secret: bool = False


class ControlSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "step_once", "set_speed", "close"]
    step_delay_ms: int | None = Field(default=None, ge=0, le=10000)
    include_god: bool = False
    reveal_secret: bool = False


ONCALL_COMMAND_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "get_services",
        "label": "Get Services Topology",
        "template": "kubectl get services",
        "cost": 1,
        "kind": "read",
        "needs_service": False,
        "needs_revision": False,
    },
    {
        "id": "get_pods",
        "label": "Get All Pods",
        "template": "kubectl get pods -n shop",
        "cost": 1,
        "kind": "read",
        "needs_service": False,
        "needs_revision": False,
    },
    {
        "id": "get_events",
        "label": "Get Pod Events",
        "template": "kubectl get events",
        "cost": 1,
        "kind": "read",
        "needs_service": False,
        "needs_revision": False,
    },
    {
        "id": "changes_list",
        "label": "List All Changes",
        "template": "changes list",
        "cost": 1,
        "kind": "read",
        "needs_service": False,
        "needs_revision": False,
    },
    {
        "id": "edges_list",
        "label": "List Current Edges",
        "template": "edges list",
        "cost": 1,
        "kind": "read",
        "needs_service": False,
        "needs_revision": False,
    },
    {
        "id": "rollout_history",
        "label": "Deploy Rollout History",
        "template": "kubectl rollout history deploy/{service}",
        "cost": 1,
        "kind": "read",
        "needs_service": True,
        "needs_revision": False,
    },
    {
        "id": "config_history",
        "label": "Service Config History",
        "template": "config history {service}",
        "cost": 1,
        "kind": "read",
        "needs_service": True,
        "needs_revision": False,
    },
    {
        "id": "rollout_undo_rev",
        "label": "Rollback Deploy Revision",
        "template": "kubectl rollout undo deploy/{service} --to-revision={revision}",
        "cost": 3,
        "kind": "action",
        "needs_service": True,
        "needs_revision": True,
    },
    {
        "id": "config_revert",
        "label": "Revert Config Change",
        "template": "config revert {service} {revision}",
        "cost": 3,
        "kind": "action",
        "needs_service": True,
        "needs_revision": True,
    },
    {
        "id": "rollout_restart",
        "label": "Rolling Restart Service",
        "template": "kubectl rollout restart deploy/{service}",
        "cost": 3,
        "kind": "action",
        "needs_service": True,
        "needs_revision": False,
    },
]


class WorkbenchEntry:
    """Owns one interactive or live-agent episode and its recorded steps."""

    def __init__(
        self,
        *,
        session_id: str,
        session: EpisodeSession,
        seed: int,
        difficulty: int,
        salt: str | None,
        mode: Literal["human", "policy", "mcp"],
        actor_label: str,
        instruction: str,
        first_observation: Observation,
        step_delay_ms: int,
        auto_run: bool,
    ) -> None:
        self.session_id = session_id
        self.session = session
        self.seed = seed
        self.difficulty = difficulty
        self.salt = salt
        self.mode = mode
        self.actor_label = actor_label
        self.instruction = instruction
        self.last_observation = first_observation
        self.step_delay_ms = step_delay_ms
        self.started_at = now()
        self.lock = asyncio.Lock()
        self.steps: list[StepRecord] = []
        self.thoughts: dict[int, str] = {}
        self.outcome: tuple[StopReason, Score] | None = None
        self.error: str | None = None
        self.paused = not auto_run
        self.resume_event = asyncio.Event()
        if auto_run:
            self.resume_event.set()
        self.single_step_budget = 0
        self.runner_task: asyncio.Task[None] | None = None
        self.mcp_app: StreamableHTTPASGIApp | None = None

    @property
    def status(self) -> str:
        if self.outcome is not None:
            return "closed"
        if self.session.closed:
            return "grading"
        if self.mode == "policy":
            return "paused" if self.paused else "running"
        return "waiting"

    async def execute_call(self, call: ToolCall, thought: str | None = None) -> ToolResult:
        async with self.lock:
            idx = len(self.steps)
            result = await self.session.step(call)
            if self.session.steps_used > idx:
                self.steps.append(StepRecord(step=idx, call=call, result=result))
                if thought:
                    self.thoughts[idx] = thought
                self.last_observation = Observation(text=result_text(result), last_result=result)
            if self.session.task.done() or self.session.steps_used >= self.session.max_steps:
                self.outcome = await self.session.close()
            return result

    async def finalize(self, error: str | None = None) -> tuple[StopReason, Score]:
        async with self.lock:
            if error is not None:
                self.error = error
            if self.outcome is None:
                self.outcome = await self.session.close(error)
            return self.outcome

    def serialize(
        self,
        *,
        include_god: bool = False,
        reveal_secret: bool = False,
        inspect_minute: int | None = None,
        up_to_step: int | None = None,
    ) -> dict[str, Any]:
        total = len(self.steps)
        limit = total if up_to_step is None else max(0, min(total, up_to_step))
        sliced = self.steps[:limit]
        serialized_steps = serialize_steps(sliced)
        for item in serialized_steps:
            st = item.get("step")
            if isinstance(st, int) and st in self.thoughts:
                item["thought"] = self.thoughts[st]

        all_serialized = serialize_steps(self.steps)
        for item in all_serialized:
            st = item.get("step")
            if isinstance(st, int) and st in self.thoughts:
                item["thought"] = self.thoughts[st]

        fog = build_fog_state(sliced)
        god = (
            build_god_state(
                self.session.task,
                inspect_minute=inspect_minute,
                reveal_secret=reveal_secret,
            )
            if include_god
            else None
        )
        outcome_dict: dict[str, Any] | None = None
        if self.outcome is not None:
            reason, score = self.outcome
            outcome_dict = {
                "stop_reason": reason,
                "score": score.model_dump(mode="json"),
                "solved": self.session.task.solved(score),
            }

        return {
            "session_id": self.session_id,
            "task": self.session.task.spec.ref,
            "seed": self.seed,
            "difficulty": self.difficulty,
            "salted": self.salt is not None,
            "mode": self.mode,
            "actor_label": self.actor_label,
            "instruction": self.instruction,
            "status": self.status,
            "paused": self.paused,
            "step_delay_ms": self.step_delay_ms,
            "steps_used": self.session.steps_used,
            "max_steps": self.session.max_steps,
            "selected_step": limit,
            "total_steps": total,
            "started_at": self.started_at,
            "error": self.error,
            "mcp_path": f"/ui/sessions/{self.session_id}/mcp",
            "steps": serialized_steps,
            "all_steps": all_serialized,
            "fog_state": fog,
            "god_state": god,
            "outcome": outcome_dict,
        }


class WorkbenchManager:
    """Lifecycle owner for interactive UI sessions, background policies, and MCP endpoints."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.entries: dict[str, WorkbenchEntry] = {}
        self._exit_stack = contextlib.AsyncExitStack()

    def get(self, session_id: str) -> WorkbenchEntry:
        entry = self.entries.get(session_id)
        if entry is None:
            raise HTTPException(404, detail="Unknown workbench session")
        return entry

    async def ensure_mcp_app(self, entry: WorkbenchEntry) -> StreamableHTTPASGIApp:
        if entry.mcp_app is not None:
            return entry.mcp_app
        mcp_server = _build_recorded_mcp_server(entry)
        manager = StreamableHTTPSessionManager(mcp_server, json_response=True, stateless=True)
        await self._exit_stack.enter_async_context(manager.run())
        entry.mcp_app = StreamableHTTPASGIApp(manager)
        return entry.mcp_app

    async def create(self, req: CreateSessionRequest) -> WorkbenchEntry:
        task = _lookup_task(req.task_id)
        salt: str | None = None
        if req.use_eval_salt:
            salt = os.environ.get("CRUCIBLE_EVAL_SALT")
            if not salt:
                raise HTTPException(
                    400, detail="CRUCIBLE_EVAL_SALT is not set in server environment"
                )

        actor_label = req.mode
        if req.mode == "policy":
            try:
                get_policy(req.policy, task.spec.ref)
            except KeyError as exc:
                raise HTTPException(404, detail=str(exc.args[0])) from None
            actor_label = req.policy
        elif req.mode == "human":
            actor_label = "human-operator"
        else:
            actor_label = "external-mcp"

        session_id = uuid4().hex[:16]
        path = self.settings.runs_dir / "workbench" / f"{session_id}.jsonl"
        ep_session = EpisodeSession(
            task,
            req.seed,
            TranscriptWriter(path),
            salt=salt,
            difficulty=req.difficulty,
        )
        job = Job(task=task.spec.ref, policy=actor_label, seed=req.seed, repeat=0)
        try:
            first_obs = await ep_session.start(policy_name=actor_label, job=job)
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await ep_session.close(type(exc).__name__)
            raise

        entry = WorkbenchEntry(
            session_id=session_id,
            session=ep_session,
            seed=req.seed,
            difficulty=req.difficulty,
            salt=salt,
            mode=req.mode,
            actor_label=actor_label,
            instruction=task.instruction(),
            first_observation=first_obs,
            step_delay_ms=req.step_delay_ms,
            auto_run=req.auto_run,
        )
        self.entries[session_id] = entry

        if req.mode == "mcp":
            await self.ensure_mcp_app(entry)
        elif req.mode == "policy":
            entry.runner_task = asyncio.create_task(self._run_policy_loop(entry, req.policy))
        return entry

    async def _wait_gate(self, entry: WorkbenchEntry) -> None:
        while entry.paused and entry.single_step_budget <= 0:
            entry.resume_event.clear()
            await entry.resume_event.wait()
        if entry.single_step_budget > 0:
            entry.single_step_budget -= 1
            if entry.single_step_budget == 0 and entry.paused:
                entry.resume_event.clear()

    async def _run_policy_loop(self, entry: WorkbenchEntry, policy_name: str) -> None:
        task = entry.session.task
        try:
            policy = get_policy(policy_name, task.spec.ref)
            await policy.reset(task.spec, task.tools(), entry.instruction)
            while not task.done() and entry.session.steps_used < entry.session.max_steps:
                await self._wait_gate(entry)
                if entry.session.closed:
                    return
                call = await policy.act(entry.last_observation)
                if call is None:
                    break
                await entry.execute_call(call)
                if entry.step_delay_ms > 0 and not task.done():
                    await asyncio.sleep(entry.step_delay_ms / 1000.0)
            await entry.finalize()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await entry.finalize("CancelledError")
            raise
        except Exception as exc:
            logger.exception("Workbench policy loop failed")
            await entry.finalize(f"{type(exc).__name__}: {exc}")

    async def aclose(self) -> None:
        for entry in list(self.entries.values()):
            if entry.runner_task is not None and not entry.runner_task.done():
                entry.runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.runner_task
            with contextlib.suppress(Exception):
                await entry.finalize()
        await self._exit_stack.aclose()


def _build_recorded_mcp_server(entry: WorkbenchEntry) -> Server[dict[str, Any]]:
    """MCP server whose tool calls route through `entry.execute_call` so the UI sees live steps."""
    specs = entry.session.task.tools()

    async def list_tools(
        ctx: ServerRequestContext[dict[str, Any]],
        params: mcp_types.PaginatedRequestParams | None,
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(name=s.name, description=s.description, input_schema=s.json_schema())
                for s in specs
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext[dict[str, Any]], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        args: dict[str, JsonValue] = dict(params.arguments or {})
        result = await entry.execute_call(ToolCall(name=params.name, args=args))
        dumped = result.model_dump(mode="json")
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=json.dumps(dumped, sort_keys=True))],
            structured_content=dumped,
            is_error=not result.ok,
        )

    return Server(
        "crucible-workbench",
        instructions=entry.instruction,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _lookup_task(task_ref: str) -> Task:
    try:
        return get_task(task_ref)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc.args[0])) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


def _allowed_replay_path(target: Path, extra_dirs: list[Path]) -> Path:
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    cwd = Path.cwd().resolve()
    allowed_roots = {
        cwd,
        cwd.parent.resolve(),
        *(d.resolve() for d in extra_dirs),
    }
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(403, detail="Path is outside workspace and runs directories")
    return resolved


def _find_web_dist() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "web" / "dist",
        Path.cwd().parent / "web" / "dist",
        here.parents[4] / "web" / "dist" if len(here.parents) > 4 else Path("/nonexistent"),
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def add_workbench_routes(app: FastAPI, settings: Settings) -> WorkbenchManager:
    """Attach `/ui/*` API routes, local Vite CORS, and built SPA static files if present."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    manager = WorkbenchManager(settings)
    jobs = HarborJobs(settings)
    extra_dirs = [settings.runs_dir, settings.harbor_jobs_dir, settings.harbor_exports_dir]

    class SessionMcpASGI:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            req = Request(scope, receive)
            session_id = str(req.path_params.get("session_id", ""))
            entry = manager.entries.get(session_id)
            if entry is None:
                resp = Response("Unknown session", status_code=404)
                await resp(scope, receive, send)
                return
            mcp_asgi = await manager.ensure_mcp_app(entry)
            await mcp_asgi(scope, receive, send)

    app.router.routes.append(Route("/ui/sessions/{session_id}/mcp", SessionMcpASGI()))

    @app.get("/ui/meta")
    async def ui_meta() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tasks = list_tasks()
        policies_by_task: dict[str, list[str]] = {}
        tools_by_task: dict[str, list[dict[str, Any]]] = {}
        for ref in tasks:
            t = get_task(ref)
            policies_by_task[ref] = list(list_policies(ref))
            tools_by_task[ref] = [
                WorkbenchToolInfo(
                    name=spec.name,
                    description=spec.description,
                    kind=spec.kind,
                    input_schema=spec.json_schema(),
                ).model_dump(mode="json")
                for spec in t.tools()
            ]
        return {
            "tasks": list(tasks),
            "default_task": "oncall@1" if "oncall@1" in tasks else (tasks[0] if tasks else ""),
            "policies": policies_by_task,
            "tools": tools_by_task,
            "fault_kinds": list(FAULTS),
            "remediations": ["rollback", "config_revert", "restart"],
            "command_templates": ONCALL_COMMAND_TEMPLATES,
            "eval_salt_configured": bool(os.environ.get("CRUCIBLE_EVAL_SALT")),
        }

    @app.get("/ui/sessions")
    async def ui_list_sessions() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        rows: list[dict[str, Any]] = []
        for entry in reversed(list(manager.entries.values())):
            score_val = entry.outcome[1].total if entry.outcome is not None else None
            rows.append(
                {
                    "session_id": entry.session_id,
                    "task": entry.session.task.spec.ref,
                    "seed": entry.seed,
                    "difficulty": entry.difficulty,
                    "mode": entry.mode,
                    "actor_label": entry.actor_label,
                    "status": entry.status,
                    "steps_used": entry.session.steps_used,
                    "max_steps": entry.session.max_steps,
                    "score": round(score_val, 4) if score_val is not None else None,
                    "started_at": entry.started_at,
                }
            )
        return rows

    @app.post("/ui/sessions")
    async def ui_create_session(  # pyright: ignore[reportUnusedFunction]
        body: CreateSessionRequest,
        include_god: bool = False,
        reveal_secret: bool = False,
    ) -> dict[str, Any]:
        entry = await manager.create(body)
        god_default = include_god or body.mode != "human"
        return entry.serialize(include_god=god_default, reveal_secret=reveal_secret)

    @app.get("/ui/sessions/{session_id}")
    async def ui_get_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        include_god: bool = False,
        reveal_secret: bool = False,
        inspect_minute: int | None = None,
        up_to_step: int | None = None,
    ) -> dict[str, Any]:
        entry = manager.get(session_id)
        return entry.serialize(
            include_god=include_god,
            reveal_secret=reveal_secret,
            inspect_minute=inspect_minute,
            up_to_step=up_to_step,
        )

    @app.post("/ui/sessions/{session_id}/step")
    async def ui_step_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        body: StepSessionRequest,
    ) -> dict[str, Any]:
        entry = manager.get(session_id)
        result = await entry.execute_call(ToolCall(name=body.name, args=body.args))
        return {
            "result": result.model_dump(mode="json"),
            "session": entry.serialize(
                include_god=body.include_god, reveal_secret=body.reveal_secret
            ),
        }

    @app.post("/ui/sessions/{session_id}/control")
    async def ui_control_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        body: ControlSessionRequest,
    ) -> dict[str, Any]:
        entry = manager.get(session_id)
        if body.step_delay_ms is not None:
            entry.step_delay_ms = body.step_delay_ms
        if body.action == "pause":
            entry.paused = True
            entry.resume_event.clear()
        elif body.action == "resume":
            entry.paused = False
            entry.resume_event.set()
        elif body.action == "step_once":
            entry.paused = True
            entry.single_step_budget += 1
            entry.resume_event.set()
        elif body.action == "close":
            if entry.runner_task is not None and not entry.runner_task.done():
                entry.runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.runner_task
            await entry.finalize()
        return entry.serialize(include_god=body.include_god, reveal_secret=body.reveal_secret)

    @app.get("/ui/runs")
    async def ui_list_runs() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        return await list_saved_runs([settings.harbor_jobs_dir], harbor_only=True)

    @app.get("/ui/runs/replay")
    async def ui_replay_run(  # pyright: ignore[reportUnusedFunction]
        path: str = Query(..., min_length=1),
        export_path: str | None = None,
        up_to_step: int | None = None,
        inspect_minute: int | None = None,
        reveal_secret: bool = False,
    ) -> dict[str, Any]:
        safe_path = _allowed_replay_path(Path(path), extra_dirs)
        safe_export = _allowed_replay_path(Path(export_path), extra_dirs) if export_path else None
        try:
            return await load_and_replay_file(
                safe_path,
                extra_dirs=extra_dirs,
                explicit_export=safe_export,
                up_to_step=up_to_step,
                inspect_minute=inspect_minute,
                reveal_secret=reveal_secret,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None

    @app.get("/ui/harbor/jobs")
    async def ui_harbor_jobs() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await asyncio.to_thread(jobs.describe)

    @app.post("/ui/harbor/jobs")
    async def ui_launch_harbor(body: LaunchHarborRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await jobs.launch(body)

    @app.get("/ui/harbor/containers")
    async def ui_harbor_containers() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await list_harbor_containers()

    @app.get("/ui/harbor/containers/{container_id}")
    async def ui_harbor_container_state(  # pyright: ignore[reportUnusedFunction]
        container_id: str,
        up_to_step: int | None = None,
        inspect_minute: int | None = None,
        reveal_secret: bool = False,
    ) -> dict[str, Any]:
        try:
            return await tail_harbor_container(
                container_id,
                up_to_step=up_to_step,
                inspect_minute=inspect_minute,
                reveal_secret=reveal_secret,
            )
        except ValueError as exc:
            raise HTTPException(404, detail=str(exc)) from None

    @app.get("/ui/compare")
    async def ui_compare_policy(  # pyright: ignore[reportUnusedFunction]
        task: str = "oncall@1",
        seed: int = 0,
        difficulty: int = Query(default=0, ge=0, le=1),
        policy: str = "graph_walk",
        session_id: str | None = None,
        up_to_step: int | None = None,
        inspect_minute: int | None = None,
        reveal_secret: bool = False,
    ) -> dict[str, Any]:
        _lookup_task(task)
        try:
            get_policy(policy, task)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc.args[0])) from None

        salt: str | None = None
        if session_id and session_id in manager.entries:
            salt = manager.entries[session_id].salt
        return await run_baseline_comparison(
            task,
            seed,
            difficulty=difficulty,
            policy_name=policy,
            salt=salt,
            up_to_step=up_to_step,
            inspect_minute=inspect_minute,
            reveal_secret=reveal_secret,
        )

    dist = _find_web_dist()
    if dist is not None:
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

        @app.get("/", include_in_schema=False)
        async def ui_index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
            return FileResponse(dist / "index.html")

    return manager


__all__ = ["WorkbenchManager", "add_workbench_routes"]

"""The FastAPI app factory and the HTTP v1 API."""

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.routing import Route

from crucible.core.jobs import Job
from crucible.core.registry import get_task, list_tasks
from crucible.core.scoring import Score
from crucible.core.session import EpisodeSession
from crucible.core.task import Task
from crucible.core.tools import Observation, ToolCall, ToolKind, ToolResult
from crucible.core.transcript import StopReason, TranscriptWriter, read_transcript
from crucible.server.logging import configure_logging
from crucible.server.mcp import build_mcp_server
from crucible.server.settings import Settings
from crucible.server.spectator import Spectator
from crucible.server.store import Entry, InMemorySessionStore, SessionStore


class ToolInfo(BaseModel):
    name: str
    description: str
    kind: ToolKind
    input_schema: dict[str, JsonValue]


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    seed: int
    difficulty: int = Field(default=0, ge=0, le=0)


class StartResponse(BaseModel):
    episode_id: str
    instruction: str
    observation: Observation
    max_steps: int


class CloseResponse(BaseModel):
    score: Score
    steps: int
    stop_reason: StopReason


def _lookup(task_ref: str) -> Task:
    try:
        return get_task(task_ref)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc.args[0])) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


def _add_api_routes(
    app: FastAPI, settings: Settings, sessions: SessionStore, logger: logging.Logger
) -> None:
    """The `/v1` routes (api mode)."""

    async def entry_for(episode_id: str) -> Entry:
        await sessions.expire()
        entry = sessions.get(episode_id)
        if entry is None:
            raise HTTPException(404, detail="Unknown episode")
        return entry

    @app.get("/v1/tasks")
    async def tasks() -> list[str]:  # pyright: ignore[reportUnusedFunction]
        return list_tasks()

    @app.get("/v1/tasks/{task_id}/tools")
    async def tools(task_id: str) -> list[ToolInfo]:  # pyright: ignore[reportUnusedFunction]
        return [
            ToolInfo(
                name=t.name,
                description=t.description,
                kind=t.kind,
                input_schema=t.json_schema(),
            )
            for t in _lookup(task_id).tools()
        ]

    @app.post("/v1/episodes")
    async def start(body: StartRequest) -> StartResponse:  # pyright: ignore[reportUnusedFunction]
        task = _lookup(body.task_id)
        episode_id = uuid4().hex
        path: Path = settings.runs_dir / "episodes" / f"{episode_id}.jsonl"
        session = EpisodeSession(task, body.seed, TranscriptWriter(path))
        job = Job(task=task.spec.ref, policy="external", seed=body.seed, repeat=0)
        try:
            observation = await session.start(policy_name="external", job=job)
            instruction = task.instruction()
            sessions.put(episode_id, Entry(session))
        except BaseException as exc:
            try:
                await session.close(type(exc).__name__)
            except Exception:
                logger.exception("Failed to finalize episode after startup failure")
            raise
        logger.info("episode started", extra={"episode_id": episode_id})
        return StartResponse(
            episode_id=episode_id,
            instruction=instruction,
            observation=observation,
            max_steps=session.max_steps,
        )

    @app.post("/v1/episodes/{episode_id}/step")
    async def step(episode_id: str, call: ToolCall) -> ToolResult:  # pyright: ignore[reportUnusedFunction]
        entry = await entry_for(episode_id)
        async with entry.lock:
            result = await entry.session.step(call)
        logger.info("step", extra={"episode_id": episode_id, "tool": call.name, "ok": result.ok})
        return result

    @app.post("/v1/episodes/{episode_id}/close")
    async def close(episode_id: str) -> CloseResponse:  # pyright: ignore[reportUnusedFunction]
        entry = await entry_for(episode_id)
        async with entry.lock:
            reason, score = await entry.session.close()
            steps = entry.session.steps_used
        logger.info("episode closed", extra={"episode_id": episode_id})
        return CloseResponse(score=score, steps=steps, stop_reason=reason)


class GradeResponse(BaseModel):
    task: str
    seed: int
    reward: float
    breakdown: dict[str, float]
    flags: dict[str, bool]
    steps: int
    stop_reason: StopReason
    transcript: list[dict[str, JsonValue]]
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    cleanup_errors: list[str] = Field(default_factory=list)


def _add_sidecar_routes(app: FastAPI, settings: Settings, logger: logging.Logger) -> None:
    """`/mcp` and `/grade` for the one episode of `settings.sidecar_task` (sidecar mode)."""
    task = _lookup(settings.sidecar_task)
    seed = settings.sidecar_seed
    path = settings.runs_dir / "episodes" / f"sidecar-{task.spec.ref}-{seed}.jsonl"
    session = EpisodeSession(task, seed, TranscriptWriter(path), salt=settings.sidecar_salt)
    lock = asyncio.Lock()
    spectator = (
        Spectator(
            settings.spectator_path,
            task,
            seed,
            settings.sidecar_salt or "",
            settings.spectator_run_id,
            settings.spectator_strategy,
        )
        if settings.spectator_path
        else None
    )
    app.state.spectator = spectator
    manager = StreamableHTTPSessionManager(
        build_mcp_server(session, lock, spectator), json_response=True, stateless=True
    )
    app.state.sidecar_session = session
    app.state.mcp_manager = manager
    app.router.routes.append(Route("/mcp", StreamableHTTPASGIApp(manager)))

    @app.get("/grade", response_model=GradeResponse)
    async def grade(  # pyright: ignore[reportUnusedFunction]
        x_grade_token: Annotated[str | None, Header()] = None,
    ) -> Response:
        if x_grade_token is None or not hmac.compare_digest(
            x_grade_token.encode(), settings.grade_token.encode()
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        async with lock:
            reason, score = await session.close()
            steps = session.steps_used
            if spectator:
                spectator.finalize(reason, score)
        transcript = read_transcript(path)
        records = [transcript.header, *transcript.steps, transcript.footer]
        logger.info("episode graded")
        return JSONResponse(
            GradeResponse(
                task=task.spec.ref,
                seed=seed,
                reward=score.total,
                breakdown=score.breakdown,
                flags=score.flags,
                steps=steps,
                stop_reason=reason,
                transcript=[r.model_dump(mode="json") for r in records],
                evidence=task.grading_evidence(),
                cleanup_errors=list(session.cleanup_errors),
            ).model_dump(mode="json")
        )


def create_app(settings: Settings | None = None, store: SessionStore | None = None) -> FastAPI:
    """Build the app. `store` lets tests inject a store with a fake clock.

    Api mode serves `/v1`. Sidecar mode serves one episode on `/mcp` and `/grade`, no `/v1`.
    """
    settings = settings or Settings()
    logger = configure_logging(settings.log_level)
    if settings.mode == "sidecar" and not settings.grade_token:
        raise ValueError("GRADE_TOKEN is required in sidecar mode")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if settings.mode != "sidecar":

            async def sweep() -> None:
                while True:
                    await asyncio.sleep(min(30.0, settings.session_ttl_seconds / 2))
                    await sessions.expire()

            sweeper = asyncio.create_task(sweep())
            try:
                yield
            finally:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
                await sessions.aclose()
            return
        session: EpisodeSession = app.state.sidecar_session
        manager: StreamableHTTPSessionManager = app.state.mcp_manager
        job = Job(task=session.task.spec.ref, policy="external", seed=settings.sidecar_seed)
        error: str | None = None
        try:
            await session.start(policy_name="external", job=job)
            async with manager.run():
                yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            # Drop a half-written transcript if nobody graded the episode.
            if not session.closed:
                reason, score = await session.close()
                if app.state.spectator:
                    app.state.spectator.finalize(reason, score)

    app = FastAPI(
        title="Crucible",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:  # pyright: ignore[reportUnusedFunction]
        return "ok"

    if settings.mode == "sidecar":
        _add_sidecar_routes(app, settings, logger)
    else:
        sessions: SessionStore = store or InMemorySessionStore(settings.session_ttl_seconds)
        _add_api_routes(app, settings, sessions, logger)
    return app


__all__ = ["create_app"]

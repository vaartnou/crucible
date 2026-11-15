"""One physically verified delivery through a separate HTTP application fixture."""

import json
import os
import secrets
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from crucible.backends import (
    BackendConnection,
    BackendEpisode,
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
    tool_map,
    validate_call,
)
from crucible.envs.backend_probe.app import Record
from crucible.export.deployment import DeploymentBundle


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdvanceArgs(NoArgs):
    minutes: int = Field(ge=1, le=10)


_TOOLS = [
    ToolSpec(
        name="inspect", description="Read committed delivery state.", kind="read", args_model=NoArgs
    ),
    ToolSpec(
        name="dispatch",
        description="Dispatch delivery-1 idempotently.",
        kind="action",
        args_model=NoArgs,
    ),
    ToolSpec(
        name="advance",
        description="Advance virtual movement, one distance unit per minute.",
        kind="clock",
        args_model=AdvanceArgs,
    ),
    ToolSpec(
        name="finish",
        description="Stop now; unfinished work does not continue after grading.",
        kind="end",
        args_model=NoArgs,
    ),
]


@register_task
class BackendProbe(Task):
    """Infrastructure fixture: fixed 10-unit trip, 100 value and 1/unit driving cost."""

    external_backend: ClassVar[bool] = True
    spec: ClassVar[TaskSpec] = TaskSpec(
        id="backend_probe",
        version=1,
        max_steps=8,
        title="External backend delivery proof",
        description="Infrastructure fixture proving isolated application-backed episodes.",
        keywords=("backend", "fixture"),
    )
    # Generic checks run without external services. Explicit integration tests below
    # verify the same properties against a real process instead of silently mocking it.
    checks: ClassVar[TaskChecks] = TaskChecks(
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
        self._episode: BackendEpisode | None = None
        self._minute = 0
        self._distance = 0
        self._verified = False
        self._finished = False
        self._score: Score | None = None
        self._snapshot = Record()
        self._evidence: tempfile.TemporaryDirectory[str] | None = None

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        if self._runtime is not None:
            raise RuntimeError("A probe instance is single-use")
        url = os.environ.get("BACKEND_PROBE_URL", "")
        token = os.environ.get("BACKEND_PROBE_TOKEN", "")
        if not url or not token:
            raise RuntimeError(
                "Start an isolated backend probe and configure its private connection"
            )
        connection = BackendConnection(
            deployment_id=os.environ.get("BACKEND_PROBE_DEPLOYMENT", url),
            base_url=SecretStr(url),
            headers={"X-Backend-Token": SecretStr(token)},
            ownership="attached",
            exclusive_access=True,
        )

        async def prepare(episode: BackendEpisode) -> None:
            response = await episode.client.put(f"episodes/{episode.resource.episode_id}")
            response.raise_for_status()

        async def release(episode: BackendEpisode) -> None:
            response = await episode.client.delete(f"episodes/{episode.resource.episode_id}")
            response.raise_for_status()

        async def artifacts(episode: BackendEpisode) -> dict[str, bytes]:
            response = await episode.client.get(f"episodes/{episode.resource.episode_id}")
            response.raise_for_status()
            return {
                "application.json": response.content,
                "verified-events.json": json.dumps(self._events()).encode(),
            }

        self._runtime = HttpBackendRuntime(
            connection,
            ExclusiveLeaseProvider(
                Path(os.environ.get("BACKEND_PROBE_LOCK_DIR", "/tmp/crucible-backend-leases"))
            ),
            prepare=prepare,
            release=release,
            artifacts=artifacts,
        )
        self._episode = await self._runtime.acquire()
        return Observation(text=self.instruction(), data=self._view())

    def tools(self) -> list[ToolSpec]:
        return list(_TOOLS)

    async def _read(self) -> Record:
        if self._episode is None:
            raise RuntimeError("Probe has no episode")
        response = await self._episode.client.get(f"episodes/{self._episode.resource.episode_id}")
        response.raise_for_status()
        self._snapshot = Record.model_validate(response.json())
        return self._snapshot

    def _view(self) -> dict[str, JsonValue]:
        return {
            "request_id": "delivery-1",
            "status": self._snapshot.status,
            "minute": self._minute,
            "distance": self._distance,
            "verified_arrival": self._verified,
        }

    async def step(self, call: ToolCall) -> ToolResult:
        if self.done():
            return ToolResult(ok=False, error="Episode is over")
        parsed = validate_call(tool_map(_TOOLS), call)
        if isinstance(parsed, ToolResult):
            return parsed
        if self._episode is None:
            raise RuntimeError("Probe has no episode")
        base = f"episodes/{self._episode.resource.episode_id}"
        if call.name == "finish":
            self._finished = True
        elif call.name == "dispatch":
            # Read-back before retry after ambiguous timeouts. The application
            # additionally treats this stable request ID as idempotent.
            if (await self._read()).status == "available":
                try:
                    response = await self._episode.client.post(f"{base}/dispatch")
                    response.raise_for_status()
                except httpx.TimeoutException:
                    if (await self._read()).status == "available":
                        raise
        elif isinstance(parsed, AdvanceArgs):
            record = await self._read()
            self._minute += parsed.minutes
            if record.status == "dispatched" and not self._verified:
                self._distance = min(10, self._distance + parsed.minutes)
                if self._distance == 10:
                    self._verified = True
            # Synchronization is retried on later advances without duplicating the
            # physical event. Application status alone cannot set _verified.
            if self._verified:
                response = await self._episode.client.put(
                    f"{base}/progress", json={"status": "delivered"}
                )
                response.raise_for_status()
        await self._read()
        return ToolResult(ok=True, content=self._view())

    def done(self) -> bool:
        return self._finished

    def _events(self) -> list[dict[str, str | int]]:
        return (
            [{"request_id": "delivery-1", "kind": "arrival", "distance": 10}]
            if self._verified
            else []
        )

    async def grade(self) -> Score:
        if self._score is None:
            utility = (100 if self._verified else 0) - self._distance
            self._score = Score(
                total=max(0, min(1, utility / 100)),
                breakdown={
                    "utility": float(utility),
                    "distance": float(self._distance),
                    "deliveries": float(self._verified),
                },
            )
            if self._runtime is not None and self._episode is not None:
                # Evidence precedes resource release and is never exposed to the agent.
                evidence = os.environ.get("BACKEND_PROBE_ARTIFACTS")
                if evidence:
                    target = Path(evidence) / self._episode.resource.episode_id
                else:
                    self._evidence = tempfile.TemporaryDirectory(prefix="crucible-probe-evidence-")
                    target = Path(self._evidence.name)
                await self._runtime.collect_artifacts(target)
        return self._score

    async def aclose(self) -> None:
        if self._runtime is not None:
            await self._runtime.aclose()
        if self._evidence is not None:
            self._evidence.cleanup()

    def grading_evidence(self) -> dict[str, JsonValue]:
        return {
            "verified_events": [dict(event) for event in self._events()],
            "application": self._snapshot.model_dump(mode="json"),
            "minute": self._minute,
            "distance": self._distance,
            "infrastructure_fixture": True,
        }

    def instruction(self) -> str:
        return (
            "Deliver request delivery-1: value 100, destination 10 distance units away. "
            "Dispatch the application record, advance 10 virtual minutes, inspect the verified "
            "arrival, then finish. Travel costs 1 per distance unit. Only physical arrival earns "
            "value; application status does not. Stopping freezes movement immediately. "
            "This is an infrastructure proof, not a calibrated benchmark."
        )

    def deployment_bundle(self) -> DeploymentBundle:
        token = secrets.token_hex(32)
        assets = files("crucible.envs.backend_probe") / "assets"
        return DeploymentBundle(
            services={
                "probe-app": {
                    "build": {"context": "."},
                    "environment": {"BACKEND_PROBE_TOKEN": token},
                    "healthcheck": {
                        "test": [
                            "CMD",
                            "python",
                            "-c",
                            "import urllib.request; "
                            "urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)",
                        ],
                        "interval": "2s",
                        "timeout": "3s",
                        "retries": 30,
                    },
                    "deploy": {"resources": {"limits": {"cpus": "0.5", "memory": "256M"}}},
                }
            },
            files={
                "Dockerfile": (assets / "Dockerfile").read_bytes(),
                "requirements.txt": (assets / "requirements.txt").read_bytes(),
                "app.py": (files("crucible.envs.backend_probe") / "app.py").read_bytes(),
            },
            sidecar_environment={
                "BACKEND_PROBE_URL": "http://probe-app:8000",
                "BACKEND_PROBE_TOKEN": token,
                "BACKEND_PROBE_ARTIFACTS": "/tmp/runs/backend-evidence",
            },
            readiness_services=("probe-app",),
            source_fingerprints={
                "fixture": "backend_probe@1",
                "python": "sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b",
            },
        )

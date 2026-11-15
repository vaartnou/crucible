"""Actual localhost HTTP proof; no in-memory transport or fake backend adapter."""

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from crucible.backends import LeaseConflict
from crucible.core.episode import run_episode
from crucible.core.jobs import Job
from crucible.core.scoring import Score
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter
from crucible.envs.backend_probe.app import create_app
from crucible.envs.backend_probe.policies.reference import Reference
from crucible.envs.backend_probe.task import BackendProbe
from crucible.policies.baselines import DoNothing


@asynccontextmanager
async def live_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[str]:
    monkeypatch.setenv("BACKEND_PROBE_TOKEN", "private-fixture-token")
    monkeypatch.setenv("BACKEND_PROBE_LOCK_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("BACKEND_PROBE_ARTIFACTS", str(tmp_path / "evidence"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("BACKEND_PROBE_URL", url)
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield url
    finally:
        server.should_exit = True
        async with asyncio.timeout(10):
            await serving


async def play(task: BackendProbe, path: Path, *, noop: bool = False) -> Score:
    policy = DoNothing() if noop else Reference()
    result = await run_episode(
        task,
        policy,
        0,
        job=Job(task="backend_probe@1", policy=policy.name, seed=0),
        writer=TranscriptWriter(path),
    )
    assert result.stop_reason != "error"
    return result.score


async def test_complete_delivery_and_private_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with live_probe(tmp_path, monkeypatch) as url:
        task = BackendProbe()
        score = await play(task, tmp_path / "episode.jsonl")
        assert score.total == 0.9
        assert score.breakdown == {"utility": 90.0, "distance": 10.0, "deliveries": 1.0}
        assert task.grading_evidence()["verified_events"] == [
            {"request_id": "delivery-1", "kind": "arrival", "distance": 10}
        ]
        evidence = list((tmp_path / "evidence").glob("*/application.json"))
        assert len(evidence) == 1
        assert json.loads(evidence[0].read_text())["status"] == "delivered"
        episode_id = evidence[0].parent.name
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            assert (await client.get("/healthz")).is_success
            assert (await client.get(f"/episodes/{episode_id}")).status_code == 403
            assert (
                await client.get(
                    f"/episodes/{episode_id}", headers={"X-Backend-Token": "private-fixture-token"}
                )
            ).status_code == 404  # Only the episode record was removed; service is alive.
        transcript = (tmp_path / "episode.jsonl").read_text()
        assert "private-fixture-token" not in transcript
        assert url not in transcript


async def test_reset_replay_noop_and_duplicate_commitments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with live_probe(tmp_path, monkeypatch):
        first, second = BackendProbe(), BackendProbe()
        assert await play(first, tmp_path / "first.jsonl") == await play(
            second, tmp_path / "second.jsonl"
        )
        assert first.grading_evidence() == second.grading_evidence()
        assert (await play(BackendProbe(), tmp_path / "noop.jsonl", noop=True)).total == 0
        task = BackendProbe()
        await task.reset(0)
        try:
            for name in ["dispatch", "dispatch"]:
                assert (await task.step(ToolCall(name=name))).ok
            for _ in range(2):
                assert (await task.step(ToolCall(name="advance", args={"minutes": 10}))).ok
            assert (await task.grade()).breakdown["deliveries"] == 1
            events = task.grading_evidence()["verified_events"]
            assert isinstance(events, list) and len(events) == 1
        finally:
            await task.aclose()


async def test_conflicting_episode_is_rejected_before_record_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with live_probe(tmp_path, monkeypatch):
        first, conflict = BackendProbe(), BackendProbe()
        await first.reset(0)
        try:
            with pytest.raises(LeaseConflict):
                await conflict.reset(0)
        finally:
            await conflict.aclose()
            await first.aclose()
        assert (await play(BackendProbe(), tmp_path / "after-release.jsonl")).total == 0.9


def test_missing_connection_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_PROBE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PROBE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="configure its private connection"):
        asyncio.run(BackendProbe().reset(0))


async def test_application_status_cannot_forge_physical_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with live_probe(tmp_path, monkeypatch) as url:
        task = BackendProbe()
        await task.reset(0)
        try:
            assert (await task.step(ToolCall(name="dispatch"))).ok
            episode = task._episode  # pyright: ignore[reportPrivateUsage]
            assert episode is not None
            # Deliberately corrupt application status through the private API.
            # The real agent has neither this credential nor this tool.
            async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                response = await client.put(
                    f"/episodes/{episode.resource.episode_id}/progress",
                    headers={"X-Backend-Token": "private-fixture-token"},
                    json={"status": "delivered"},
                )
                assert response.is_success
            result = await task.step(ToolCall(name="inspect"))
            assert isinstance(result.content, dict)
            assert result.content["status"] == "delivered"
            assert (await task.grade()).total == 0
            assert task.grading_evidence()["verified_events"] == []
        finally:
            await task.aclose()

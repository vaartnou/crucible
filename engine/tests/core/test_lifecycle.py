"""Resource lifecycle regressions; these tasks model real application resource owners."""

import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest

from crucible.core.episode import run_episode
from crucible.core.jobs import Job
from crucible.core.scoring import Score
from crucible.core.session import EpisodeSession
from crucible.core.task import Task, TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec
from crucible.core.transcript import TranscriptWriter, read_transcript
from crucible.policies.baselines import DoNothing
from crucible.server.store import Entry, InMemorySessionStore


class ResourceTask(Task):
    spec: ClassVar[TaskSpec] = TaskSpec(
        id="resource", version=1, max_steps=2, title="Resource", description="Lifecycle fixture"
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.release_count = 0
        self.grade_count = 0
        self.grade_started = asyncio.Event()
        self.fail_reset = False
        self.fail_grade = False
        self.fail_close = False
        self.started = asyncio.Event()
        self.continue_reset = asyncio.Event()
        self.continue_reset.set()
        self.continue_grade = asyncio.Event()
        self.continue_grade.set()
        self.order: list[str] = []

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        self.started.set()
        await self.continue_reset.wait()
        if self.fail_reset:
            raise ValueError("reset failed")
        return Observation()

    def tools(self) -> list[ToolSpec]:
        return []

    async def step(self, call: ToolCall) -> ToolResult:
        return ToolResult(ok=True)

    def done(self) -> bool:
        return False

    async def grade(self) -> Score:
        self.order.append("grade")
        self.grade_count += 1
        self.grade_started.set()
        await self.continue_grade.wait()
        if self.fail_grade:
            raise RuntimeError("grade failed")
        return Score(total=0.5)

    async def aclose(self) -> None:
        # The transcript is finalized before application resources disappear.
        assert read_transcript(self.path).footer is not None
        self.order.append("release")
        self.release_count += 1
        if self.fail_close:
            raise OSError("release failed")

    def instruction(self) -> str:
        return ""


def make_session(tmp_path: Path) -> tuple[ResourceTask, EpisodeSession, Job]:
    path = tmp_path / "resource.jsonl"
    task = ResourceTask(path)
    session = EpisodeSession(task, 1, TranscriptWriter(path))
    return task, session, Job(task="resource@1", policy="do_nothing", seed=1)


async def test_concurrent_close_freezes_once_and_stops_actions(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    await session.start(policy_name="external", job=job)
    task.continue_grade.clear()
    first = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert session.closed
    assert not (await session.step(ToolCall(name="anything"))).ok
    second = asyncio.create_task(session.close())
    task.continue_grade.set()
    assert await first == await second == await session.close()
    assert task.grade_count == task.release_count == 1
    assert task.order == ["grade", "release"]


async def test_failed_reset_releases_without_grading(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    task.fail_reset = True
    with pytest.raises(ValueError, match="reset failed"):
        await session.start(policy_name="external", job=job)
    assert task.grade_count == 0
    assert task.release_count == 1
    assert read_transcript(task.path).footer.error == "ValueError"
    await session.close()
    assert task.release_count == 1


async def test_cancelled_initialization_releases_before_propagating(tmp_path: Path) -> None:
    task, _, job = make_session(tmp_path)
    task.continue_reset.clear()
    run = asyncio.create_task(
        run_episode(task, DoNothing(), 1, job=job, writer=TranscriptWriter(task.path))
    )
    await task.started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert task.release_count == 1
    # An interrupted job is not a finished one: resume must run it again.
    assert not task.path.exists()


async def test_cancelled_close_does_not_cancel_finalization(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    await session.start(policy_name="external", job=job)
    task.continue_grade.clear()
    closing = asyncio.create_task(session.close())
    await task.grade_started.wait()
    closing.cancel()
    task.continue_grade.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert task.release_count == 1
    assert (await session.close())[1].total == 0.5


async def test_grade_failure_is_recorded_released_and_never_retried(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    await session.start(policy_name="external", job=job)
    task.fail_grade = True
    with pytest.raises(RuntimeError, match="grade failed"):
        await session.close()
    reason, score = await session.close()
    assert reason == "error"
    assert score.flags["env_error"]
    assert task.grade_count == task.release_count == 1
    assert read_transcript(task.path).footer.error == "RuntimeError"


async def test_cleanup_failure_keeps_primary_error_and_records_diagnostic(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    task.fail_reset = task.fail_close = True
    with pytest.raises(ValueError, match="reset failed"):
        await session.start(policy_name="external", job=job)
    assert read_transcript(task.path).footer.error == "ValueError"
    assert session.cleanup_errors == ["OSError"]
    assert json.loads(task.path.with_suffix(".cleanup.json").read_text()) == {
        "cleanup_errors": ["OSError"]
    }


async def test_clean_close_removes_stale_cleanup_diagnostics(tmp_path: Path) -> None:
    task, session, job = make_session(tmp_path)
    stale = task.path.with_suffix(".cleanup.json")
    stale.write_text(json.dumps({"cleanup_errors": ["OSError"]}) + "\n")
    await session.start(policy_name="external", job=job)
    await session.close()
    assert session.cleanup_errors == []
    assert not stale.exists()


async def test_store_expiration_and_shutdown_release_resources(tmp_path: Path) -> None:
    now = [0.0]
    store = InMemorySessionStore(10, lambda: now[0])
    task, session, job = make_session(tmp_path / "expires")
    await session.start(policy_name="external", job=job)
    store.put("expires", Entry(session))
    now[0] = 11
    await store.expire()
    assert store.get("expires") is None
    assert task.release_count == 1
    task2, session2, job2 = make_session(tmp_path / "shutdown")
    await session2.start(policy_name="external", job=job2)
    store.put("shutdown", Entry(session2))
    await store.aclose()
    await store.aclose()
    assert task2.release_count == 1


async def test_active_request_is_not_expired(tmp_path: Path) -> None:
    now = [0.0]
    store = InMemorySessionStore(10, lambda: now[0])
    task, session, job = make_session(tmp_path)
    await session.start(policy_name="external", job=job)
    entry = Entry(session)
    store.put("active", entry)
    async with entry.lock:
        now[0] = 20
        await store.expire()
        assert task.release_count == 0
    await store.expire()
    assert task.release_count == 1


async def test_server_shutdown_closes_ungraded_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from crucible.server import app as server_app
    from crucible.server.settings import Settings

    task = ResourceTask(tmp_path / "unused")

    def lookup(_: str) -> Task:
        return task

    monkeypatch.setattr(server_app, "_lookup", lookup)
    app = server_app.create_app(Settings(runs_dir=tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/episodes", json={"task_id": "resource@1", "seed": 1})
            assert response.is_success
            task.path = tmp_path / "episodes" / f"{response.json()['episode_id']}.jsonl"
            assert task.release_count == 0
    assert task.grade_count == task.release_count == 1


async def test_failed_sidecar_initialization_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible.server import app as server_app
    from crucible.server.settings import Settings

    task = ResourceTask(tmp_path / "episodes" / "sidecar-resource@1-0.jsonl")
    task.fail_reset = True

    def lookup(_: str) -> Task:
        return task

    monkeypatch.setattr(server_app, "_lookup", lookup)
    app = server_app.create_app(
        Settings(mode="sidecar", sidecar_task="resource@1", grade_token="test", runs_dir=tmp_path)
    )
    with pytest.raises(ValueError, match="reset failed"):
        async with app.router.lifespan_context(app):
            raise AssertionError("startup must fail")
    assert task.grade_count == 0
    assert task.release_count == 1


async def test_server_sweeper_expires_without_new_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible.server import app as server_app
    from crucible.server.settings import Settings

    class SweptStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__(1, lambda: 100.0)
            self.swept = asyncio.Event()

        async def expire(self) -> None:
            await super().expire()
            self.swept.set()

    store = SweptStore()
    task, session, job = make_session(tmp_path)
    await session.start(policy_name="external", job=job)
    store.put("expires", Entry(session))
    # Refresh the fake clock after initial insertion so the entry is truly idle.
    monkeypatch.setattr(store, "_clock", lambda: 200.0)
    app = server_app.create_app(Settings(runs_dir=tmp_path, session_ttl_seconds=1), store)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(store.swept.wait(), timeout=2)
        assert task.release_count == 1


async def test_transcript_io_failure_still_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "resource.jsonl"
    task = ResourceTask(path)
    writer = TranscriptWriter(path)
    session = EpisodeSession(task, 1, writer)
    job = Job(task="resource@1", policy="external", seed=1)
    await session.start(policy_name="external", job=job)

    def fail_footer(score: Score, reason: str, error: str | None = None) -> None:
        raise OSError("disk full")

    async def release() -> None:
        task.release_count += 1

    monkeypatch.setattr(writer, "footer", fail_footer)
    monkeypatch.setattr(task, "aclose", release)
    with pytest.raises(OSError, match="disk full"):
        await session.close()
    assert task.release_count == 1

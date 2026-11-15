"""The HTTP v1 API: routes, errors, budget, close, TTL and hidden-state leaks."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from crucible.core.registry import get_task
from crucible.server.app import create_app
from crucible.server.settings import Settings
from crucible.server.store import InMemorySessionStore


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(tmp_path: Path, clock: Clock) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(runs_dir=tmp_path, session_ttl_seconds=100)
    app = create_app(settings, InMemorySessionStore(100, clock))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def start(client: httpx.AsyncClient, task: str = "guess@1", seed: int = 0) -> str:
    r = await client.post("/v1/episodes", json={"task_id": task, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()["episode_id"]


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert (r.status_code, r.text) == (200, "ok")


async def test_tasks(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tasks")
    assert r.json() == [
        "guess@1",
        "oncall@1",
        "supplygym@1",
        "supplygym@2",
        "supplygym-cala@1",
        "supplygym-cala@2",
        "supplygym-cala@3",
        "supplygym-fresh@1",
        "supplygym-fresh@2",
    ]


async def test_tools_have_schemas(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tasks/guess@1/tools")
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()}
    assert len(tools) == 2
    expected = {t.name: t.json_schema() for t in get_task("guess@1").tools()}
    assert {n: t["input_schema"] for n, t in tools.items()} == expected
    assert {"name", "description", "kind", "input_schema"} == set(tools["guess"])


async def test_unknown_task_and_episode(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/tasks/nope@1/tools")).status_code == 404
    r = await client.post("/v1/episodes", json={"task_id": "nope@1", "seed": 0})
    assert r.status_code == 404
    for path in ("step", "close"):
        r = await client.post(
            f"/v1/episodes/missing/{path}", json={"name": "guess", "args": {"n": 0}}
        )
        assert r.status_code == 404


async def test_start_response(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/episodes", json={"task_id": "guess@1", "seed": 1})
    body = r.json()
    assert body["max_steps"] == 10
    assert body["instruction"] == get_task("guess@1").instruction()
    assert len(body["episode_id"]) == 32


async def test_budget_refusal_on_call_11(client: httpx.AsyncClient) -> None:
    eid = await start(client)
    for _ in range(10):
        r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
        assert r.json()["ok"] is False
    r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "budget" in r.json()["error"]
    closed = (await client.post(f"/v1/episodes/{eid}/close")).json()
    assert closed["steps"] == 10
    assert closed["stop_reason"] == "budget_exhausted"


async def test_bad_call_is_200_not_ok(client: httpx.AsyncClient) -> None:
    eid = await start(client)
    r = await client.post(
        f"/v1/episodes/{eid}/step", json={"name": "get_metrics", "args": {"service": "web"}}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


async def test_close_is_idempotent(client: httpx.AsyncClient) -> None:
    eid = await start(client)
    await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    first = await client.post(f"/v1/episodes/{eid}/close")
    second = await client.post(f"/v1/episodes/{eid}/close")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["stop_reason"] == "policy_stop"
    after = await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    assert after.json() == {"ok": False, "content": None, "error": "Episode is closed"}


async def test_ttl_expiry(client: httpx.AsyncClient, clock: Clock) -> None:
    eid = await start(client)
    clock.now = 50
    assert (
        await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    ).is_success
    clock.now = 140  # 90 s since last use: still alive (sliding TTL)
    assert (
        await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    ).is_success
    clock.now = 400
    r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "guess", "args": {"n": 0}})
    assert r.status_code == 404

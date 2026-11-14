"""The HTTP v1 API: routes, errors, budget, close, TTL and hidden-state leaks."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from crucible.core.registry import get_task, list_tasks
from crucible.envs.incident.world import make_world
from crucible.server.app import create_app
from crucible.server.settings import Settings
from crucible.server.store import InMemorySessionStore

HIDDEN_WORDS = [
    "bad_deploy",
    "cache_crash",
    "db_disk_full",
    "fault_kind",
    "fault_start",
    "herring",
    "is_bad",
]


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


async def start(client: httpx.AsyncClient, task: str = "incident@1", seed: int = 0) -> str:
    r = await client.post("/v1/episodes", json={"task_id": task, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()["episode_id"]


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert (r.status_code, r.text) == (200, "ok")


async def test_tasks(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tasks")
    assert r.json() == list_tasks()


async def test_tools_have_schemas(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tasks/incident@1/tools")
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()}
    assert len(tools) == 11
    expected = {t.name: t.json_schema() for t in get_task("incident@1").tools()}
    assert {n: t["input_schema"] for n, t in tools.items()} == expected
    props = tools["get_metrics"]["input_schema"]["properties"]["service"]
    assert set(props["enum"]) == {"api", "cache", "db"}
    assert {"name", "description", "kind", "input_schema"} == set(tools["wait"])


async def test_unknown_task_and_episode(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/tasks/nope@1/tools")).status_code == 404
    r = await client.post("/v1/episodes", json={"task_id": "nope@1", "seed": 0})
    assert r.status_code == 404
    for path in ("step", "close"):
        r = await client.post(f"/v1/episodes/missing/{path}", json={"name": "get_alerts"})
        assert r.status_code == 404


async def test_start_response(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/episodes", json={"task_id": "incident@1", "seed": 1})
    body = r.json()
    assert body["max_steps"] == 40
    assert body["instruction"] == get_task("incident@1").instruction()
    assert len(body["episode_id"]) == 32


async def test_budget_refusal_on_call_41(client: httpx.AsyncClient) -> None:
    eid = await start(client)
    for _ in range(40):
        r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})
        assert r.json()["ok"] is True
    r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "budget" in r.json()["error"]
    closed = (await client.post(f"/v1/episodes/{eid}/close")).json()
    assert closed["steps"] == 40
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
    await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})
    first = await client.post(f"/v1/episodes/{eid}/close")
    second = await client.post(f"/v1/episodes/{eid}/close")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["stop_reason"] == "policy_stop"
    after = await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})
    assert after.json() == {"ok": False, "content": None, "error": "Episode is closed"}


async def test_ttl_expiry(client: httpx.AsyncClient, clock: Clock) -> None:
    eid = await start(client)
    clock.now = 50
    assert (await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})).is_success
    clock.now = 140  # 90 s since last use: still alive (sliding TTL)
    assert (await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})).is_success
    clock.now = 400
    r = await client.post(f"/v1/episodes/{eid}/step", json={"name": "get_alerts"})
    assert r.status_code == 404


@pytest.mark.parametrize("seed", range(3))
async def test_no_hidden_leak_in_http_bodies(client: httpx.AsyncClient, seed: int) -> None:
    world = make_world(seed)
    eid = await start(client, seed=seed)
    bodies: list[tuple[int, str]] = []
    minute = 0
    for i in range(40):
        calls: list[dict[str, object]] = [
            {"name": "get_topology"},
            {"name": "get_alerts"},
            {"name": "list_deploys"},
            {"name": "get_metrics", "args": {"service": "api"}},
            {"name": "read_logs", "args": {"service": "db", "since": max(0, minute - 15)}},
            {"name": "wait", "args": {"minutes": 10}},
        ]
        r = await client.post(f"/v1/episodes/{eid}/step", json=calls[i % len(calls)])
        result = r.json()
        bodies.append((minute, r.text))
        if result["ok"] and isinstance(result["content"], dict):
            m = result["content"].get("minute")
            minute = m if isinstance(m, int) else minute
    for word in HIDDEN_WORDS:
        assert all(word not in text for _, text in bodies), word
    bad_id = world.bad_deploy_id
    if bad_id is not None:
        for m, text in bodies:
            if m < world.fault_start:
                assert bad_id not in text
    assert json.loads(bodies[0][1])["ok"] is True

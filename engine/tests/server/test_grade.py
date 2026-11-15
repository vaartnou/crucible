"""`/grade`, the token, and the route sets of the two modes."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from crucible.server.app import create_app
from crucible.server.settings import Settings

TOKEN = "secret-token"


def sidecar_settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="sidecar", grade_token=TOKEN, runs_dir=tmp_path, sidecar_seed=1, sidecar_task="guess@1"
    )


@asynccontextmanager
async def sidecar_client(tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    """A context manager, not a fixture: the lifespan must start and stop in one task."""
    app = create_app(sidecar_settings(tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_grade_needs_the_token(tmp_path: Path) -> None:
    async with sidecar_client(tmp_path) as sidecar:
        missing = await sidecar.get("/grade")
        wrong = await sidecar.get("/grade", headers={"x-grade-token": "nope"})
        assert (missing.status_code, missing.json()) == (403, {"error": "forbidden"})
        assert (wrong.status_code, wrong.json()) == (403, {"error": "forbidden"})


async def test_grade_with_token_is_idempotent(tmp_path: Path) -> None:
    async with sidecar_client(tmp_path) as sidecar:
        first = await sidecar.get("/grade", headers={"x-grade-token": TOKEN})
        second = await sidecar.get("/grade", headers={"x-grade-token": TOKEN})
        assert first.status_code == 200
        body = first.json()
        assert 0.0 <= body["reward"] <= 1.0
        assert body["task"] == "guess@1"
        assert body["seed"] == 1
        assert body["steps"] == 0
        assert body["stop_reason"] == "policy_stop"
        assert [r["type"] for r in body["transcript"]] == ["header", "footer"]
        assert second.json() == body


async def test_sidecar_without_token_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="GRADE_TOKEN"):
        create_app(Settings(mode="sidecar", grade_token="", runs_dir=tmp_path))


async def test_sidecar_has_no_v1(tmp_path: Path) -> None:
    async with sidecar_client(tmp_path) as sidecar:
        assert (await sidecar.get("/healthz")).text == "ok"
        assert (await sidecar.get("/v1/tasks")).status_code == 404
        assert (
            await sidecar.post("/v1/episodes", json={"task_id": "guess@1", "seed": 0})
        ).status_code == 404


async def test_api_mode_has_no_grade_or_mcp(tmp_path: Path) -> None:
    app = create_app(Settings(runs_dir=tmp_path, grade_token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/grade", headers={"x-grade-token": TOKEN})).status_code == 404
        assert (await c.post("/mcp", json={})).status_code == 404
        assert (await c.get("/v1/tasks")).status_code == 200


async def test_autoplay_requires_separate_token_and_records_public_actions(tmp_path: Path) -> None:
    settings = sidecar_settings(tmp_path).model_copy(
        update={"solution_token": "oracle-secret", "sidecar_salt": "private-salt"}
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        for token in (None, TOKEN, "wrong"):
            headers = {} if token is None else {"x-solution-token": token}
            assert (await c.post("/autoplay", headers=headers)).status_code == 403
        response = await c.post("/autoplay", headers={"x-solution-token": "oracle-secret"})
        assert response.status_code == 200
        assert response.json()["reward"] > 0
        grade = (await c.get("/grade", headers={"x-grade-token": TOKEN})).json()
        assert grade["reward"] == response.json()["reward"]
        assert grade["steps"] > 0
        assert grade["transcript"][0]["salted"] is True
        assert all(record["call"]["name"] == "guess" for record in grade["transcript"][1:-1])
        assert (
            await c.post("/autoplay", headers={"x-solution-token": "oracle-secret"})
        ).status_code == 409


async def test_autoplay_disabled_without_solution_token(tmp_path: Path) -> None:
    async with sidecar_client(tmp_path) as c:
        assert (await c.post("/autoplay", headers={"x-solution-token": ""})).status_code == 403

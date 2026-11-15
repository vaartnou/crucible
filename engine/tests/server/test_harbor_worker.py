"""The public UI forwards Harbor operations; the worker keeps Docker and credentials private."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException, Request

from crucible.server import harbor_worker
from crucible.server.app import create_app
from crucible.server.harbor_jobs import HarborJobs, LaunchHarborRequest
from crucible.server.settings import Settings

TOKEN = "worker-test-token-0123456789abcdef0123456789"


async def test_worker_requires_auth_and_exposes_only_harbor_routes(tmp_path: Path) -> None:
    app = create_app(
        Settings(runs_dir=tmp_path, harbor_worker_mode=True, harbor_worker_token=TOKEN)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://worker.test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/ui/harbor/jobs")).status_code == 401
        assert (
            await client.get("/ui/runs", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await client.get("/ui/harbor/jobs")).status_code == 200
        assert (await client.get("/ui/meta")).status_code == 404
        assert (await client.post("/ui/sessions", json={})).status_code == 404


async def test_public_app_forwards_worker_operations_and_keeps_play_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = create_app(
        Settings(
            runs_dir=tmp_path / "worker",
            harbor_jobs_dir=tmp_path / "jobs",
            harbor_worker_mode=True,
            harbor_worker_token=TOKEN,
        )
    )

    @worker.get("/ui/harbor/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {
            "query": request.query_params.multi_items(),
            "authorization": request.headers.get("authorization"),
            "cookie": request.headers.get("cookie"),
        }

    @worker.post("/ui/harbor/echo")
    async def echo_body(request: Request) -> dict[str, object]:
        return {"body": await request.json()}

    def worker_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=worker))

    monkeypatch.setattr(harbor_worker, "AsyncClient", worker_client)
    public = create_app(
        Settings(
            runs_dir=tmp_path / "public",
            harbor_worker_url="https://worker.test",
            harbor_worker_token=TOKEN,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public), base_url="https://demo.test"
    ) as client:
        jobs = await client.get("/ui/harbor/jobs")
        assert jobs.status_code == 200
        assert jobs.json()["jobs_dir"] == str(tmp_path / "jobs")
        assert (await client.get("/ui/runs")).json() == []
        response = await client.get(
            "/ui/harbor/echo?path=%2Fworker%2Ftrial&step=1&step=2",
            headers={"Authorization": "Bearer browser-value", "Cookie": "private=yes"},
        )
        assert response.json() == {
            "query": [["path", "/worker/trial"], ["step", "1"], ["step", "2"]],
            "authorization": f"Bearer {TOKEN}",
            "cookie": None,
        }
        launch = {"export": "demo", "model": "google/approved", "concurrency": 1}
        assert (await client.post("/ui/harbor/echo", json=launch)).json() == {"body": launch}
        assert (await client.post("/ui/sessions", json={"mode": "human"})).status_code == 200


async def test_worker_unavailable_returns_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private connection detail", request=request)

    def worker_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fail))

    monkeypatch.setattr(harbor_worker, "AsyncClient", worker_client)
    app = create_app(
        Settings(
            runs_dir=tmp_path,
            harbor_worker_url="https://worker.test",
            harbor_worker_token=TOKEN,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://demo.test"
    ) as client:
        response = await client.get("/ui/harbor/jobs")
        assert response.status_code == 503
        assert "private connection detail" not in response.text


@pytest.mark.parametrize(
    "values",
    [
        {"harbor_worker_mode": True},
        {"harbor_worker_url": "https://worker.test"},
        {"harbor_worker_url": "http://worker.test", "harbor_worker_token": TOKEN},
        {"harbor_worker_url": "https://worker.test/path", "harbor_worker_token": TOKEN},
        {
            "harbor_worker_url": "https://worker.test",
            "harbor_worker_token": TOKEN,
            "harbor_worker_mode": True,
        },
    ],
)
def test_invalid_worker_configuration_fails(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        create_app(Settings.model_validate(values))


async def test_worker_enforces_models_concurrency_and_persisted_daily_limit(tmp_path: Path) -> None:
    jobs = HarborJobs(
        Settings(
            harbor_jobs_dir=tmp_path,
            harbor_allowed_models="google/approved, openai/approved",
            harbor_max_concurrency=1,
            harbor_daily_job_limit=1,
        )
    )
    for request in [
        LaunchHarborRequest(export="demo", model="unapproved", concurrency=1),
        LaunchHarborRequest(export="demo", model="google/approved", concurrency=2),
    ]:
        with pytest.raises(HTTPException) as error:
            await jobs.launch(request)
        assert error.value.status_code == 400
    (tmp_path / datetime.now(UTC).strftime("oncall-%Y%m%d-previous")).mkdir()
    with pytest.raises(HTTPException) as limit:
        await jobs.launch(
            LaunchHarborRequest(export="demo", model="google/approved", concurrency=1)
        )
    assert limit.value.status_code == 429

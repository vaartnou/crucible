"""Contract checks against a real subprocess HTTP application."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from crucible.backends import (
    BackendConnection,
    BackendEpisode,
    BackendError,
    ExclusiveLeaseProvider,
    HttpBackendRuntime,
    LeaseConflict,
)

SERVICE = """
from fastapi import FastAPI, Response
app = FastAPI()
records = {}
@app.get("/healthz")
def health():
    return {"ready": True}
@app.post("/records/{episode}")
def prepare(episode: str):
    records[episode] = {"value": 7}
    return records[episode]
@app.get("/records")
def read():
    return records
@app.delete("/records/{episode}")
def release(episode: str):
    records.pop(episode, None)
    return Response(status_code=204)
"""


@pytest.fixture
def service(tmp_path: Path) -> Iterator[str]:
    """Bind in the parent so parallel tests cannot race for a supposedly free port."""
    (tmp_path / "backend_fixture.py").write_text(SERVICE)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend_fixture:app",
                "--fd",
                str(listener.fileno()),
                "--log-level",
                "error",
            ],
            cwd=tmp_path,
            pass_fds=(listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            yield url
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)


def connection(url: str, *, exclusive: bool = True) -> BackendConnection:
    return BackendConnection(
        deployment_id="contract-fixture",
        base_url=SecretStr(url),
        headers={"Authorization": SecretStr("Bearer private-token")},
        exclusive_access=exclusive,
    )


async def test_real_http_prepare_inspect_collect_release(service: str, tmp_path: Path) -> None:
    async def prepare(episode: BackendEpisode) -> None:
        response = await episode.client.post(f"records/{episode.resource.episode_id}")
        response.raise_for_status()

    async def release(episode: BackendEpisode) -> None:
        response = await episode.client.delete(f"records/{episode.resource.episode_id}")
        response.raise_for_status()

    async def artifacts(episode: BackendEpisode) -> Mapping[str, bytes]:
        response = await episode.client.get("records")
        response.raise_for_status()
        return {"application/snapshot.json": response.content}

    runtime = HttpBackendRuntime(
        connection(service),
        ExclusiveLeaseProvider(tmp_path / "locks"),
        prepare=prepare,
        release=release,
        artifacts=artifacts,
    )
    episode = await runtime.acquire()
    assert await runtime.acquire() is episode
    data = (await episode.client.get("records")).json()
    assert data == {episode.resource.episode_id: {"value": 7}}
    hashes = await runtime.collect_artifacts(tmp_path / "evidence")
    content = (tmp_path / "evidence/application/snapshot.json").read_bytes()
    assert json.loads(content) == data
    assert hashes == {"application/snapshot.json": hashlib.sha256(content).hexdigest()}
    assert episode.resource.isolation == "exclusive"
    await runtime.aclose()
    await runtime.aclose()
    assert episode.client.is_closed
    # Release removes only our records. The attached service remains running.
    async with httpx.AsyncClient(base_url=service, trust_env=False) as client:
        assert (await client.get("/records")).json() == {}
        assert (await client.get("/healthz")).status_code == 200
    with pytest.raises(BackendError, match="closed"):
        await runtime.acquire()


async def test_conflict_before_preparation_and_reacquire(tmp_path: Path) -> None:
    provider = ExclusiveLeaseProvider(tmp_path)
    config = connection("http://127.0.0.1:1")
    lease = await provider.acquire(config, "first")
    with pytest.raises(LeaseConflict):
        await provider.acquire(config, "second")
    # A separate Python process must observe the same lock.
    script = """
import asyncio, sys
from pathlib import Path
from pydantic import SecretStr
from crucible.backends import BackendConnection, ExclusiveLeaseProvider, LeaseConflict
async def main():
    config = BackendConnection(
        deployment_id="contract-fixture", base_url=SecretStr("http://other-alias"),
        exclusive_access=True,
    )
    try:
        await ExclusiveLeaseProvider(Path(sys.argv[1])).acquire(config, "child")
    except LeaseConflict:
        return
    raise AssertionError("cross-process conflict not rejected")
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    output, error = await child.communicate()
    assert child.returncode == 0, (output, error)
    await lease.release()
    await lease.release()
    next_lease = await provider.acquire(config, "third")
    await next_lease.release()
    # Lock files intentionally remain: unlinking would break process exclusion.
    assert len(await asyncio.to_thread(lambda: list(tmp_path.glob("*.lock")))) == 1


async def test_unproven_access_rejected_without_application_calls(tmp_path: Path) -> None:
    called = False

    async def prepare(episode: BackendEpisode) -> None:
        nonlocal called
        called = True

    runtime = HttpBackendRuntime(
        connection("http://127.0.0.1:1", exclusive=False),
        ExclusiveLeaseProvider(tmp_path),
        prepare=prepare,
    )
    with pytest.raises(BackendError, match="isolation or exclusive"):
        await runtime.acquire()
    assert not called
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    await runtime.aclose()


async def test_readiness_failure_releases_lease_and_has_sanitized_error(tmp_path: Path) -> None:
    config = connection("http://127.0.0.1:1")
    provider = ExclusiveLeaseProvider(tmp_path)
    runtime = HttpBackendRuntime(config, provider, readiness_attempts=2, readiness_interval=0)
    with pytest.raises(BackendError, match="readiness check failed") as error:
        await runtime.acquire()
    assert "127.0.0.1" not in str(error.value)
    assert "private-token" not in str(error.value)
    lease = await provider.acquire(config, "recovered")
    await lease.release()
    await runtime.aclose()


async def test_partial_prepare_and_cleanup_error_preserve_primary(
    service: str, tmp_path: Path
) -> None:
    async def prepare(episode: BackendEpisode) -> None:
        raise ValueError("primary")

    async def release(episode: BackendEpisode) -> None:
        raise RuntimeError("contains private data")

    config = connection(service)
    provider = ExclusiveLeaseProvider(tmp_path)
    runtime = HttpBackendRuntime(config, provider, prepare=prepare, release=release)
    with pytest.raises(ValueError, match="primary"):
        await runtime.acquire()
    assert runtime.cleanup_errors == ("RuntimeError",)
    await runtime.aclose()
    lease = await provider.acquire(config, "recovered")
    await lease.release()


async def test_cancel_during_prepare_releases_lease(service: str, tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def prepare(episode: BackendEpisode) -> None:
        entered.set()
        await asyncio.Event().wait()

    config = connection(service)
    provider = ExclusiveLeaseProvider(tmp_path)
    runtime = HttpBackendRuntime(config, provider, prepare=prepare)
    task = asyncio.create_task(runtime.acquire())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lease = await provider.acquire(config, "recovered")
    await lease.release()


async def test_artifact_paths_and_secret_redaction(service: str, tmp_path: Path) -> None:
    names = {"snapshot.txt": b"Bearer private-token"}

    async def artifacts(episode: BackendEpisode) -> Mapping[str, bytes]:
        return names

    runtime = HttpBackendRuntime(
        connection(service), ExclusiveLeaseProvider(tmp_path / "locks"), artifacts=artifacts
    )
    await runtime.acquire()
    evidence = tmp_path / "evidence"
    try:
        await runtime.collect_artifacts(evidence)
        assert (evidence / "snapshot.txt").read_bytes() == b"[REDACTED]"
        names["../escape"] = b"bad"
        with pytest.raises(BackendError, match="safe relative path"):
            await runtime.collect_artifacts(evidence)
        assert not (tmp_path / "escape").exists()
        del names["../escape"]
        (evidence / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
        names["link/escape"] = b"bad"
        with pytest.raises(BackendError, match="escapes"):
            await runtime.collect_artifacts(evidence)
        assert not (tmp_path / "outside").exists()
    finally:
        await runtime.aclose()


def test_private_configuration_repr_serialization_and_validation() -> None:
    config = connection("https://private.example/v1/")
    assert "private.example" not in repr(config)
    assert "private-token" not in repr(config)
    assert "private-token" not in config.model_dump_json()
    assert "private.example" not in config.model_dump_json()
    for url in ("file:///tmp/secret", "https://user:pass@example", "https://example?token=secret"):
        with pytest.raises(ValidationError):
            connection(url)
    with pytest.raises(ValueError, match="local HTTP path"):
        HttpBackendRuntime(config, ExclusiveLeaseProvider(Path("unused")), readiness_path="//evil")


async def test_client_construction_failure_is_not_masked_by_lease_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.backends import ResourceLease

    released: list[str] = []

    class Provider:
        async def acquire(self, connection: BackendConnection, episode_id: str) -> ResourceLease:
            async def release() -> None:
                released.append(episode_id)
                raise RuntimeError("private release failure")

            return ResourceLease(episode_id, "isolated", "isolated", release)

    def fail_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        raise ValueError("primary client construction failure")

    runtime = HttpBackendRuntime(connection("http://localhost"), Provider())
    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    with pytest.raises(ValueError, match="primary client construction failure"):
        await runtime.acquire()
    assert len(released) == 1
    assert runtime.cleanup_errors == ("RuntimeError",)
    await runtime.aclose()

"""Run a sidecar app on a free port for the length of one test."""

import asyncio
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from crucible.server.app import create_app
from crucible.server.settings import Settings

TOKEN = "test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def live_sidecar(runs_dir: Path, seed: int, task: str = "guess@1") -> AsyncGenerator[str]:
    """Yield the base URL of a sidecar server. Always shut it down."""
    settings = Settings(
        mode="sidecar",
        grade_token=TOKEN,
        sidecar_task=task,
        sidecar_seed=seed,
        runs_dir=runs_dir,
        log_level="warning",
    )
    async with _live(settings) as url:
        yield url


@asynccontextmanager
async def live_api(runs_dir: Path) -> AsyncGenerator[str]:
    """Yield the base URL of an api-mode server. Always shut it down."""
    async with _live(Settings(runs_dir=runs_dir, log_level="warning")) as url:
        yield url


@asynccontextmanager
async def _live(settings: Settings) -> AsyncGenerator[str]:
    port = free_port()
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(20):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await serving
        except TimeoutError:
            serving.cancel()

"""`crucible play` against live servers. The CLI runs in a worker thread (it owns a loop)."""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crucible.cli import app
from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from tests.server.live import free_port, live_api, live_sidecar

runner = CliRunner()


async def _play(*args: str) -> tuple[int, str]:
    result = await asyncio.to_thread(runner.invoke, app, ["play", *args])
    return result.exit_code, result.output


async def test_play_api_matches_eval(tmp_path: Path) -> None:
    local = (
        await LocalExecutor().run([Job(task="oncall@1", policy="reference", seed=0)], tmp_path)
    )[0]
    async with live_api(tmp_path / "srv") as url:
        code, output = await _play("--api", url, "--seed", "0", "--task", "oncall@1")
    assert code == 0
    body = json.loads(output.strip().splitlines()[-1])
    assert body == {
        "steps": local.steps,
        "stop_reason": local.stop_reason,
        "score": local.score.total,
    }


async def test_play_mcp_prints_steps_without_score(tmp_path: Path) -> None:
    async with live_sidecar(tmp_path, 0, task="oncall@1") as url:
        code, output = await _play("--mcp", f"{url}/mcp", "--task", "oncall@1")
    assert code == 0
    body = json.loads(output.strip().splitlines()[-1])
    assert set(body) == {"steps", "stop_reason"}
    assert body["steps"] > 0
    assert body["stop_reason"] == "policy_stop"


@pytest.mark.parametrize(
    "args",
    [
        ["--task", "oncall@1"],
        ["--task", "oncall@1", "--api", "http://x", "--mcp", "http://y"],
        ["--task", "oncall@1", "--api", "http://x"],
        ["--task", "oncall@1", "--mcp", "http://y", "--seed", "1"],
        ["--task", "oncall@1", "--mcp", "http://y", "--policy", "nope"],
        ["--mcp", "http://y", "--task", "nope@1"],
        ["--mcp", "http://y"],
    ],
)
async def test_play_bad_flags_exit_2(args: list[str]) -> None:
    code, _ = await _play(*args)
    assert code == 2


async def test_play_unreachable_server_exits_2() -> None:
    code, _ = await _play(
        "--api", f"http://127.0.0.1:{free_port()}", "--seed", "0", "--task", "oncall@1"
    )
    assert code == 2
    code, _ = await _play("--mcp", f"http://127.0.0.1:{free_port()}/mcp", "--task", "oncall@1")
    assert code == 2

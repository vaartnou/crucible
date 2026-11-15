"""The MCP sidecar over a live server: tools, reference parity, bad calls, hidden values."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult

from crucible.agents.remote import McpEpisode, drive, fetch_grade
from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.tools import Observation, ToolCall
from tests.server.live import TOKEN, live_sidecar

TASK = "incident@1"
HIDDEN_WORDS = [
    "bad_deploy",
    "cache_crash",
    "db_disk_full",
    "fault_kind",
    "fault_start",
    "herring",
    "is_bad",
]


async def test_tools_list_matches_specs(tmp_path: Path) -> None:
    specs = {s.name: s for s in get_task(TASK).tools()}
    async with live_sidecar(tmp_path, 0) as url, Client(f"{url}/mcp") as client:
        listed = (await client.list_tools()).tools
        assert client.instructions == get_task(TASK).instruction()
    assert len(listed) == len(specs) == 11
    for tool in listed:
        assert tool.description == specs[tool.name].description
        assert tool.input_schema == specs[tool.name].json_schema()


@pytest.mark.parametrize("seed", range(3))
async def test_reference_over_mcp_matches_local(seed: int, tmp_path: Path) -> None:
    local = (
        await LocalExecutor().run([Job(task=TASK, policy="reference", seed=seed)], tmp_path / "l")
    )[0]
    task = get_task(TASK)
    async with live_sidecar(tmp_path / "srv", seed) as url:
        episode, instruction = await McpEpisode.connect(
            f"{url}/mcp", grade=lambda: fetch_grade(url, TOKEN)
        )
        outcome = await drive(
            get_policy("reference", TASK), episode, task.spec, task.tools(), instruction
        )
        async with httpx.AsyncClient() as http:
            body = (await http.get(f"{url}/grade", headers={"x-grade-token": TOKEN})).json()
    assert outcome.score == local.score
    assert outcome.steps == local.steps
    assert outcome.stop_reason == local.stop_reason
    assert body["reward"] == local.score.total
    assert body["transcript"][0]["policy"] == "external"
    assert len(body["transcript"]) == local.steps + 2


def structured(result: CallToolResult) -> dict[str, Any]:
    out: dict[str, Any] = result.model_dump()["structured_content"]
    return out


def minute(result: CallToolResult) -> int:
    return int(structured(result)["content"]["minute"])


async def test_bad_call_is_an_error_and_changes_nothing(tmp_path: Path) -> None:
    async with live_sidecar(tmp_path, 0) as url, Client(f"{url}/mcp") as client:
        before = await client.call_tool("get_alerts", {})
        bad = await client.call_tool("get_metrics", {"service": "web"})
        after = await client.call_tool("get_alerts", {})
        unknown = await client.call_tool("no_such_tool", {})
    assert bad.is_error
    assert structured(bad)["ok"] is False
    assert "service" in str(structured(bad)["error"])
    assert unknown.is_error
    assert not before.is_error
    # A good call advances the clock by one minute; the bad call must not add another.
    assert minute(after) == minute(before) + 1


@pytest.mark.parametrize("seed", range(3))
async def test_no_hidden_value_in_mcp_results(seed: int, tmp_path: Path) -> None:
    task = get_task(TASK)
    texts: list[str] = []
    async with live_sidecar(tmp_path, seed) as url:
        episode, instruction = await McpEpisode.connect(f"{url}/mcp")
        policy = get_policy("reference", TASK)
        await policy.reset(task.spec, task.tools(), instruction)
        obs = Observation()
        for _ in range(task.spec.max_steps):
            call = await policy.act(obs)
            if call is None:
                break
            result = await episode.step(call)
            obs = Observation(last_result=result)
            texts.append(json.dumps(result.model_dump(mode="json")))
        extra = await episode.step(ToolCall(name="get_metrics", args={"service": "web"}))
        texts.append(json.dumps(extra.model_dump(mode="json")))
        await episode.close()
    assert len(texts) > 5
    for text in texts:
        for word in HIDDEN_WORDS:
            assert word not in text, word


@pytest.mark.parametrize("task_ref", ["incident@1", "supplygym-cala@3"])
async def test_budget_exhaustion_returns_structured_error(tmp_path: Path, task_ref: str) -> None:
    task = get_task(task_ref)
    feed = tmp_path / "spectator.jsonl" if task_ref == "supplygym-cala@3" else None
    tool = "get_briefing" if feed else "get_alerts"
    async with (
        live_sidecar(tmp_path, 0, task_ref, spectator_path=feed) as url,
        Client(f"{url}/mcp") as client,
    ):
        for _ in range(task.spec.max_steps):
            assert not (await client.call_tool(tool, {})).is_error
        refused = await client.call_tool(tool, {})
        assert refused.is_error
        assert structured(refused)["ok"] is False
        assert "budget" in structured(refused)["error"].lower()
        if feed:
            events = [json.loads(line) for line in feed.read_text().splitlines()]
            assert len(events) == 1
            assert events[0]["stop_reason"] == "budget_exhausted"
        grade = await fetch_grade(url, TOKEN)
        assert grade.stop_reason == "budget_exhausted"
        assert (await client.call_tool(tool, {})).is_error
    if feed:
        assert len(feed.read_text().splitlines()) == 1

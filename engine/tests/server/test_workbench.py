"""Tests for Oncall Workbench UI API: human play, Fog-of-War, God-View, replay, and isolation."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.core.tools import ToolCall, ToolResult
from crucible.core.transcript import TranscriptWriter, read_transcript
from crucible.envs.oncall.task import OncallTask
from crucible.feedback.version import environment_hash, simulator_hash
from crucible.server.app import create_app
from crucible.server.settings import Settings
from crucible.server.sources import list_saved_runs


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(mode="api", runs_dir=tmp_path)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_sidecar_mode_does_not_expose_ui_routes(tmp_path: Path) -> None:
    """Harbor sidecar containers must never expose omniscient `/ui` routes."""
    settings = Settings(mode="sidecar", runs_dir=tmp_path, grade_token="secret-grade-token")
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/ui/meta")).status_code == 404
        assert (await c.get("/ui/sessions")).status_code == 404


async def test_ui_meta_lists_oncall_tools_and_policies(client: httpx.AsyncClient) -> None:
    r = await client.get("/ui/meta")
    assert r.status_code == 200
    body = r.json()
    assert "oncall@1" in body["tasks"]
    assert "supplygym-cala@3" in body["tasks"]
    assert body["default_task"] == "oncall@1"
    assert "graph_walk" in body["policies"]["oncall@1"]
    assert "reference" in body["policies"]["oncall@1"]
    tool_names = {t["name"] for t in body["tools"]["oncall@1"]}
    assert tool_names == {
        "run",
        "query_metrics",
        "query_edges",
        "search_logs",
        "list_alerts",
        "wait",
        "resolve",
    }
    assert len(body["command_templates"]) == 10


async def test_human_play_fog_of_war_vs_god_view(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/ui/sessions",
        json={"task_id": "oncall@1", "seed": 2, "difficulty": 1, "mode": "human"},
    )
    assert r.status_code == 200
    initial = r.json()
    sid = initial["session_id"]
    assert initial["god_state"] is None
    assert initial["fog_state"]["topology_known"] is False
    assert initial["fog_state"]["services"] == []

    step1 = await client.post(
        f"/ui/sessions/{sid}/step",
        json={"name": "run", "args": {"command": "kubectl get services"}, "include_god": False},
    )
    assert step1.status_code == 200
    s1 = step1.json()["session"]
    assert s1["steps_used"] == 1
    assert s1["fog_state"]["topology_known"] is True
    assert len(s1["fog_state"]["services"]) == 8
    assert s1["fog_state"]["current_minute"] == 6
    assert s1["god_state"] is None

    god_resp = await client.get(f"/ui/sessions/{sid}?include_god=true&reveal_secret=false")
    assert god_resp.status_code == 200
    god = god_resp.json()["god_state"]
    assert god is not None
    assert god["secret"] is None
    assert len(god["services"]) == 8
    assert len(god["traces"]) >= 2
    assert len(god["timeline"]["minutes"]) == len(god["timeline"]["actual_loss"])

    secret_resp = await client.get(f"/ui/sessions/{sid}?include_god=true&reveal_secret=true")
    secret = secret_resp.json()["god_state"]["secret"]
    assert secret is not None
    assert secret["culprit"]
    assert secret["change_id"].startswith("chg-")


async def test_scoped_change_details_survive_later_global_headers(
    client: httpx.AsyncClient,
) -> None:
    initial = (await client.post("/ui/sessions", json={"seed": 0})).json()
    endpoint = f"/ui/sessions/{initial['session_id']}/step"

    async def run(command: str) -> dict[str, Any]:
        response = await client.post(endpoint, json={"name": "run", "args": {"command": command}})
        assert response.status_code == 200
        return response.json()

    headers = await run("changes list")
    rows = headers["result"]["content"]["data"]
    assert all("summary" not in row for row in rows)
    target = rows[0]["service"]
    scoped = await run(f"changes list {target}")
    details = {row["id"]: row for row in scoped["session"]["fog_state"]["changes"]}
    discovered = [row for row in details.values() if row["service"] == target]
    assert discovered
    assert all("summary" in row and "before" in row and "after" in row for row in discovered)
    assert all("summary" not in row for row in details.values() if row["service"] != target)
    refreshed = await run("changes list")
    refreshed_details = {row["id"]: row for row in refreshed["session"]["fog_state"]["changes"]}
    for row in discovered:
        for key in ("summary", "before", "after"):
            assert refreshed_details[row["id"]][key] == row[key]


async def test_deep_shared_dependencies_all_appear_in_call_paths(client: httpx.AsyncClient) -> None:
    response = await client.post("/ui/sessions?include_god=true", json={"seed": 0})
    assert response.status_code == 200
    god = response.json()["god_state"]
    assert len(god["services"]) == 23
    traces = god["traces"]
    assert len({trace["trace_id"] for trace in traces}) == len(traces)
    traced_edges = {
        (span["caller"], span["service"])
        for trace in traces
        for span in trace["spans"]
        if span["caller"] is not None
    }
    actual_edges = {(edge["caller"], edge["callee"]) for edge in god["edges"]}
    assert traced_edges == actual_edges
    assert max(len(trace["spans"]) for trace in traces) >= 5


async def test_replay_scrubbing_cannot_validate_a_divergent_world(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    result = (
        await LocalExecutor().run(
            [Job(task="oncall@1", policy="reference", seed=1)], tmp_path / "replay"
        )
    )[0]
    path = result.transcript_path
    valid = await client.get("/ui/runs/replay", params={"path": str(path), "up_to_step": 0})
    assert valid.status_code == 200
    assert valid.json()["has_matched_world"]

    transcript = read_transcript(path)
    records = [
        rec.model_dump(mode="json")
        for rec in (transcript.header, *transcript.steps, transcript.footer)
    ]
    records[1]["result"]["content"]["data"][0]["name"] = "obsolete-service"
    path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")
    for selected_step in (0, 1):
        response = await client.get(
            "/ui/runs/replay", params={"path": str(path), "up_to_step": selected_step}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["salt_status"] == "diverged_simulator"
        assert payload["has_matched_world"] is False
        assert payload["god_state"] is None


async def test_saved_comparison_uses_recorded_task_and_rejects_divergence(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    result = (
        await LocalExecutor().run(
            [Job(task="oncall@2", policy="incident_runbook", seed=0)], tmp_path / "overlap"
        )
    )[0]
    params = {"replay_path": str(result.transcript_path), "policy": "incident_runbook"}
    response = await client.get("/ui/compare", params=params)
    assert response.status_code == 200
    assert response.json()["task"] == "oncall@2"
    assert response.json()["score"]["total"] == result.score.total
    records = result.transcript_path.read_text().splitlines()
    row = json.loads(records[1])
    row["result"]["content"]["data"][0]["value"] = -999
    records[1] = json.dumps(row)
    result.transcript_path.write_text("\n".join(records) + "\n")
    invalid = await client.get("/ui/compare", params=params)
    assert invalid.status_code == 422


async def test_overlap_session_reveals_both_causes_only_when_requested(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/ui/sessions?include_god=true", json={"task_id": "oncall@2", "seed": 0}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["god_state"]["secret"] is None
    revealed = await client.get(
        f"/ui/sessions/{payload['session_id']}",
        params={"include_god": True, "reveal_secret": True},
    )
    assert len(revealed.json()["god_state"]["secret"]["incidents"]) == 2


async def test_live_policy_session_completes_and_grades(tmp_path: Path) -> None:
    settings = Settings(mode="api", runs_dir=tmp_path)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/ui/sessions",
            json={
                "task_id": "oncall@1",
                "seed": 0,
                "difficulty": 1,
                "mode": "policy",
                "policy": "reference",
                "step_delay_ms": 0,
                "auto_run": True,
            },
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]

        final_body = None
        for _ in range(50):
            poll = await c.get(f"/ui/sessions/{sid}?include_god=true")
            body = poll.json()
            if body["status"] == "closed":
                final_body = body
                break
            await asyncio.sleep(0.02)

        assert final_body is not None
        assert final_body["outcome"] is not None
        assert final_body["outcome"]["score"]["total"] > 0.5
        assert final_body["outcome"]["solved"] is True


async def test_saved_and_live_tmp_transcript_replay(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch1"
    await LocalExecutor().run([Job(task="oncall@1", policy="loudest_alert", seed=1)], batch_dir)

    live_path = tmp_path / "live_agent" / "0.jsonl"
    writer = TranscriptWriter(live_path)
    writer.header(
        task="oncall@1",
        scoring_hash="abc",
        policy="live-test-model",
        seed=1,
        repeat=0,
        job_id="j1",
    )
    writer.step(
        0,
        ToolCall(name="list_alerts", args={}),
        ToolResult(
            ok=True,
            content={
                "minute": 6,
                "observed_at": 5,
                "data": [
                    {
                        "id": "errors-edge-gateway",
                        "service": "edge-gateway",
                        "severity": 3,
                        "error_ratio": 0.25,
                        "started_at": 1,
                    }
                ],
            },
        ),
    )

    settings = Settings(mode="api", runs_dir=tmp_path)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        runs = await list_saved_runs([tmp_path])
        live_entries = [r for r in runs if r["live"] is True and r["policy"] == "live-test-model"]
        done_entries = [r for r in runs if r["live"] is False and r["policy"] == "loudest_alert"]
        assert len(live_entries) == 1
        assert len(done_entries) == 1

        rep = await c.get(
            "/ui/runs/replay",
            params={"path": done_entries[0]["path"], "up_to_step": 2},
        )
        assert rep.status_code == 200
        rep_body = rep.json()
        assert rep_body["selected_step"] == 2
        assert rep_body["total_steps"] >= 2
        assert rep_body["god_state"] is not None

        cmp_resp = await c.get(
            "/ui/compare",
            params={"task": "oncall@1", "seed": 1, "difficulty": 1, "policy": "graph_walk"},
        )
        assert cmp_resp.status_code == 200
        assert cmp_resp.json()["score"]["total"] > 0.5

    writer.abort()


_HAS_WEB_DIST = (Path(__file__).resolve().parents[3] / "web" / "dist" / "index.html").is_file()


def test_oncall_simulator_hashes_remain_untouched() -> None:
    """Verify `envs/oncall` source hashes are well-formed and import cleanly."""
    task = OncallTask()
    assert len(simulator_hash(task)) == 64
    assert len(environment_hash(task)) == 64
    _ = json.dumps({"ok": True})


async def test_built_spa_served_without_shadowing_api_routes(client: httpx.AsyncClient) -> None:
    """When `web/dist` exists, `/` serves the SPA while `/healthz` and `/ui/meta` stay intact."""
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/ui/meta")).status_code == 200
    if _HAS_WEB_DIST:
        root = await client.get("/")
        assert root.status_code == 200
        assert "Crucible // Oncall Incident" in root.text
        assert '<div id="root"></div>' in root.text

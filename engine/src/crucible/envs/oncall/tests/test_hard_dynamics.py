"""Tests for anti-shortcut and causal difficulty features of oncall@1."""

# pyright: reportPrivateUsage=false

import pytest

from crucible.core.tools import ToolCall
from crucible.envs.oncall import telemetry
from crucible.envs.oncall.model import State, solve
from crucible.envs.oncall.task import OncallTask
from crucible.envs.oncall.world import World, make_world


def _max_depth(world: World, name: str, memo: dict[str, int] | None = None) -> int:
    if memo is None:
        memo = {}
    if name in memo:
        return memo[name]
    service = next((s for s in world.services if s.name == name), None)
    if not service or not service.dependencies:
        memo[name] = 0
        return 0
    d = 1 + max(_max_depth(world, dep, memo) for dep in service.dependencies)
    memo[name] = d
    return d


def test_opaque_service_names_and_diamond_dag() -> None:
    for seed in range(10):
        w_hard = make_world(seed, "hard-topo", difficulty=0)
        assert len(w_hard.services) == 23
        w_diff1 = make_world(seed, "hard-topo", difficulty=1)
        assert len(w_diff1.services) == 8

        for world, min_depth in ((w_hard, 4), (w_diff1, 3)):
            name_to_idx = {s.name: i for i, s in enumerate(world.services)}
            for i, s in enumerate(world.services):
                for dep in s.dependencies:
                    assert dep in name_to_idx
                    assert name_to_idx[dep] < i

                for suffix in ("-api", "-worker", "-db"):
                    assert suffix not in s.name

            # Check shared dependencies (>= 2 callers)
            caller_counts: dict[str, int] = {}
            for s in world.services:
                for dep in s.dependencies:
                    caller_counts[dep] = caller_counts.get(dep, 0) + 1
            shared = [dep for dep, count in caller_counts.items() if count >= 2]
            assert len(shared) >= 3

            # Check max path depth from "edge-gateway"
            assert _max_depth(world, "edge-gateway") >= min_depth


async def test_global_changes_list_omits_diffs_while_scoped_shows_diffs() -> None:
    task = OncallTask()
    await task.reset(0)
    result = await task.step(ToolCall(name="run", args={"command": "changes list"}))
    assert result.ok
    assert isinstance(result.content, dict)
    rows = result.content["data"]
    assert isinstance(rows, list)
    assert len(rows) > 0
    for row in rows:
        assert isinstance(row, dict)
        assert set(row.keys()) == {"id", "minute", "service", "kind", "author"}
        for key in ("before", "after", "summary"):
            assert key not in row

    first_row = rows[0]
    assert isinstance(first_row, dict)
    chosen_svc = str(first_row["service"])
    scoped = await task.step(ToolCall(name="run", args={"command": f"changes list {chosen_svc}"}))
    assert scoped.ok
    assert isinstance(scoped.content, dict)
    scoped_rows = scoped.content["data"]
    assert isinstance(scoped_rows, list)
    assert len(scoped_rows) > 0
    for row in scoped_rows:
        assert isinstance(row, dict)
        for key in ("before", "after", "summary", "callee"):
            assert key in row


def test_pre_incident_load_ramp_and_exact_zero_minute_demand() -> None:
    world = make_world(42, "load-test")
    state = State(minute=0)
    pods_neg20, _ = solve(world, state, -20)
    pods_zero, _ = solve(world, state, 0)

    gateway_neg20 = sum(p.request_rate for p in pods_neg20 if p.service == "edge-gateway")
    gateway_zero = sum(p.request_rate for p in pods_zero if p.service == "edge-gateway")

    assert gateway_neg20 < gateway_zero

    gateway_zero_pods = [p for p in pods_zero if p.service == "edge-gateway"]
    total_request_rate = sum(p.request_rate for p in gateway_zero_pods)
    assert total_request_rate == pytest.approx(world.entry_rate)


async def test_canary_memory_leak_hides_in_service_average_until_pod_breakdown() -> None:
    seed = next(s for s in range(100) if make_world(s, "canary-leak").fault == "memory_leak")
    task = OncallTask()
    await task.reset(seed, salt="canary-leak")
    world = task._world
    assert world is not None
    culprit = world.culprit

    history = task._state.history
    service_mem = telemetry.metrics(history, "memory", culprit, "service", window=5)
    assert len(service_mem) == 1
    assert float(str(service_mem[0]["value"])) < 700.0

    pod_mem = telemetry.metrics(history, "memory", culprit, "pod", window=5)
    values = [float(str(r["value"])) for r in pod_mem]
    assert max(values) == pytest.approx(1024.0)
    assert min(values) < 400.0

    latest_pods = task._state.history[-1]
    culprit_pods = [p for p in latest_pods if p.service == culprit]
    oom_killed = [p for p in culprit_pods if p.state == "OOMKilled"]
    assert len(oom_killed) == 1


async def test_silent_status_queue_backlog_keeps_pods_running() -> None:
    seed = next(s for s in range(100) if make_world(s, "silent-queue").fault == "queue_backlog")
    task = OncallTask()
    await task.reset(seed, salt="silent-queue")
    world = task._world
    assert world is not None
    culprit = world.culprit

    latest_pods = task._state.history[-1]
    culprit_pods = [p for p in latest_pods if p.service == culprit]
    assert len(culprit_pods) > 0
    for p in culprit_pods:
        assert p.state == "Running"

    q_metrics = telemetry.metrics(task._state.history, "queue_depth", culprit, "service", window=5)
    assert len(q_metrics) == 1
    assert float(str(q_metrics[0]["value"])) > 0.0


async def test_logs_do_not_leak_exact_timeout_or_pool_limits() -> None:
    for fault in ("config_push", "pool_exhaustion"):
        salt = f"leak-check-{fault}"
        seed = next(s for s in range(100) if make_world(s, salt).fault == fault)
        task = OncallTask()
        await task.reset(seed, salt=salt)
        world = task._world
        assert world is not None
        logs = telemetry.logs(task._state.history, world.culprit, "", "ERROR", since=30, page=0)
        forbidden = [
            "handler exception in deployed build",
            "pool exhausted calling",
            "upstream timeout after",
        ]
        for line in logs:
            msg = str(line["message"])
            for f in forbidden:
                assert f not in msg


async def test_alerts_include_herrings_and_upstream_severity_bias() -> None:
    seeds = range(5)
    for seed in seeds:
        task = OncallTask()
        await task.reset(seed, salt="alerts-check")
        alts = telemetry.alerts(task._state.history)
        sev1 = [a for a in alts if a["severity"] == 1]
        assert len(sev1) >= 2

        sev3 = [a for a in alts if a["severity"] == 3]
        assert any(a["service"] == "edge-gateway" for a in sev3)

"""Behavioral checks for the simulation and adversarial scoring boundaries."""

import ast
from pathlib import Path

# White-box tests inspect private simulation state to verify grader boundaries.
# pyright: reportPrivateUsage=false
import pytest

from crucible.core.scoring import Score
from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.envs.oncall import telemetry
from crucible.envs.oncall.policies.bots import GraphWalk, Reference
from crucible.envs.oncall.task import OncallTask
from crucible.envs.oncall.world import FAULTS, make_world


def seed_for(fault: str, salt: str | None = None) -> int:
    return next(seed for seed in range(1000) if make_world(seed, salt).fault == fault)


async def act(task: OncallTask, command: str) -> None:
    result = await task.step(ToolCall(name="run", args={"command": command}))
    assert result.ok, result.error


@pytest.mark.parametrize("seed", range(12))
async def test_no_action_zero(seed: int) -> None:
    task = OncallTask()
    await task.reset(seed, salt="holdout")
    await task.step(ToolCall(name="resolve"))
    assert (await task.grade()).total == 0


def test_world_salt_and_sizes() -> None:
    assert make_world(4, "a") == make_world(4, "a")
    assert make_world(4, "a") != make_world(4, "b")
    assert len(make_world(4).services) == 23
    assert len(make_world(4, difficulty=1).services) == 8


async def test_partial_rollout_has_one_bad_pod_and_consistent_logs() -> None:
    task = OncallTask()
    await task.reset(seed_for("partial_rollout", "pods"), salt="pods")
    world = task._world
    assert world is not None
    rows = [p for p in task._state.history[-1] if p.service == world.culprit]
    assert [p.error_ratio for p in rows] == pytest.approx([0, 0, 0.6])
    lines = telemetry.logs(task._state.history, world.culprit, "", "ERROR", 1, 0)
    assert lines and {str(line["pod"]) for line in lines} == {rows[2].pod}
    assert telemetry.logs(task._state.history, world.culprit, "", "ERROR", 1, 0) == lines


async def test_restarted_leak_recurs_in_private_probe() -> None:
    task = OncallTask()
    await task.reset(seed_for("memory_leak"))
    world = task._world
    assert world is not None
    await act(task, f"kubectl rollout restart deploy/{world.culprit}")
    assert all(p.error_ratio == 0 for p in task._state.history[-1])
    await task.step(ToolCall(name="resolve"))
    before = len(task._state.history)
    score = await task.grade()
    assert not score.flags["verified"]
    assert len(task._state.history) == before
    assert await task.grade() == score


@pytest.mark.parametrize("fault", FAULTS)
async def test_correct_fix_survives_probe(fault: str) -> None:
    task = OncallTask()
    await task.reset(seed_for(fault, "repairs"), salt="repairs")
    world = task._world
    assert world is not None
    if world.fault in ("config_push", "pool_exhaustion", "retry_storm"):
        await act(task, f"config revert {world.culprit} {world.change_id}")
    else:
        await act(task, f"kubectl rollout undo deploy/{world.culprit}")
    if fault == "queue_backlog":
        # Restarting consumption does not erase queued work; wait for it to drain.
        assert not (await task.grade()).flags["verified"]
        await task.step(ToolCall(name="wait", args={"minutes": 30}))
    await task.step(ToolCall(name="resolve"))
    assert (await task.grade()).flags["verified"]


async def test_invalid_calls_spend_budget_without_advancing_clock() -> None:
    task = OncallTask()
    await task.reset(0)
    for _ in range(80):
        result = await task.step(ToolCall(name="run", args={"command": "rm -rf /"}))
        assert not result.ok
    assert task.done() and task._state.minute == 5
    assert (await task.grade()).total == 0


async def test_wrong_config_revision_does_not_fix() -> None:
    task = OncallTask()
    await task.reset(seed_for("config_push"))
    world = task._world
    assert world is not None
    baseline = next(
        c.id
        for c in world.changes
        if c.service == world.culprit and c.kind == "config" and c.minute < 0
    )
    await act(task, f"config revert {world.culprit} {baseline}")
    assert not task._state.repaired
    assert task._state.penalty > 0


async def test_report_does_not_repair() -> None:
    task = OncallTask()
    await task.reset(0)
    world = task._world
    assert world is not None
    await task.step(
        ToolCall(
            name="resolve",
            args={
                "report": {
                    "service": world.culprit,
                    "fault_kind": world.fault,
                    "change_id": world.change_id,
                    "remediation": "rollback",
                }
            },
        )
    )
    assert (await task.grade()).total == 0


def test_public_views_do_not_read_private_fault_fields() -> None:
    for filename in ("telemetry.py", "commands.py"):
        path = Path(__file__).parents[1] / filename
        tree = ast.parse(path.read_text())
        private = {"culprit", "fault", "fingerprint", "change_id"}
        reads = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not reads & private


async def test_cache_restart_propagates_to_gateway() -> None:
    task = OncallTask()
    await task.reset(seed_for("partial_rollout"))
    world = task._world
    assert world is not None
    await act(task, f"kubectl rollout undo deploy/{world.culprit}")
    await act(task, "kubectl rollout restart deploy/session-cache")
    gateway = [p for p in task._state.history[-1] if p.service == "edge-gateway"]
    assert all(p.error_ratio > 0.5 for p in gateway)
    assert task._state.penalty >= 0.2


async def test_recorded_history_keeps_sample_time_and_survives_repair() -> None:
    task = OncallTask()
    await task.reset(seed_for("config_push"))
    world = task._world
    assert world is not None
    query = ToolCall(
        name="query_metrics",
        args={
            "metric": "error_ratio",
            "service": world.culprit,
            "window": 12,
            "end_minute": 5,
            "series": True,
        },
    )
    before = await task.step(query)
    assert isinstance(before.content, dict)
    assert before.content["observed_at"] == 5
    assert before.content["minute"] == 6
    data = before.content["data"]
    assert isinstance(data, list)
    assert any(isinstance(row, dict) and row.get("minute") == -6 for row in data)
    await act(task, f"config revert {world.culprit} {world.change_id}")
    after = await task.step(query)
    assert isinstance(after.content, dict)
    assert after.content["data"] == data
    future = await task.step(
        ToolCall(
            name="query_metrics",
            args={"metric": "error_ratio", "service": world.culprit, "end_minute": 150},
        )
    )
    assert not future.ok


async def test_targeted_deploy_reverts_selected_revision() -> None:
    task = OncallTask()
    await task.reset(seed_for("partial_rollout"))
    world = task._world
    assert world is not None
    await act(task, f"kubectl rollout undo deploy/{world.culprit} --to-revision={world.change_id}")
    assert world.change_id in task._state.reverted_changes
    assert (await task.grade()).flags["verified"]
    invalid = await task.step(
        ToolCall(
            name="run",
            args={"command": f"kubectl rollout undo deploy/{world.culprit} --to-revision=unknown"},
        )
    )
    assert not invalid.ok


async def test_edges_show_effective_limits_and_revert_preserves_other_changes() -> None:
    task = OncallTask()
    await task.reset(seed_for("config_push"))
    world = task._world
    assert world is not None
    before = telemetry.edges(task._state.edge_history, world.culprit, 1)
    await act(task, f"config revert {world.culprit} {world.change_id}")
    after = telemetry.edges(task._state.edge_history, world.culprit, 1)
    assert before and after
    assert before[0]["timeout_ms"] != after[0]["timeout_ms"]
    assert before[0]["pool_size"] == after[0]["pool_size"]
    assert after[0]["error_ratio"] == pytest.approx(0)


def test_deploy_history_does_not_disclose_private_build_effects() -> None:
    for seed in range(20):
        world = make_world(seed)
        for change in world.changes:
            if change.kind == "deploy":
                assert set(change.before) <= {"version"}
                assert set(change.after) <= {"version"}


@pytest.mark.parametrize("seed", [1009, 2017, 4093, 8191])
@pytest.mark.parametrize("policy_type", [GraphWalk, Reference])
async def test_public_rule_solves_heldout_salted_worlds(
    seed: int,
    policy_type: type[GraphWalk],
) -> None:
    task = OncallTask()
    observation = await task.reset(seed, salt="heldout-causal-public-rule-v1")
    policy = policy_type()
    await policy.reset(task.spec, task.tools(), task.instruction())
    while not task.done():
        call = await policy.act(observation)
        if call is None:
            break
        result = await task.step(call)
        assert result.ok, (call, result.error)
        observation = Observation(last_result=result)
    score = await task.grade()
    assert score.flags["verified"], (seed, score)
    assert score.flags["diagnosis_correct"], (seed, score)
    assert score.total >= 0.85, (seed, score)


async def test_service_rate_matches_sum_of_pods_and_edge_admissions() -> None:
    task = OncallTask()
    await task.reset(seed_for("pool_exhaustion"))
    world = task._world
    assert world is not None
    rows = telemetry.metrics(task._state.history, "request_rate", world.culprit, "pod", 1)
    total = sum(float(str(row["value"])) for row in rows)
    service = telemetry.metrics(task._state.history, "request_rate", world.culprit, "service", 1)
    assert float(str(service[0]["value"])) == pytest.approx(total)
    incoming = [edge for edge in task._state.edge_history[-1] if edge.callee == world.culprit]
    assert total == pytest.approx(sum(edge.attempt_rate for edge in incoming), abs=1e-5)


@pytest.mark.parametrize("fault", FAULTS)
async def test_public_rule_recovers_each_fault_with_causal_configuration(fault: str) -> None:
    salt = "integration-fault-coverage"
    task = OncallTask()
    observation = await task.reset(seed_for(fault, salt), salt=salt)
    policy = GraphWalk()
    await policy.reset(task.spec, task.tools(), task.instruction())
    while not task.done():
        call = await policy.act(observation)
        if call is None:
            break
        result = await task.step(call)
        assert result.ok, (call, result.error)
        observation = Observation(last_result=result)
    score = await task.grade()
    assert score.flags["verified"] and score.flags["diagnosis_correct"], (fault, score)
    assert score.total > 0


def test_recovery_semantics_do_not_change_reward_gates() -> None:
    task = OncallTask()
    recovered_late = Score(total=0.64, flags={"verified": True, "diagnosis_correct": True})
    assert task.solved(recovered_late)
    assert recovered_late.total < 0.75
    assert not task.solved(Score(total=0.99, flags={"verified": True, "diagnosis_correct": False}))
    assert not task.solved(Score(total=0.99, flags={"verified": False, "diagnosis_correct": True}))


@pytest.mark.parametrize("onset", [13, 37])
async def test_rule_traces_delayed_overload_to_earlier_caller_onset(onset: int) -> None:
    """Synthetic public evidence; no generated world, service naming or seed assumptions."""
    task = OncallTask()
    policy = GraphWalk()
    await policy.reset(task.spec, task.tools(), task.instruction())
    policy.services = [
        {"name": "storage", "dependencies": []},
        {"name": "reader", "dependencies": ["storage"]},
        {"name": "edge", "dependencies": ["reader"]},
    ]
    policy.health = {"storage": 0.2, "reader": 0.9, "edge": 0.9}
    policy.latencies = {"storage": 10, "reader": 0.2, "edge": 0.3}
    policy.onsets = {"reader": onset, "storage": onset + 8, "edge": onset}
    policy.edges = [
        {
            "caller": "reader",
            "callee": "storage",
            "request_rate": 100,
            "offered_attempt_rate": 190,
            "error_ratio": 0.9,
            "latency": 10,
            "timeout_ms": 90,
            "retries": 1,
            "concurrency": 17,
            "pool_size": 96,
        }
    ]
    policy.changes = [
        {
            "id": "revision-c",
            "service": "reader",
            "callee": "storage",
            "kind": "config",
            "minute": onset,
            "before": {"timeout_ms": 1200},
            "after": {"timeout_ms": 90},
        }
    ]
    policy._choose_target()
    assert policy.target == "reader"
    policy.onset = policy.onsets[policy.target]
    policy.stage = 8
    call = await policy.act(
        Observation(
            last_result=ToolResult(
                ok=True,
                content={
                    "data": [
                        {"message": "upstream timeout after 90ms calling storage; retries=1"},
                    ]
                },
            )
        )
    )
    assert call is not None and call.name == "query_edges"
    assert call.args["end_minute"] == onset - 1
    repair = await policy.act(
        Observation(
            last_result=ToolResult(
                ok=True,
                content={
                    "data": [
                        {"caller": "reader", "callee": "storage", "latency": 0.3, "error_ratio": 0},
                    ]
                },
            )
        )
    )
    assert repair is not None and repair.args == {"command": "config revert reader revision-c"}
    assert policy.report["fault_kind"] == "config_push"
    # A later rolling window must not erase the earliest observed event.
    policy.stage = 5
    await policy.act(
        Observation(
            last_result=ToolResult(
                ok=True,
                content={
                    "data": [
                        {"name": "reader", "minute": onset + 20, "value": 0.9},
                    ]
                },
            )
        )
    )
    assert policy.onsets["reader"] == onset

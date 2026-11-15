"""Causal and reporting boundaries for overlapping incidents."""

# pyright: reportPrivateUsage=false

from dataclasses import replace

import pytest
from pydantic import JsonValue

from crucible.core.tools import Observation, ToolCall, ToolResult
from crucible.envs.oncall.grader import _diagnosis, loss
from crucible.envs.oncall.model import State, advance
from crucible.envs.oncall.overlap import OverlapTask, make_overlap_world
from crucible.envs.oncall.policies.bots import IncidentRunbook, Reference
from crucible.envs.oncall.task import LogsArgs, OncallTask, Report
from crucible.envs.oncall.world import Incident


def test_tool_schemas_avoid_empty_enums_and_preserve_validation() -> None:
    def check(value: JsonValue) -> None:
        if isinstance(value, dict):
            enum = value.get("enum")
            if isinstance(enum, list):
                assert "" not in enum
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for task in (OncallTask(), OverlapTask()):
        for tool in task.tools():
            check(tool.args_model.model_json_schema())
    assert LogsArgs(service="x", level="").level == ""
    assert Report(fault_kind="").fault_kind == ""
    with pytest.raises(ValueError):
        LogsArgs(service="x", level="WARN")
    with pytest.raises(ValueError):
        Report(fault_kind="arbitrary")


def _report(incident: Incident) -> dict[str, str]:
    return {
        "service": incident.service,
        "fault_kind": incident.fault_kind,
        "change_id": incident.change_id,
        "remediation": "config_revert"
        if incident.fault_kind in ("config_push", "pool_exhaustion", "retry_storm")
        else "rollback",
    }


def test_diagnoses_match_once_in_any_order_and_penalize_extra_guesses() -> None:
    incidents = (Incident("a", "memory_leak", "a1"), Incident("b", "config_push", "b1"))
    reports = [_report(i) for i in incidents]
    assert _diagnosis(incidents, reports) == 1
    assert _diagnosis(incidents, reports[::-1]) == 1
    assert _diagnosis(incidents, reports[:1]) == 0.5
    assert _diagnosis(incidents, [reports[0], reports[0]]) < 1
    assert _diagnosis(incidents, [*reports, reports[0]]) == pytest.approx(2 / 3)
    assert _diagnosis(incidents, []) == 0


async def test_runbook_uses_public_memory_growth_and_rollout_history() -> None:
    task, policy = OverlapTask(), IncidentRunbook()
    await policy.reset(task.spec, task.tools(), task.instruction())
    first = await policy.act(Observation())
    assert first is not None and first.name == "query_metrics"
    history = await policy.act(
        Observation(
            last_result=ToolResult(
                ok=True,
                content={
                    "data": [
                        {"name": "reader", "minute": 50, "value": 200.0},
                        {"name": "reader", "minute": 51, "value": 225.0},
                        {"name": "reader", "minute": 52, "value": 250.0},
                        {"name": "storage", "minute": 50, "value": 300.0},
                        {"name": "storage", "minute": 52, "value": 300.0},
                    ]
                },
            )
        )
    )
    assert (
        history is not None and history.args["command"] == "kubectl rollout history deploy/reader"
    )
    repair = await policy.act(
        Observation(
            last_result=ToolResult(
                ok=True,
                content={
                    "data": [
                        {"id": "release-old", "minute": 30},
                        {"id": "release-current", "minute": 49},
                    ]
                },
            )
        )
    )
    assert repair is not None
    assert repair.args["command"] == (
        "kubectl rollout undo deploy/reader --to-revision=release-current"
    )


@pytest.mark.parametrize("seed", range(12))
def test_each_regression_independently_causes_loss_and_both_reverts_restore(seed: int) -> None:
    world = make_overlap_world(seed, "overlap-causal")
    assert world == make_overlap_world(seed, "overlap-causal")
    assert world != make_overlap_world(seed, "different")
    assert len({i.service for i in world.incidents}) == 2
    assert len({i.change_id for i in world.incidents}) == 2
    for repaired in ((), world.incidents[:1], world.incidents[1:], world.incidents):
        state = State(minute=-31, reverted_changes={i.change_id for i in repaired})
        advance(world, state, 151)
        residual = sum(loss(frame) for frame in state.history[-30:])
        assert (residual < 0.01) == (len(repaired) == 2), (seed, repaired, residual)
    # Relabeling private diagnosis metadata cannot change any physical observation.
    relabeled = replace(world, culprit="other", fault="other", change_id="other", incidents=())
    original, changed = State(minute=-31), State(minute=-31)
    advance(world, original, 61)
    advance(relabeled, changed, 61)
    assert original.history == changed.history
    assert original.edge_history == changed.edge_history


async def test_reports_do_not_repair_and_single_report_schema_is_rejected() -> None:
    task = OverlapTask()
    await task.reset(0)
    assert task._world is not None
    invalid = await task.step(ToolCall(name="resolve", args={"report": {}}))
    assert not invalid.ok and not task.done()
    reports: list[JsonValue] = [dict(_report(i)) for i in task._world.incidents]
    result = await task.step(ToolCall(name="resolve", args={"reports": reports}))
    assert result.ok
    score = await task.grade()
    assert score.total == 0
    assert not score.flags["verified"]
    assert score.flags["diagnosis_correct"]


@pytest.mark.parametrize("seed", [1009, 2017, 4093, 8191])
async def test_public_reference_tracks_both_incidents_on_holdout(seed: int) -> None:
    task = OverlapTask()
    obs = await task.reset(seed, salt="overlap-heldout")
    policy = Reference()
    await policy.reset(task.spec, task.tools(), task.instruction())
    while not task.done():
        call = await policy.act(obs)
        assert call is not None
        result = await task.step(call)
        assert result.ok, (call, result.error)
        obs = Observation(last_result=result)
    score = await task.grade()
    assert task.solved(score), (seed, score)
    assert score.total > 0

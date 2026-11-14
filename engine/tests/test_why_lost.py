# pyright: basic
"""Paired replay, post hoc rules, dollar reconciliation and offline output."""

import asyncio
import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from crucible.core.tools import ToolCall
from crucible.core.transcript import read_transcript
from crucible.testing import ScriptedPolicy, play
from crucible.why_lost import build, render, replay, tag_weeks, validate_pair, waterfall


@pytest.fixture
def runs(tmp_path: Path) -> tuple[Path, Path]:
    agent, default = tmp_path / "agent.jsonl", tmp_path / "default.jsonl"

    async def save():
        calls = [
            ToolCall(name="get_status"),
            ToolCall(
                name="set_controls",
                args={
                    "expedite_fraction": 0.3,
                    "supplier_weights": {"S01": 0.5},
                    "approve_alternative": "A01",
                },
            ),
            ToolCall(name="set_controls", args={"supplier_weights": {"S01": 2}}),
            ToolCall(name="set_controls", args={"approve_alternative": "A01"}),
            ToolCall(name="finish"),
        ]
        await play(ScriptedPolicy(calls), 1000, agent, task_ref="supplygym@1")
        await play(ScriptedPolicy([ToolCall(name="finish")]), 1000, default, task_ref="supplygym@1")

    asyncio.run(save())
    return agent, default


def test_replay_and_single_file_cli(runs, tmp_path):
    agent, default = runs
    out = tmp_path / "report.html"
    command = [
        sys.executable,
        "-m",
        "crucible.why_lost",
        "--agent",
        str(agent),
        "--default",
        str(default),
        "--out",
        str(out),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    page = out.read_text(encoding="utf-8")
    assert "W53" in page and "Days 364-364" in page
    assert "weight flip · 1" in page and "backup approval · 1" in page
    assert "Default completion" in page
    assert "<svg" in page and "Revenue" in page
    assert "<script" not in page and "https://" not in page and " src=" not in page
    # Refused approvals must not advance a week or charge a second fee.
    a = asyncio.run(replay(read_transcript(agent)))
    assert len(a["weeks"]) == 53
    assert sum(len(w["new_approvals"]) for w in a["weeks"]) == 1
    assert sum(w["fallback"] for w in a["weeks"]) == 51
    assert sum(w["delta"]["profit"] for w in a["weeks"]) == pytest.approx(a["totals"]["profit"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", 99),
        ("task", "supplygym-fresh@1"),
        ("repeat", 1),
        ("scoring_hash", "different"),
        ("git_commit", "different"),
        ("salted", True),
    ],
)
def test_pair_identity(runs, field, value):
    a, b = (read_transcript(p) for p in runs)
    b = b.model_copy(update={"header": b.header.model_copy(update={field: value})})
    with pytest.raises(ValueError):
        validate_pair(a, b)


def test_tampered_totals_and_results_rejected(runs):
    a = read_transcript(runs[0])
    changed = a.model_copy(deep=True)
    changed.footer.score.breakdown["holding"] += 1
    with pytest.raises(ValueError, match="saved holding"):
        asyncio.run(replay(changed))
    changed = a.model_copy(deep=True)
    changed.steps[0] = changed.steps[0].model_copy(
        update={"result": changed.steps[0].result.model_copy(update={"content": "tampered"})}
    )
    with pytest.raises(ValueError, match="saved result"):
        asyncio.run(replay(changed))


def test_non_default_and_input_overwrite_rejected(runs, tmp_path):
    with pytest.raises(ValueError, match="default controls"):
        build(runs[0], runs[0], tmp_path / "bad.html")
    original = runs[0].read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        build(runs[0], runs[1], runs[0])
    assert runs[0].read_bytes() == original


def test_waterfall_signed_revenue_and_costs():
    default = dict(
        profit=55,
        terminal_credit=5,
        revenue=100,
        purchase=30,
        holding=5,
        expedite=5,
        switching=5,
        stockout=5,
    )
    agent = dict(
        profit=-10,
        terminal_credit=10,
        revenue=80,
        purchase=40,
        holding=10,
        expedite=20,
        switching=20,
        stockout=10,
    )
    svg = waterfall(agent, default)
    assert "Revenue: -$20.00" in svg
    assert "Purchase: -$10.00" in svg
    assert "Terminal Credit: $5.00" in svg
    assert "Agent profit: -$10.00" in svg
    with pytest.raises(ValueError, match="reconcile"):
        waterfall({**agent, "profit": 99}, default)


def test_tag_rules_and_negative_cases():
    base: dict[str, Any] = dict(
        controls={"supplier_weights": {"S01": 1}, "expedite_fraction": 0},
        new_approvals=[],
        approved=set(),
        delta={"expedite": 0, "lost": 0},
        warnings=[],
        health=False,
        fallback=False,
    )
    default = [copy.deepcopy(base) for _ in range(3)]
    agent = copy.deepcopy(default)
    agent[0]["controls"]["supplier_weights"]["S01"] = 0.5
    agent[0].update(warnings=["S01"], health=True)
    agent[1]["controls"]["supplier_weights"]["S01"] = 2
    agent[1].update(health=True, new_approvals=[10], approved={10})
    agent[1]["delta"]["expedite"] = 5
    agent[2]["delta"]["expedite"] = 5
    default[2]["delta"]["lost"] = 2
    tag_weeks(agent, default)
    assert agent[0]["tags"] == []
    assert set(agent[1]["tags"]) == {
        "expedite without stockout",
        "backup approval",
        "weight flip",
        "late reaction",
    }
    assert agent[2]["tags"] == []  # return to neutral, default stockout, no late change
    agent[1]["fallback"] = True
    tag_weeks(agent, default)
    assert "late reaction" not in agent[1]["tags"]


def test_escaping_and_neutral_title(runs):
    run = read_transcript(runs[1])
    data = asyncio.run(replay(run))
    run = run.model_copy(
        update={"header": run.header.model_copy(update={"policy": "<script>alert(1)</script>"})}
    )
    page = render(run, copy.deepcopy(data), copy.deepcopy(data))
    assert "How the AI compared" in page
    assert "&lt;script&gt;" in page and "<script>" not in page


def test_report_not_imported_by_agent_modules():
    root = Path(__file__).parents[1] / "src" / "crucible" / "envs" / "supplygym"
    for path in root.rglob("*.py"):
        assert "crucible.why_lost" not in path.read_text(encoding="utf-8")

"""Reward integrity, conservation, calibration and deterministic replay."""

import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import JsonValue

from crucible.core.jobs import Job
from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter
from crucible.envs.last_siding.task import BUDGET, LastSidingTask
from crucible.envs.last_siding.tests.build_bank import BANK, build, canonical
from crucible.envs.last_siding.tests.demo import PROOF, call, replay
from crucible.envs.last_siding.tests.oracle import continuation, distances
from crucible.envs.last_siding.world import CONTROL_SEED, DEMO_SEED, bank, draw
from crucible.envs.last_siding.yard import shunts, transfer


def test_generated_bank_is_exhaustively_certified() -> None:
    assert json.loads(json.dumps(build())) == json.loads(BANK.read_text())
    layouts = bank()
    keys = {(b.manifests, canonical(b.board, b.caps)) for b in layouts}
    assert len(keys) == len(layouts) >= 100
    for seed in (DEMO_SEED, CONTROL_SEED):
        demo = draw(seed, None)
        assert (demo.manifests, canonical(demo.board, demo.caps)) not in keys
    assert all(5 <= b.optimal <= BUDGET for b in layouts)
    assert draw(3, "repeat") == draw(3, "repeat")
    assert draw(3, "repeat") != draw(3, "different")
    assert len(
        {
            canonical(b.board, b.caps) + b.manifests
            for b in (draw(s, None) for s in range(len(layouts)))
        }
    ) == len(layouts)


def test_fixture_first_move_counterfactuals() -> None:
    for seed, remaining in ((DEMO_SEED, 6), (CONTROL_SEED, 5)):
        layout = draw(seed, None)
        d = distances(layout.caps, layout.manifests)
        assert layout.optimal == d[layout.board] == 6
        costs = {m: d[transfer(layout.board, m)] for m in shunts(layout.board, layout.caps)}
        assert min(costs.values()) == costs[(1, 2, 2)] == 5
        assert costs[(0, 1, 1)] == remaining


@pytest.mark.parametrize(
    "action",
    [
        ToolCall(name="dispatch"),
        ToolCall(name="invent"),
        ToolCall(name="stop", args={"reward": 1}),
        ToolCall(name="preview", args={"move": "dispatch"}),
        *[
            ToolCall(name="shunt", args={"source": "D", "destination": "L", "count": 1, **override})
            for override in (
                {"count": 0},
                {"count": 2},
                {"count": 50},
                {"destination": "D"},
                {"source": "Z"},
                {"destination": "X"},
                {"top": "UNKNOWN"},
                {"count": True},
                {"count": "1"},
            )
        ],
    ],
)
async def test_refusals_are_atomic(action: ToolCall) -> None:
    t = LastSidingTask()
    start = await t.reset(DEMO_SEED)
    grade = await t.grade()
    assert not (await t.step(action)).ok
    assert (await t.step(ToolCall(name="inspect"))).content == start.data
    assert await t.grade() == grade


async def test_preview_then_execute_and_conserve() -> None:
    t = LastSidingTask()
    obs = await t.reset(DEMO_SEED)
    assert t.layout
    inventory = Counter("".join(t.layout.manifests))
    for m in continuation(t.board, t.layout.caps, t.layout.manifests):
        action = call(m)
        arg: JsonValue = action.args if m is not None else "dispatch"
        view = (await t.step(ToolCall(name="inspect"))).content
        preview = ToolCall(name="preview", args={"move": arg})
        expected = await t.step(preview)
        assert expected == await t.step(preview)
        assert (await t.step(ToolCall(name="inspect"))).content == view
        actual = await t.step(action)
        assert actual.ok and expected.content == {"hypothetical": actual.content}
        assert Counter("".join(t.board) + "".join(t.layout.manifests[: t.sent])) == inventory
        if m is None and t.sent == 1:
            assert not (await t.step(action)).ok  # duplicate dispatch, second not ready
            score = await t.grade()
            assert score.total == 0.10 and not t.solved(score)
    score = await t.grade()
    assert score.total == 1 == (await t.grade()).total and t.solved(score)
    for name in ("inspect", "preview", "shunt", "stop", "dispatch"):
        assert not (await t.step(ToolCall(name=name))).ok
    assert await t.grade() == score
    assert (await t.reset(DEMO_SEED)).data == obs.data


async def test_wrong_order_and_budget_boundary() -> None:
    t = LastSidingTask()
    await t.reset(DEMO_SEED)
    # Reach the exact last shunt using five reversible pairs before the 6-shunt solution.
    assert t.layout
    path = continuation(t.board, t.layout.caps, t.layout.manifests)
    for _ in range(5):
        for m in ((0, 1, 1), (1, 0, 1)):
            assert (await t.step(call(m))).ok
    for m in path[:-1]:
        assert (await t.step(call(m))).ok
    assert t.used == BUDGET and not t.done()
    assert not (await t.step(call((0, 1, 1)))).ok
    assert (await t.step(ToolCall(name="dispatch"))).ok
    assert t.done() and (await t.grade()).total == 0.875
    await t.reset(DEMO_SEED)
    for _ in range(BUDGET // 2):
        for m in ((0, 1, 1), (1, 0, 1)):
            assert (await t.step(call(m))).ok
    assert t.done() and (await t.grade()).total == 0


async def test_wrong_manifest_permutation() -> None:
    t = LastSidingTask()
    await t.reset(DEMO_SEED)
    # Reach D=BA legally; equal membership is insufficient.
    for m in ((0, 2, 2), (1, 0, 2), (0, 1, 1), (2, 1, 2), (2, 0, 1)):
        assert (await t.step(call(m))).ok
    assert t.board[0] == "BA" and not (await t.step(call(None))).ok
    assert (await t.grade()).total == 0


async def test_harness_counts_reads_and_errors(tmp_path: Path) -> None:
    t = LastSidingTask()
    session = EpisodeSession(t, DEMO_SEED, TranscriptWriter(tmp_path / "reads.jsonl"))
    await session.start(
        policy_name="scripted", job=Job(task=t.spec.ref, policy="scripted", seed=DEMO_SEED)
    )
    for turn in range(t.spec.max_steps):
        result = await session.step(ToolCall(name="inspect" if turn % 2 else "bad"))
        assert result.ok == bool(turn % 2)
    assert not (await session.step(call((0, 1, 1)))).ok
    assert t.used == 0 and session.steps_used == 64
    reason, score = await session.close()
    assert reason == "budget_exhausted" and score.total == 0


@pytest.mark.parametrize("path", sorted(PROOF.glob("*.jsonl")), ids=lambda p: p.stem)
async def test_saved_replays(path: Path) -> None:
    await replay(path, verbose=False)

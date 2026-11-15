"""Outcome scoring boundaries and fixed-denominator failure handling."""

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.core.episode import InfraError
from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import Observation, ToolCall
from crucible.core.transcript import TranscriptWriter, read_transcript
from crucible.envs.supplygym import scenarios
from crucible.envs.supplygym.outcomes import RUBRIC, RUBRIC_HASH, OutcomeTask, grade_outcomes
from crucible.envs.supplygym.play import Play
from crucible.envs.supplygym.policies import baselines, reference
from crucible.envs.supplygym.policies.reference import Decider, make_decider
from crucible.envs.supplygym.scenarios import Scenario, Suite, aggregate, digest, snapshot


def scenario(index: int = 0) -> Scenario:
    return Scenario(
        id=f"scenario-{index}",
        task="supplygym@2",
        seed=index,
        split="dev",
        weight=1,
        scoring_hash=RUBRIC_HASH,
        initial_state_hash="test",
    )


@pytest.mark.parametrize(
    ("service", "ratio", "expected"),
    [
        (0.98, 0.20, 1.0),
        (0.98, 0.1999, 0.5),
        (0.9799, 0.20, 0.5),
        (0.95, 0.0, 0.5),
        (0.9499, 0.20, 0.0),
        (0.98, -0.0001, 0.0),
    ],
)
def test_verified_threshold_boundaries(service: float, ratio: float, expected: float) -> None:
    play = Play(1000)
    play.env.day, play.weeks = 365, 53
    ledger = play.env.tot
    ledger.update(demand=10000, sales=service * 10000, lost=(1 - service) * 10000)
    ledger["lost"] = ledger["demand"] - ledger["sales"]
    ledger["revenue"] = ledger["sales"] * 15
    ledger["purchase"] = ledger["revenue"] - ratio * 150000
    before = copy.deepcopy(play.env.__dict__)
    assert grade_outcomes(play).total == expected
    assert play.env.day == before["day"]
    assert play.env.tot == before["tot"]
    # Existing terminal profit credit and claimed profit have no scoring effect.
    ledger["profit"] = 1e20
    assert grade_outcomes(play).total == expected


def test_grader_rejects_corrupt_accounting() -> None:
    play = Play(1000)
    play.env.tot["purchase"] = float("nan")
    with pytest.raises(ValueError, match="accounting"):
        grade_outcomes(play)


@pytest.mark.parametrize("weeks", [0, 1, 4, 26, 52])
def test_unfinished_never_default_completes(weeks: int) -> None:
    play = Play(1000)
    for _ in range(weeks):
        play.apply({})
    play.finished = True
    day = play.env.day
    first = grade_outcomes(play)
    assert first.total == 0
    assert not first.flags["completed"]
    assert play.env.day == day < 365
    assert grade_outcomes(play) == first


@pytest.mark.parametrize("task_ref", ["supplygym@2", "supplygym-fresh@2", "supplygym-cala@2"])
async def test_normal_session_entry_points(task_ref: str, tmp_path: Path) -> None:
    task = get_task(task_ref)
    assert isinstance(task, OutcomeTask)
    session = EpisodeSession(task, 1000, TranscriptWriter(tmp_path / "episode.jsonl"))
    await session.start(policy_name="ss", job=Job(task=task_ref, policy="ss", seed=1000))
    assert "remaining weeks on the default rule" not in task.instruction()
    assert "98%" in task.instruction()
    if "cala" in task_ref:
        assert "electronics" in task.instruction()
        assert task.regime == 2
    for _ in range(53):
        assert (await session.step(ToolCall(name="set_controls", args={}))).ok
    reason, score = await session.close()
    assert reason == "task_done"
    assert score.flags["completed"]
    assert score.breakdown["days_completed"] == 365
    assert score.total in (0.0, 0.5, 1.0)
    assert await session.close() == (reason, score)


async def test_budget_exhaustion_and_finish_score_zero(tmp_path: Path) -> None:
    for action in ("get_status", "finish"):
        task = get_task("supplygym@2")
        session = EpisodeSession(task, 1000, TranscriptWriter(tmp_path / f"{action}.jsonl"))
        await session.start(
            policy_name="test", job=Job(task=task.spec.ref, policy="test", seed=1000)
        )
        for _ in range(120 if action == "get_status" else 1):
            await session.step(ToolCall(name=action, args={}))
        _, score = await session.close()
        assert score.total == 0
        assert score.breakdown["days_completed"] == 0


async def test_v2_reference_is_deterministic_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: list[str] = []

    def capture(name: str) -> Decider:
        selected.append(name)
        return make_decider(name)

    monkeypatch.setattr(baselines, "make_decider", capture)
    task = get_task("supplygym-cala@2")
    policy = get_policy("reference", task.spec.ref)
    await policy.reset(task.spec, task.tools(), task.instruction())
    assert selected == ["availability"]


@pytest.mark.parametrize("name", ["base_stock", "fixed_lot", "fill_adjusted_base_stock"])
async def test_replenishment_is_validated_and_persists(name: str) -> None:
    task = get_task("supplygym@2")
    assert isinstance(task, OutcomeTask)
    await task.reset(1000)
    assert (await task.step(ToolCall(name="set_controls", args={"replenishment": name}))).ok
    assert (await task.step(ToolCall(name="set_controls"))).ok
    before = snapshot(task)
    assert before["replenishment"] == name
    assert not (
        await task.step(ToolCall(name="set_controls", args={"replenishment": "unknown"}))
    ).ok
    assert snapshot(task) == before
    assert (await task.step(ToolCall(name="set_controls", args={"replenishment": "ss"}))).ok
    assert snapshot(task)["replenishment"] == "ss"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("ss", 200), ("base_stock", 150), ("fixed_lot", 100), ("fill_adjusted_base_stock", 250)],
)
def test_daily_formulas_use_only_known_receipts(name: str, expected: int) -> None:
    env: Any = Play(1000).env
    env.record = True
    env.replenishment = name
    env.fill[0] = 0.5
    env.orders = [
        {"sup": 0, "rev": 7, "ordered": 200, "qty": 3},
        {"sup": 0, "rev": 0, "ordered": 50, "qty": 20},
    ]
    # Default position is 150 below s. The fill estimate removes 100 units.
    s, _ = env.levels(env.active(), 0)
    env.onhand = s - 370
    costs: dict[str, Any] = {"purchase": 0.0, "expedite": 0.0, "_orders": []}
    env._place_orders(env.ctrl, costs)
    # Per-supplier rounding can differ from the intended total by up to half a unit each.
    requested = sum(row[1] for row in costs["_orders"])
    assert abs(requested - expected) <= len(env.active()) / 2


def test_four_of_ten_is_point_four_and_infrastructure_stays_scheduled() -> None:
    scheduled = [scenario(i) for i in range(10)]
    rows: dict[str, dict[str, Any]] = {
        s.id: {"status": "completed", "score": 1.0} for s in scheduled[:4]
    }
    result = aggregate(scheduled, rows)
    assert result["overall_score"] == 0.4
    assert result["scheduled_weight"] == 10
    assert not result["valid_for_comparison"]
    for s in scheduled[4:]:
        rows[s.id] = {"status": "unfinished", "score": 0.0}
    assert aggregate(scheduled, rows)["valid_for_comparison"]
    rows[scheduled[4].id] = {"status": "infrastructure_failure", "score": None}
    assert aggregate(scheduled, rows)["overall_score"] == 0.4
    assert not aggregate(scheduled, rows)["valid_for_comparison"]
    scheduled[0] = scheduled[0].model_copy(update={"weight": 3.0})
    assert aggregate(scheduled, rows)["overall_score"] == 0.5
    rows[scheduled[0].id]["score"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        aggregate(scheduled, rows)


def test_schedule_validation() -> None:
    args: dict[str, Any] = dict(name="test", source_hash="test", rubric=RUBRIC.model_dump())
    with pytest.raises(ValidationError, match="unique"):
        Suite(**args, scenarios=(scenario(), scenario()))
    with pytest.raises(ValidationError, match="exactly one split"):
        Suite(
            **args,
            scenarios=(
                scenario(),
                scenario().model_copy(update={"id": "other", "split": "heldout"}),
            ),
        )
    with pytest.raises(ValidationError):
        Scenario.model_validate({**scenario().model_dump(), "weight": float("nan")})


async def test_retry_from_same_state_preserves_both_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = get_task("supplygym@2")
    assert isinstance(task, OutcomeTask)
    await task.reset(0)
    item = scenario().model_copy(update={"initial_state_hash": digest(snapshot(task))})
    suite = Suite(name="test", source_hash="source", rubric=RUBRIC.model_dump(), scenarios=(item,))
    monkeypatch.setattr(scenarios, "source_digest", lambda: "source")
    attempts = 0

    class FailingOnce:
        name = "test"

        async def reset(self, *args: Any) -> None:
            pass

        async def act(self, observation: Observation) -> ToolCall | None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise InfraError("transport unavailable")
            return None

    def factory(*args: Any) -> FailingOnce:
        return FailingOnce()

    monkeypatch.setattr(scenarios, "get_policy", factory)
    report = await scenarios.run_suite(suite, "dev", "test", tmp_path / "run")
    row = report["scenarios"][0]
    assert [a["status"] for a in row["attempts"]] == ["infrastructure_failure", "unfinished"]
    assert report["overall_score"] == 0
    assert report["valid_for_comparison"]
    for attempt in row["attempts"]:
        assert read_transcript(Path(attempt["trace"])).footer.score.total == 0
    first, second = await asyncio.gather(
        *(asyncio.to_thread(Path(a["initial_state"]).read_text) for a in row["attempts"])
    )
    assert first == second
    with pytest.raises(FileExistsError):
        await scenarios.run_suite(suite, "dev", "test", tmp_path / "run")


async def test_infrastructure_exhaustion_keeps_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = scenario()
    suite = Suite(name="test", source_hash="source", rubric=RUBRIC.model_dump(), scenarios=(item,))
    monkeypatch.setattr(scenarios, "source_digest", lambda: "source")
    # A mismatched initial state is a verifier failure and cannot become an agent failure.
    report = await scenarios.run_suite(suite, "dev", "do_nothing", tmp_path / "run")
    assert len(report["scenarios"][0]["attempts"]) == 3
    assert report["scenarios"][0]["score"] is None
    assert report["scheduled_weight"] == 1
    assert not report["valid_for_comparison"]
    assert report["overall_score"] == 0


async def test_audit_replays_calls_and_rejects_tampered_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = get_task("supplygym@2")
    assert isinstance(task, OutcomeTask)
    await task.reset(0)
    item = scenario().model_copy(update={"initial_state_hash": digest(snapshot(task))})
    suite = Suite(name="test", source_hash="source", rubric=RUBRIC.model_dump(), scenarios=(item,))
    monkeypatch.setattr(scenarios, "source_digest", lambda: "source")
    report = await scenarios.run_suite(suite, "dev", "ss", tmp_path / "run")
    path = tmp_path / "run" / "report.json"
    audited = await scenarios.audit_report(path)
    assert audited["overall_score"] == report["overall_score"]
    # Choose a different score even if the measured result was 0.5.
    report["scenarios"][0]["score"] = 0.25
    scenarios.write_json(path, report)
    with pytest.raises(ValueError, match="Replay score"):
        await scenarios.audit_report(path)


@pytest.mark.parametrize("salt", ["", "private-salt"])
async def test_cala_upper_bound_is_fixed_and_grading_is_read_only(salt: str) -> None:
    from crucible.envs.supplygym.cala_upper_bound import surplus_upper_bound

    task = get_task("supplygym-cala@3")
    await task.reset(1000, salt=salt)
    assert isinstance(task, OutcomeTask)
    initial_play = Play(1000, 2, salt)
    expected = surplus_upper_bound(initial_play) / 100
    initial = snapshot(task)
    assert expected > 0
    assert (await task.grade()).total == 0
    assert snapshot(task) == initial
    for _ in range(53):
        assert (await task.step(ToolCall(name="set_controls", args={}))).ok
    before = snapshot(task)
    score = await task.grade()
    assert score.flags["completed"]
    assert score.breakdown["surplus_upper_bound"] == expected
    assert score.breakdown["terminal_surplus"] <= expected
    floor = score.breakdown["surplus_floor"]
    assert score.total == pytest.approx(
        max(0, min(1, (score.breakdown["adjusted_surplus"] - floor) / (expected - floor)))
    )
    assert score.total < 0.3
    assert snapshot(task) == before
    assert await task.grade() == score


async def test_cala_v3_reference_never_loads_ppo(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: list[str] = []

    def capture(name: str) -> Decider:
        selected.append(name)
        return make_decider(name)

    def reject_checkpoint(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Cala @3 must not construct a PPO policy")

    monkeypatch.setattr(reference, "PpoDecider", reject_checkpoint)
    monkeypatch.setattr(baselines, "make_decider", capture)
    task = get_task("supplygym-cala@3")
    observation = await task.reset(1000)
    policy = get_policy("reference", task.spec.ref)
    await policy.reset(task.spec, task.tools(), task.instruction())
    while not task.done():
        action = await policy.act(observation)
        assert action is not None
        result = await task.step(action)
        assert result.ok
        observation = Observation(last_result=result)
    assert (await task.grade()).total > 0
    assert selected == ["service_availability"]


@pytest.mark.parametrize(
    "opening_onhand,opening_pipeline", [(0, 100000), (100000, 0), (50000, 50000)]
)
def test_terminal_certificate_counts_prepaid_units_once(
    opening_onhand: int,
    opening_pipeline: int,
) -> None:
    from crucible.envs.supplygym.cala_upper_bound import surplus_upper_bound
    from crucible.envs.supplygym.sim import TERMINAL_VALUE

    play = Play(1000, 2)
    env: Any = play.env
    env.onhand = opening_onhand
    # This prepaid shipment never arrives during the year but retains salvage value.
    env.orders = [
        {
            "sup": 0,
            "arr": 1000,
            "nom": 1000,
            "rev": 0,
            "qty": opening_pipeline,
            "ordered": opening_pipeline,
        }
    ]
    bound = surplus_upper_bound(play)
    demand = sum(int(value) for value in env.tape.demand[: env.horizon])
    assert bound == 490 * demand + 1010 * (opening_onhand + opening_pipeline)
    # The full simulator must obey the *unclipped* dollar certificate.
    for _ in range(53):
        play.apply({})
    terminal = TERMINAL_VALUE * (env.onhand + sum(order["qty"] for order in env.orders))
    operating = grade_outcomes(play).breakdown["operating_surplus"]
    assert operating + terminal <= bound / 100
    with pytest.raises(ValueError, match="reset"):
        surplus_upper_bound(play)


async def test_v3_floor_is_salted_deterministic_and_does_not_leak() -> None:
    floors: list[float] = []
    for salt in ["", "private-salt", "private-salt"]:
        task = get_task("supplygym-cala@3")
        observation = await task.reset(1000, salt=salt)
        score = await task.grade()
        floors.append(score.breakdown["surplus_floor"])
        assert score.total == 0.0
        assert "surplus_floor" not in observation.model_dump_json()
        assert "surplus_upper_bound" not in observation.model_dump_json()
        assert isinstance(task, OutcomeTask)
        before = snapshot(task)
        for _ in range(120):
            if task.done():
                break
            await task.step(ToolCall(name="get_status"))
        assert (await task.grade()).total == 0.0
        assert snapshot(task)["day"] == before["day"] == 0
    assert floors[0] != floors[1]
    assert floors[1] == floors[2]


@pytest.mark.parametrize("weeks", [1, 26, 52])
async def test_v3_incomplete_and_early_completion_stay_zero(weeks: int) -> None:
    task = get_task("supplygym-cala@3")
    await task.reset(1000)
    for _ in range(weeks):
        assert (await task.step(ToolCall(name="set_controls"))).ok
    assert (await task.grade()).total == 0.0
    assert (await task.step(ToolCall(name="finish"))).ok
    assert task.done()
    assert (await task.grade()).total == 0.0

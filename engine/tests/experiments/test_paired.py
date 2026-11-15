"""The statistical units are worlds, with missing/invalid trials kept visible."""

from crucible.experiments.paired import OPERATORS, PilotSpec, Trial, analyze

HASH = "a" * 64


def spec() -> PilotSpec:
    return PilotSpec(
        experiment_id="test-only",
        candidate_sha256=HASH,
        world_ids=("a", "b", "c"),
        reference_verified=True,
        model_settings={"model": "synthetic-unit-test"},
        rules={},
    )


def trials() -> list[Trial]:
    return [
        Trial(
            experiment_id="test-only",
            candidate_sha256=HASH,
            world_id=world,
            initial_state_sha256=HASH,
            operator=operator,
            repeat=repeat,
            status="valid",
            reward=0.9 if operator == "reference" else 0.1 + repeat * 0.1,
            utility=90 if operator == "reference" else 10 + repeat * 10,
            transcript="synthetic-unit-test.jsonl",
            attributions=("Test action lost test value",),
        )
        for world in ("a", "b", "c")
        for operator in OPERATORS
        for repeat in range(3 if operator == "agent" else 1)
    ]


def test_average_repeats_before_pairing() -> None:
    result = analyze(spec(), trials())
    assert result["conclusion"] == "Agent performs worse"
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert summary["independent_worlds"] == 3
    assert abs(float(str(summary["mean_paired_reward_gap"])) - 0.7) < 1e-9
    assert summary["reference_wins"] == 3


def test_missing_repeat_or_unverified_reference_is_inconclusive() -> None:
    assert analyze(spec(), trials()[:-1])["conclusion"] == "Evaluation inconclusive"
    unverified = spec().model_copy(update={"reference_verified": False})
    assert analyze(unverified, trials())["conclusion"] == "Evaluation inconclusive"


def test_initial_state_mismatch_invalidates_pair() -> None:
    rows = trials()
    rows[0] = rows[0].model_copy(update={"initial_state_sha256": "b" * 64})
    assert analyze(spec(), rows)["conclusion"] == "Evaluation inconclusive"


def test_failed_attempts_stay_visible_after_successful_retry() -> None:
    rows = trials()
    failed = rows[0].model_copy(update={"status": "infra_error", "reward": None, "utility": None})
    rows[0] = rows[0].model_copy(update={"attempt": 1})
    result = analyze(spec(), [failed, *rows])
    assert result["conclusion"] == "Agent performs worse"
    assert len(result["failed_attempts"]) == 1  # type: ignore[arg-type]
    rows[0] = rows[0].model_copy(update={"status": "infra_error"})
    assert analyze(spec(), rows)["conclusion"] == "Evaluation inconclusive"


def test_positive_gap_requires_attribution() -> None:
    rows = [r.model_copy(update={"attributions": ()}) for r in trials()]
    assert analyze(spec(), rows)["conclusion"] == "No demonstrated disadvantage"


def test_tie_or_better_agent_does_not_manufacture_gap() -> None:
    rows = [r.model_copy(update={"reward": 0.9}) for r in trials()]
    assert analyze(spec(), rows)["conclusion"] == "No demonstrated disadvantage"
    rows = [r.model_copy(update={"reward": 1.0}) if r.operator == "agent" else r for r in rows]
    assert analyze(spec(), rows)["conclusion"] == "No demonstrated disadvantage"

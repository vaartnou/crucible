"""CLI tests: run through `CliRunner`, and `summarise` on hand-made results."""

import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crucible.cli import app, summarise
from crucible.core.episode import EpisodeResult
from crucible.core.jobs import Job
from crucible.core.scoring import Score

runner = CliRunner()


def _result(policy: str, seed: int, total: float, flags: dict[str, bool]) -> EpisodeResult:
    return EpisodeResult(
        job=Job(task="t@1", policy=policy, seed=seed),
        task="t@1",
        policy=policy,
        seed=seed,
        score=Score(total=total, flags=flags),
        steps=1,
        stop_reason="task_done",
        transcript_path=Path("x.jsonl"),
    )


def _eval(out: Path, *extra: str) -> tuple[int, str]:
    args = ["eval", "--task", "guess@1", "--policy", "reference,do_nothing", "--out", str(out)]
    result = runner.invoke(app, [*args, *extra])
    return result.exit_code, result.output


def test_eval_guess(tmp_path: Path) -> None:
    code, output = _eval(tmp_path, "--seeds", "0-3")
    assert code == 0
    assert "reference" in output and "do_nothing" in output
    with (tmp_path / "summary.csv").open() as f:
        assert len(list(csv.DictReader(f))) == 8


def test_eval_resumes(tmp_path: Path) -> None:
    first = _eval(tmp_path, "--seeds", "0-3")
    second = _eval(tmp_path, "--seeds", "0-3")
    assert first == second
    assert first[0] == 0


def test_per_seed(tmp_path: Path) -> None:
    _, output = _eval(tmp_path, "--seeds", "0-1", "--per-seed")
    assert "Episodes" in output


def test_unknown_policy(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["eval", "--task", "guess@1", "--policy", "nope", "--out", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "Unknown policy 'nope'" in result.output
    assert "Traceback" not in result.output


def test_unknown_task(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", "--task", "nope@1", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "Unknown task" in result.output


@pytest.mark.parametrize("seeds", ["abc", "5-2", "1,1"])
def test_bad_seeds(tmp_path: Path, seeds: str) -> None:
    code, output = _eval(tmp_path, "--seeds", seeds)
    assert code == 2
    assert "eed" in output
    assert "Traceback" not in output


def test_summarise() -> None:
    results = [
        _result("a", 0, 1.0, {"root_cause_correct": True, "env_error": False}),
        _result("a", 1, 0.5, {"root_cause_correct": False, "env_error": False}),
        _result("b", 0, 0.0, {}),
    ]
    rows = summarise(results, ["b", "a"])
    assert [r.policy for r in rows] == ["b", "a"]
    b, a = rows
    assert (a.episodes, a.mean, a.min, a.max) == (2, 0.75, 0.5, 1.0)
    assert a.shares["root_cause_correct"] == 0.5
    assert a.shares["env_error"] == 0.0
    assert a.shares["probes_healthy"] is None
    assert b.mean == 0.0 and b.shares["env_error"] == 0.0


def test_tasks_and_policies() -> None:
    tasks = runner.invoke(app, ["tasks"]).output.split()
    assert "guess@1" in tasks and "guess@1" in tasks
    policies = runner.invoke(app, ["policies", "--task", "guess@1"]).output.split()
    for name in ("reference", "do_nothing"):
        assert name in policies
    assert runner.invoke(app, ["policies", "--task", "nope@1"]).exit_code == 2


def test_summarise_env_error_defaults_to_zero_but_other_flags_stay_absent() -> None:
    results = [_result("p", 0, 1.0, {}), _result("p", 1, 0.5, {"env_error": True})]
    (row,) = summarise(results, ["p"])
    assert row.shares["env_error"] == 0.5
    assert row.shares["root_cause_correct"] is None
    (clean,) = summarise([_result("q", 0, 1.0, {})], ["q"])
    assert clean.shares["env_error"] == 0.0


def test_eval_clean_run_shows_zero_env_error(tmp_path: Path) -> None:
    _, output = _eval(tmp_path, "--seeds", "0-1")
    assert "0.000" in output

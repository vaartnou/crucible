"""Statistical oracles and saved-run validation for the paired gap report."""

import csv
from pathlib import Path

import pytest

from crucible.gap_proof import bootstrap_ci, load_runs, main, report, sign_test


def save(path: Path, rows: list[tuple[str, int, int, float]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["task", "policy", "seed", "repeat", "total"])
        writer.writerows(("t@1", *row) for row in rows)
    return path


@pytest.mark.parametrize(
    ("wins", "losses", "expected"),
    [(0, 0, 1), (10, 0, 2 / 1024), (0, 10, 2 / 1024), (3, 1, 0.625), (5, 5, 1)],
)
def test_exact_sign_test(wins: int, losses: int, expected: float) -> None:
    assert sign_test(wins, losses) == pytest.approx(expected)
    assert 0 <= sign_test(1000, 1000) <= 1


def test_paired_bootstrap() -> None:
    # Analytic percentile endpoints for resampling two observations: -1, 0, 1.
    def ci(values: list[float]) -> tuple[float, float]:
        return bootstrap_ci(values, samples=2000, alpha=0.05, rng_seed=21)

    assert ci([-1, 1]) == (-1, 1)
    assert ci([0.25] * 10) == (0.25, 0.25)
    assert ci([0]) == (0, 0)
    assert ci([-1, 0, 2]) == ci([-1, 0, 2])


def test_repeat_means_and_full_report(tmp_path: Path) -> None:
    path = save(
        tmp_path / "summary.csv",
        [
            ("a", 0, 0, 1),
            ("a", 0, 1, 0),
            ("b", 0, 0, 0),
            ("b", 0, 1, 0),
            ("a", 1, 0, 0),
            ("b", 1, 0, 0.5),
        ],
    )
    scores = load_runs([path], "t@1", ["a", "b"], [0, 1])
    text = report(
        scores,
        ["a", "b"],
        [0, 1],
        hard_seeds=[0],
        default="b",
        ceiling="a",
        samples=1000,
        alpha=0.05,
        rng_seed=21,
    )
    assert "| all | a | b | 2 | 0.25 | 0.25 | 0 | -0.5 | 0.5 | 1 | 0 | 1 | 0.5 | 1 |" in text
    assert "| hard (exploratory) | a | b | 1 |" in text
    assert "| all | a | 1 | 1 | 1 | 1 | 1 |" in text  # one undefined normalized gap
    assert "| 0 | 2 | yes | a | b | 0.5 | 0 | 0.5 |" in text
    assert "| 1 | 1 | no | a | b | 0 | 0.5 | -0.5 |" in text


@pytest.mark.parametrize(
    "rows",
    [
        [("a", 0, 0, 1)],  # missing agent
        [("a", 0, 0, 1), ("b", 0, 1, 0)],  # unequal repeat IDs
        [("a", 0, 0, 1), ("a", 0, 0, 1), ("b", 0, 0, 0)],  # duplicate
        [("a", 0, 0, float("nan")), ("b", 0, 0, 0)],
        [("a", 0, -1, 1), ("b", 0, -1, 0)],
    ],
)
def test_reject_invalid_pairing(tmp_path: Path, rows: list[tuple[str, int, int, float]]) -> None:
    path = save(tmp_path / "summary.csv", rows)
    with pytest.raises(ValueError):
        load_runs([path], "t@1", ["a", "b"], [0])


def test_saved_runs_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = save(tmp_path / "summary.csv", [("a", 0, 0, 1), ("b", 0, 0, 0)])
    args = ["--runs", str(path.parent), "--task", "t@1", "--agents", "a,b", "--seeds", "0"]
    main(args)
    first = capsys.readouterr().out
    main(args)
    assert capsys.readouterr().out == first
    assert "| all | a | b | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 0 | 0 | 1 | 1 |" in first
    with pytest.raises(SystemExit) as exc:
        main([*args, "--seeds", "0-1"])
    assert exc.value.code == 2


def test_csv_aliases_and_failed_episode(tmp_path: Path) -> None:
    path = tmp_path / "external.csv"
    path.write_text(
        "task,agent,seed,repeat,score,stop_reason\nt@1,a,0,0,1,error\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="episode ended with error"):
        load_runs([path], "t@1", ["a"], [0])
    path.write_text("task,agent,seed,repeat,score\nt@1,a,0,0,1\nt@1,b,0,0,0\n", encoding="utf-8")
    assert load_runs([path], "t@1", ["a", "b"], [0])["a"][0][0] == 1


async def test_real_saved_episodes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from crucible.core.episode import EpisodeResult, run_episode
    from crucible.core.executor import _write_summary  # pyright: ignore[reportPrivateUsage]
    from crucible.core.jobs import Job
    from crucible.core.transcript import TranscriptWriter
    from crucible.envs.guess.policies.reference import BinarySearch
    from crucible.envs.guess.task import GuessNumber
    from crucible.policies.baselines import DoNothing

    results: list[EpisodeResult] = []
    for seed in range(3):
        for policy in (BinarySearch(), DoNothing()):
            job = Job(task="guess@1", policy=policy.name, seed=seed)
            results.append(
                await run_episode(
                    GuessNumber(),
                    policy,
                    seed,
                    job=job,
                    writer=TranscriptWriter(tmp_path / f"{job.job_id}.jsonl"),
                )
            )
    assert all(r.stop_reason != "error" for r in results)
    _write_summary(tmp_path / "summary.csv", results)
    main(
        [
            "--runs",
            str(tmp_path),
            "--task",
            "guess@1",
            "--agents",
            "reference,do_nothing",
            "--seeds",
            "0-2",
        ]
    )
    assert "| all | reference | do_nothing | 3 |" in capsys.readouterr().out

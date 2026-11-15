"""Tests for demo scheduling and private sidecar mount configuration."""

import importlib.util
import json
import subprocess
from argparse import Namespace
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from crucible.export.harbor import EngineArtifacts, task_folder_name

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_cala_demo.py"
SPEC = importlib.util.spec_from_file_location("run_cala_demo", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_concurrency_does_not_limit_dataset(tmp_path: Path) -> None:
    args = Namespace(harbor="harbor", model="test/model", key_env="TEST_KEY", concurrency=1)
    command = runner.harbor_command(args, tmp_path, "test-secret")
    assert command[command.index("-n") + 1] == "1"
    assert "-l" not in command


def write_trial(out: Path, score: dict[str, Any]) -> Path:
    name = task_folder_name(runner.TASK, 0)
    trial = out / "jobs" / "opencode" / f"{name}__abc123"
    trace = trial / "verifier" / "transcript.json"
    trace.parent.mkdir(parents=True)
    (trial / "result.json").write_text("{}", encoding="utf-8")
    trace.write_text(json.dumps([{"score": score}]), encoding="utf-8")
    return trace


async def test_demo_forwards_key_and_reports_judging_baselines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "demo"
    secret = "some-provider-key=test-secret"
    monkeypatch.setenv("TEST_KEY", secret)
    wheel = tmp_path / "crucible-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"mock wheel")
    requirements = tmp_path / "requirements.txt"
    requirements.touch()
    artifacts = EngineArtifacts(wheel=wheel, requirements=requirements)

    def fake_build(_: Path) -> EngineArtifacts:
        return artifacts

    monkeypatch.setattr(runner, "build_engine_artifacts", fake_build)

    def fake_harbor(command: list[str], **kwargs: object) -> nullcontext[SimpleNamespace]:
        assert command[command.index("--api-key-name") + 1] == "TEST_KEY"
        assert command[command.index("--ae") + 1] == f"TEST_KEY={secret}"
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        # Stand in for Harbor by replaying a completed baseline as the live stream.
        events = runner.read_events(out / "baselines/seed-0/reference/events.jsonl")
        live = out / "live/seed-0/events.jsonl"
        with live.open("w", encoding="utf-8") as stream:
            for event in events:
                event.update(strategy="opencode", run_id="fake-opencode")
                stream.write(json.dumps(event) + "\n")
        write_trial(out, events[-1]["score"])
        output = StringIO(" ".join(command) + "\n")
        return nullcontext(SimpleNamespace(stdout=output, wait=lambda: 0))

    monkeypatch.setattr(
        runner,
        "subprocess",
        SimpleNamespace(Popen=fake_harbor, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT),
    )
    args = Namespace(
        harbor="harbor",
        model="test/model",
        key_env="TEST_KEY",
        prompt_key=False,
        concurrency=1,
        start_seed=0,
        count=1,
        out=out,
    )
    assert await runner.run(args) == 0

    captured = capsys.readouterr()
    assert "--ae TEST_KEY=<redacted>" in captured.out
    assert secret not in captured.out + captured.err
    for metadata in (*out.glob("*.json"), out / "dataset/manifest.json"):
        assert secret not in metadata.read_text(encoding="utf-8")

    policies = {
        "reference",
        "ss",
        "base_stock",
        "fixed_lot",
        "fill_adjusted_base_stock",
        "do_nothing",
        "stop_early",
    }
    assert policies == set(runner.BASELINES)
    (scenario,) = json.loads((out / "spectator-manifest.json").read_text(encoding="utf-8"))
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["harbor_exit_code"] == 0
    assert report["valid_for_comparison"] is True
    assert set(scenario["events"]) == set(report["aggregates"]) == policies | {"opencode"}
    for strategy, path in scenario["events"].items():
        events = runner.read_events(out / path)
        assert {event["strategy"] for event in events} == {strategy}
        assert {event["scenario_comparison_id"] for event in events} == {
            scenario["scenario_comparison_id"]
        }
        (final,) = [event for event in events if event["event"] == "final"]
        assert final == events[-1]
        assert report["aggregates"][strategy]["mean_score"] == final["score"]["total"]
        if strategy in {"do_nothing", "stop_early"}:
            assert final["score"]["total"] == 0.0
            assert final["completed"] is False
        else:
            assert final["completed"] is True


@pytest.mark.parametrize(
    ("damaged", "contents", "exit_code", "status"),
    [
        ("events", '{"event": "week"', 0, "invalid_trial_artifacts"),
        ("trace", "", 0, "missing_verified_result"),
        ("trace", "[]", 0, "missing_verified_result"),
        ("result", '{"exception_info":', 2, "invalid_trial_artifacts"),
        ("result", '{"exception_info": {"type": "TimeoutError"}}', 2, "trial_error"),
        ("result", "{}", 2, "verified"),
    ],
)
def test_collect_results_preserves_failure_status(
    tmp_path: Path, damaged: str, contents: str, exit_code: int, status: str
) -> None:
    score = {"total": 0.5, "breakdown": {"surplus_upper_bound": 200, "operating_surplus": 100}}
    final = {
        "event": "final",
        "score": score,
        "scenario_comparison_id": "scenario",
        "scorer_version": "scorer",
        "simulator_version": "simulator",
    }
    trace = write_trial(tmp_path, score)
    live = tmp_path / "live/seed-0/events.jsonl"
    for path in (
        live,
        *(tmp_path / "baselines/seed-0" / policy / "events.jsonl" for policy in runner.BASELINES),
    ):
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(final) + "\n", encoding="utf-8")

    files = {"events": live, "trace": trace, "result": trace.parents[1] / "result.json"}
    files[damaged].write_text(contents, encoding="utf-8")
    assert not runner.collect_results(tmp_path, {0: 200.0}, exit_code)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["harbor_exit_code"] == exit_code
    assert report["valid_for_comparison"] is False
    assert report["scenarios"][0]["status"] == status
    assert "aggregates" not in report


def test_spectator_mount_is_sidecar_only(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  main: {}\n  mcp-server:\n    environment: {}\n", encoding="utf-8"
    )
    events = tmp_path / "private" / "events.jsonl"
    runner.configure_spectator(compose, events, "unique-run")
    config = yaml.safe_load(compose.read_text(encoding="utf-8"))
    assert "volumes" not in config["services"]["main"]
    sidecar = config["services"]["mcp-server"]
    assert sidecar["volumes"][0]["source"] == events.resolve().as_posix()
    assert sidecar["environment"]["SPECTATOR_RUN_ID"] == "unique-run"
    assert events.is_file()

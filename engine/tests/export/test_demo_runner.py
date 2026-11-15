"""Tests for demo scheduling and private sidecar mount configuration."""

import importlib.util
from argparse import Namespace
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_cala_demo.py"
SPEC = importlib.util.spec_from_file_location("run_cala_demo", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_concurrency_does_not_limit_dataset(tmp_path: Path) -> None:
    args = Namespace(harbor="harbor", model="test/model", key_env="TEST_KEY", concurrency=1)
    command = runner.harbor_command(args, tmp_path)
    assert command[command.index("-n") + 1] == "1"
    assert "-l" not in command
    assert "TEST_KEY" in command
    assert command[command.index("--ae") + 1] == "TEST_KEY=${TEST_KEY}"
    assert "secret" not in command


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
    assert sidecar["volumes"][0]["target"] == "/app/spectator-events.jsonl"
    assert sidecar["environment"]["SPECTATOR_PATH"] == "/app/spectator-events.jsonl"
    assert sidecar["environment"]["SPECTATOR_RUN_ID"] == "unique-run"
    assert events.is_file()


def test_dotenv_reader_and_baseline_set(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('# comment\nexport ANTHROPIC_API_KEY="sk-test-value"\n', encoding="utf-8")
    assert runner.read_dotenv_key("ANTHROPIC_API_KEY", [dotenv]) == "sk-test-value"
    assert runner.read_dotenv_key("MISSING_KEY", [dotenv]) == ""
    for expected in ("reference", "fill_adjusted_base_stock", "ss", "stop_early", "do_nothing"):
        assert expected in runner.BASELINES

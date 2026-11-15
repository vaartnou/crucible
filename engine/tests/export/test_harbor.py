"""Harbor export: folder layout, config files and where the secrets go. No Docker."""

import itertools
import json
import tomllib
from pathlib import Path

import click
import pytest
import yaml
from typer.testing import CliRunner

from crucible.cli import app
from crucible.core import registry
from crucible.core.registry import get_task, register_task
from crucible.core.task import TaskSpec
from crucible.export.harbor import (
    EngineArtifacts,
    ExportError,
    export_harbor,
    task_folder_name,
)
from tests.fixtures.guess.task import GuessNumber

WHEEL = "crucible-0.1.0-py3-none-any.whl"
FILES = {
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "environment/docker-compose.yaml",
    "environment/mcp-server/Dockerfile",
    f"environment/mcp-server/{WHEEL}",
    "environment/mcp-server/requirements.txt",
    "solution/solve.sh",
    "tests/test.sh",
}


@pytest.fixture
def artifacts(tmp_path: Path) -> EngineArtifacts:
    wheel = tmp_path / "build" / WHEEL
    wheel.parent.mkdir()
    wheel.write_bytes(b"fake wheel")
    requirements = tmp_path / "build" / "requirements.txt"
    requirements.write_text("pydantic==2.14.3\n")
    return EngineArtifacts(wheel=wheel, requirements=requirements)


def _tokens() -> itertools.count[int]:
    return itertools.count()


def _export(
    out: Path,
    artifacts: EngineArtifacts,
    seeds: list[int],
    task_ref: str = "guess@1",
    **kw: object,
) -> list[Path]:
    counter = _tokens()
    return export_harbor(
        task_ref,
        seeds,
        out,
        artifacts,
        token_factory=lambda: f"token-{next(counter):04d}-secret",
        git_commit="abc123",
        **kw,  # pyright: ignore[reportArgumentType]
    )


def _files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def _token(folder: Path) -> str:
    config = tomllib.loads((folder / "task.toml").read_text())
    return config["verifier"]["env"]["GRADE_TOKEN"]


def test_layout(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    folders = _export(tmp_path / "out", artifacts, [0, 1, 7])
    assert [f.name for f in folders] == [task_folder_name("guess@1", s) for s in (0, 1, 7)]
    assert {p.name for p in (tmp_path / "out").iterdir()} == {
        "guess-1-seed-0",
        "guess-1-seed-1",
        "guess-1-seed-7",
        "manifest.json",
        "build-sidecar.sh",
    }
    for folder in folders:
        assert _files(folder) == FILES
        for script in ("solution/solve.sh", "tests/test.sh"):
            assert (folder / script).stat().st_mode & 0o111
        assert "@@" not in "".join(
            (folder / f).read_text() for f in FILES if not f.endswith(".whl")
        )


def test_task_toml_and_compose(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    (folder,) = _export(tmp_path / "out", artifacts, [5])
    config = tomllib.loads((folder / "task.toml").read_text())
    assert config["task"]["name"] == "crucible/guess-1-seed-5"
    assert config["metadata"] == {
        "task_ref": "guess@1",
        "seed": 5,
        "engine_version": "0.1.0",
        "git_commit": "abc123",
        "generator": "crucible export harbor",
    }
    (server,) = config["environment"]["mcp_servers"]
    assert server["url"] == "http://mcp-server:8000/mcp"
    assert config["environment"]["env"] == {}
    assert config["solution"]["env"]["CRUCIBLE_POLICY"] == "reference"
    assert len(config["solution"]["env"]["SOLUTION_TOKEN"]) == 64
    assert config["schema_version"] == "1.4"

    compose = yaml.safe_load((folder / "environment/docker-compose.yaml").read_text())
    assert server["name"] in compose["services"]
    env = compose["services"]["mcp-server"]["environment"]
    assert env["MODE"] == "sidecar"
    assert env["SIDECAR_TASK"] == "guess@1"
    assert env["SIDECAR_SEED"] == "5"
    assert env["GRADE_TOKEN"] == _token(folder)
    assert "environment" not in compose["services"]["main"]


@pytest.mark.parametrize("task_ref", ["guess@1"])
def test_metadata_comes_from_the_spec(
    tmp_path: Path, artifacts: EngineArtifacts, task_ref: str
) -> None:
    spec = get_task(task_ref).spec
    (folder,) = _export(tmp_path / "out", artifacts, [4], task_ref)
    task = tomllib.loads((folder / "task.toml").read_text())["task"]
    assert task["description"] == f"{spec.description} ({spec.ref}, seed 4)"
    assert task["keywords"][:3] == ["crucible", "mcp", "interactive"]
    assert set(spec.keywords) <= set(task["keywords"])
    assert len(set(task["keywords"])) == len(task["keywords"])
    instruction = (folder / "instruction.md").read_text()
    assert instruction.splitlines()[0] == f"# {spec.title}"


def test_guess_export_has_no_incident_wording(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    (folder,) = _export(tmp_path / "out", artifacts, [0], "guess@1")
    for rel in FILES:
        if rel.endswith(".whl"):
            continue
        text = (folder / rel).read_text().lower()
        for word in ("incident", "on-call", "outage", "root cause"):
            assert word not in text, (rel, word)


def test_metadata_is_escaped_for_toml(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    class Quoted(GuessNumber):
        spec = GuessNumber.spec.model_copy(
            update={
                "id": "quoted",
                "title": 'A "quoted" title',
                "description": 'Say "hi"\\ then\nstop.',
                "keywords": ('kw "1"', "mcp"),
            }
        )

    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        register_task(Quoted)
        (folder,) = _export(tmp_path / "out", artifacts, [0], "quoted@1")
    task = tomllib.loads((folder / "task.toml").read_text())["task"]
    assert task["description"] == 'Say "hi"\\ then\nstop. (quoted@1, seed 0)'
    assert task["keywords"] == ["crucible", "mcp", "interactive", 'kw "1"']
    assert (folder / "instruction.md").read_text().startswith('# A "quoted" title\n')


def test_default_resources(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    (folder,) = _export(tmp_path / "out", artifacts, [0])
    config = tomllib.loads((folder / "task.toml").read_text())
    assert config["agent"]["timeout_sec"] == 900.0
    environment = config["environment"]
    assert (environment["cpus"], environment["memory_mb"]) == (1, 2048)
    assert "docker_image" not in environment


def test_task_resources_go_into_task_toml(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    class Big(GuessNumber):
        spec = GuessNumber.spec.model_copy(
            update={
                "id": "big",
                "cpus": 4,
                "memory_mb": 8192,
                "timeout_sec": 1800,
                "docker_image": "ghcr.io/example/agent:1.2",
            }
        )

    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        register_task(Big)
        (folder,) = _export(tmp_path / "out", artifacts, [0], "big@1")
    config = tomllib.loads((folder / "task.toml").read_text())
    assert config["agent"]["timeout_sec"] == 1800.0
    environment = config["environment"]
    assert environment["cpus"] == 4
    assert environment["memory_mb"] == 8192
    assert environment["docker_image"] == "ghcr.io/example/agent:1.2"
    assert environment["build_timeout_sec"] == 900.0


@pytest.mark.parametrize(
    "bad",
    [{"cpus": 0}, {"memory_mb": -1}, {"timeout_sec": 0}, {"docker_image": 'a "b"'}],
)
def test_bad_resources_are_rejected(bad: dict[str, object]) -> None:
    fields = GuessNumber.spec.model_dump()
    with pytest.raises(ValueError):
        TaskSpec.model_validate({**fields, **bad})


def _salt(folder: Path) -> str:
    compose = yaml.safe_load((folder / "environment/docker-compose.yaml").read_text())
    return compose["services"]["mcp-server"]["environment"]["SIDECAR_SALT"]


def test_salt_is_fresh_and_only_in_the_sidecar_env(
    tmp_path: Path, artifacts: EngineArtifacts
) -> None:
    folders = _export(tmp_path / "out", artifacts, [0, 1, 2])
    salts = [_salt(f) for f in folders]
    assert all(len(s) == 32 and int(s, 16) >= 0 for s in salts)
    assert len(set(salts)) == 3
    manifest = (tmp_path / "out" / "manifest.json").read_text()
    for folder, salt in zip(folders, salts, strict=True):
        holders = {f for f in FILES if not f.endswith(".whl") and salt in (folder / f).read_text()}
        assert holders == {"environment/docker-compose.yaml"}
        assert salt not in manifest


def test_tokens_stay_with_sidecar_and_verifier(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    folders = _export(tmp_path / "out", artifacts, [0, 1, 2])
    tokens = [_token(f) for f in folders]
    assert len(set(tokens)) == 3
    manifest = (tmp_path / "out" / "manifest.json").read_text()
    for folder, token in zip(folders, tokens, strict=True):
        holders = {f for f in FILES if not f.endswith(".whl") and token in (folder / f).read_text()}
        assert holders == {"task.toml", "environment/docker-compose.yaml"}
        assert token not in manifest


def test_agent_side_has_no_engine_or_secrets(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    (folder,) = _export(tmp_path / "out", artifacts, [3])
    instruction = (folder / "instruction.md").read_text()
    assert get_task("guess@1").instruction().strip() in instruction
    for leak in ("/grade", "GRADE_TOKEN", "seed", "SIDECAR"):
        assert leak not in instruction
    main = (folder / "environment/Dockerfile").read_text()
    assert "crucible" not in main.lower()
    assert ".whl" not in main


def test_manifest(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    _export(tmp_path / "out", artifacts, [2, 0], solution_policy="stop_early")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["seeds"] == [2, 0]
    assert manifest["solution_policy"] == "stop_early"
    assert manifest["wheel"] == WHEEL
    assert len(manifest["wheel_sha256"]) == 64
    folder = tmp_path / "out" / "guess-1-seed-2"
    config = tomllib.loads((folder / "task.toml").read_text())
    assert config["solution"]["env"]["CRUCIBLE_POLICY"] == "stop_early"


def test_non_empty_out_needs_force(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    out = tmp_path / "out"
    _export(out, artifacts, [0, 1])
    with pytest.raises(ExportError, match="not empty"):
        _export(out, artifacts, [0])
    _export(out, artifacts, [4], force=True)
    assert {p.name for p in out.iterdir()} == {
        "guess-1-seed-4",
        "manifest.json",
        "build-sidecar.sh",
    }


@pytest.mark.parametrize("seeds", [[], [1, 1]])
def test_bad_seeds(tmp_path: Path, artifacts: EngineArtifacts, seeds: list[int]) -> None:
    with pytest.raises(ExportError):
        _export(tmp_path / "out", artifacts, seeds)
    assert not (tmp_path / "out").exists()


def test_cli(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    out = tmp_path / "out"
    args = ["export", "harbor", "--task", "guess@1", "--out", str(out), "--seeds", "0-2"]
    prebuilt = ["--wheel", str(artifacts.wheel), "--requirements", str(artifacts.requirements)]
    result = CliRunner().invoke(app, [*args, *prebuilt])
    assert result.exit_code == 0, result.output
    assert "harbor run -p" in result.output
    assert len(list(out.glob("guess-1-seed-*"))) == 3

    again = CliRunner().invoke(app, [*args, *prebuilt])
    assert again.exit_code == 2
    assert "--force" in again.output

    half = CliRunner().invoke(app, [*args, "--wheel", str(artifacts.wheel), "--force"])
    assert half.exit_code == 2

    unknown = CliRunner().invoke(app, [*args, *prebuilt, "--force", "--solution-policy", "nope"])
    assert unknown.exit_code == 2

    no_task = [a for a in args if a not in ("--task", "guess@1")]
    missing = CliRunner().invoke(app, [*no_task, *prebuilt, "--force"])
    assert missing.exit_code == 2
    assert "Missing option '--task'" in click.unstyle(missing.output)


def test_shared_image_and_oracle_secrets(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    folders = _export(tmp_path / "out", artifacts, [0, 1])
    images: set[str] = set()
    for folder in folders:
        config = tomllib.loads((folder / "task.toml").read_text())
        compose = yaml.safe_load((folder / "environment/docker-compose.yaml").read_text())
        service = compose["services"]["mcp-server"]
        images.add(service["image"])
        assert "build" not in service and "expose" not in service
        token = config["solution"]["env"]["SOLUTION_TOKEN"]
        assert token == service["environment"]["SOLUTION_TOKEN"]
        assert token != _token(folder)
        assert token not in (folder / "instruction.md").read_text()
        assert "uv " not in (folder / "solution/solve.sh").read_text()
    assert len(images) == 1
    build = (tmp_path / "out/build-sidecar.sh").read_text()
    assert build.count("docker build") == 1
    assert next(iter(images)) in build


def test_harbor_lint(tmp_path: Path, artifacts: EngineArtifacts) -> None:
    import shutil
    import subprocess

    harbor = shutil.which("harbor")
    if harbor is None:
        pytest.skip("Harbor CLI is not installed")
    (folder,) = _export(tmp_path / "out", artifacts, [0])
    result = subprocess.run(
        [
            harbor,
            "lint",
            str(folder),
            "--select",
            "invalid-task-config",
            "--select",
            "unknown-config-field",
            "--select",
            "invalid-task-files",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

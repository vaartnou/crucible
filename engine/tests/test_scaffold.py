"""`crucible new-env`: a new environment passes the generic checks with no edits.

The scaffold writes into a copy of the engine, then a separate pytest process (with the copy first
on `PYTHONPATH`) runs `tests/generic` and the new environment's own tests against it. The copy is
not installed, so a small `.dist-info` folder built from the copy's `pyproject.toml` stands in
for the reinstall that `uv run` does after `new-env` adds its entry point.
"""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crucible.cli import app
from crucible.scaffold import ScaffoldError, engine_root, new_env

GENERIC = [
    "test_determinism",
    "test_do_nothing_scores_zero",
    "test_reward_within_0_and_1",
    "test_no_hidden_state_in_tool_results",
    "test_reference_solves_every_seed",
]


@pytest.fixture
def engine_copy(tmp_path: Path) -> Path:
    root = tmp_path / "engine"
    ignore = shutil.ignore_patterns("__pycache__", ".venv", "runs", ".pytest_cache", "*.pyc")
    shutil.copytree(engine_root() / "src", root / "src", ignore=ignore)
    shutil.copytree(engine_root() / "tests", root / "tests", ignore=ignore)
    shutil.copy(engine_root() / "pyproject.toml", root / "pyproject.toml")
    return root


def _entry_points_dir(root: Path) -> Path:
    """A `.dist-info` folder that declares the copy's `crucible.envs` entry points."""
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    group = project["entry-points"]["crucible.envs"]
    info = root / "ep" / "crucible_scaffold_copy-0.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: crucible-scaffold-copy\nVersion: 0.0.0\n"
    )
    lines = [f"{name} = {value}" for name, value in sorted(group.items())]
    (info / "entry_points.txt").write_text("[crucible.envs]\n" + "\n".join(lines) + "\n")
    return info.parent


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    path = os.pathsep.join(str(p) for p in (root / "src", root, _entry_points_dir(root)))
    env = {**os.environ, "PYTHONPATH": path}
    env.pop("CRUCIBLE_UPDATE_GOLDEN", None)
    return subprocess.run(
        [*args], cwd=root, env=env, capture_output=True, text=True, check=False, timeout=600
    )


def test_new_env_passes_the_generic_checks_unchanged(engine_copy: Path) -> None:
    result = CliRunner().invoke(app, ["new-env", "demo_env", "--root", str(engine_copy)])
    assert result.exit_code == 0, result.output
    assert "Registered demo_env@1" in result.output

    where = _run(engine_copy, sys.executable, "-c", "import crucible; print(crucible.__file__)")
    assert Path(where.stdout.strip()).is_relative_to(engine_copy), where.stdout + where.stderr

    tests = _run(
        engine_copy,
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "-p",
        "no:cacheprovider",
        # The outer suite already checks shipped environments. This subprocess checks
        # the newly scaffolded environment plus the static policy-import audit.
        "-k",
        "demo_env or test_no_hidden_imports",
        "tests/generic",
        "src/crucible/envs/demo_env",
        "tests/policies/test_no_hidden_imports.py",
    )
    assert tests.returncode == 0, tests.stdout[-4000:] + tests.stderr[-2000:]
    for check in GENERIC:
        assert f"{check}[demo_env@1] PASSED" in tests.stdout, check
    assert "envs/demo_env/tests/test_task.py::test_registered PASSED" in tests.stdout

    bin_dir = Path(sys.executable).parent
    new_files = ["src/crucible/envs/demo_env"]
    lint = _run(engine_copy, str(bin_dir / "ruff"), "check", *new_files)
    assert lint.returncode == 0, lint.stdout
    fmt = _run(engine_copy, str(bin_dir / "ruff"), "format", "--check", *new_files)
    assert fmt.returncode == 0, fmt.stdout


def test_new_env_files(engine_copy: Path) -> None:
    written = new_env("triage", engine_copy)
    rel = {p.relative_to(engine_copy).as_posix() for p in written}
    env = "src/crucible/envs/triage"
    assert rel == {
        f"{env}/__init__.py",
        f"{env}/task.py",
        f"{env}/policies/__init__.py",
        f"{env}/policies/reference.py",
        f"{env}/tests/__init__.py",
        f"{env}/tests/test_task.py",
        f"{env}/README.md",
        "pyproject.toml",
    }
    task = (engine_copy / env / "task.py").read_text()
    assert 'id="triage"' in task and "class TriageTask(Task):" in task
    assert 'title="Triage"' in task
    policy = (engine_copy / env / "policies/reference.py").read_text()
    assert '@register_policy("reference", "triage")' in policy
    group = tomllib.loads((engine_copy / "pyproject.toml").read_text())["project"]["entry-points"]
    assert group["crucible.envs"]["triage"] == "crucible.envs.triage"
    assert group["crucible.envs"]["oncall"] == "crucible.envs.oncall"


@pytest.mark.parametrize("name", ["Bad", "1abc", "has-dash", "class", "", "a" * 25])
def test_bad_names(engine_copy: Path, name: str) -> None:
    with pytest.raises(ScaffoldError):
        new_env(name, engine_copy)


def test_taken_names(engine_copy: Path) -> None:
    with pytest.raises(ScaffoldError, match="already registered"):
        new_env("oncall", engine_copy)
    new_env("fresh", engine_copy)
    with pytest.raises(ScaffoldError, match="already exists"):
        new_env("fresh", engine_copy)


def test_cli_errors_exit_2(engine_copy: Path) -> None:
    result = CliRunner().invoke(app, ["new-env", "Bad", "--root", str(engine_copy)])
    assert result.exit_code == 2
    missing = CliRunner().invoke(app, ["new-env", "ok", "--root", str(engine_copy / "nope")])
    assert missing.exit_code == 2

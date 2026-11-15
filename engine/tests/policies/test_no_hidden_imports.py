"""Policies must not import the hidden parts of a task.

A policy sees tool results only. Generic policies (`crucible.policies`) import nothing from
`crucible.envs`; an environment's policies import from that environment's `policies` package
only, never its task, world, simulator, state or grader.
"""

import ast
from pathlib import Path

import pytest

import crucible.envs
import crucible.policies

ENVS_ROOT = Path(crucible.envs.__file__).parent
ENV_NAMES = sorted(p.name for p in ENVS_ROOT.iterdir() if (p / "__init__.py").is_file())


def _imported(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _allowed(name: str, env: str | None) -> bool:
    if not (name == "crucible" or name.startswith("crucible.")):
        return True
    if name.startswith(("crucible.core.", "crucible.policies")):
        return True
    own = f"crucible.envs.{env}.policies"
    return env is not None and (name == own or name.startswith(own + "."))


def _check(root: Path, env: str | None) -> None:
    files = sorted(root.rglob("*.py"))
    assert files, root
    for path in files:
        for name in _imported(ast.parse(path.read_text())):
            assert _allowed(name, env), (path, name)


def test_generic_policies_import_no_env() -> None:
    _check(Path(crucible.policies.__file__).parent, None)


def test_env_names_found() -> None:
    assert "oncall" in ENV_NAMES


@pytest.mark.parametrize("env", ENV_NAMES)
def test_env_policies_import_only_own_policies(env: str) -> None:
    _check(ENVS_ROOT / env / "policies", env)


def test_check_catches_a_hidden_import() -> None:
    assert not _allowed("crucible.envs.incident.world", "incident")
    assert not _allowed("crucible.envs.incident.task", "incident")
    assert not _allowed("crucible.envs.other.policies.reference", "incident")
    assert not _allowed("crucible.envs.incident.policies", None)
    assert _allowed("crucible.envs.incident.policies.views", "incident")

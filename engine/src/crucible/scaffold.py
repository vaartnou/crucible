"""`crucible new-env`: start a new environment as a copy of `guess@1`.

The copy is a working environment from the first minute: a package under `envs/<name>/` with the
task, a reference policy, tests and a `README.md` card, plus a `crucible.envs` entry point in
`pyproject.toml`. The next `uv run` reinstalls the project, so the registry finds the new
environment, and the generic checks in `tests/generic/` pick it up with no edits.
"""

import keyword
import re
from dataclasses import dataclass
from pathlib import Path

from crucible.core.registry import ENTRY_POINT_GROUP, list_tasks

__all__ = ["ScaffoldError", "engine_root", "new_env"]

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")  # short enough that copied lines fit 100
_SOURCE = "guess"
_GROUP_HEADER = f'[project.entry-points."{ENTRY_POINT_GROUP}"]'


class ScaffoldError(Exception):
    """The environment cannot be created (bad name, name taken, no source checkout)."""


@dataclass(frozen=True)
class _Replace:
    old: str
    new: str


def engine_root() -> Path:
    """The engine folder of a source checkout (holds `pyproject.toml`, `src/` and `tests/`)."""
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file() or not (root / "tests").is_dir():
        raise ScaffoldError(
            "new-env needs a source checkout of the engine; pass --root <engine folder>"
        )
    return root


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part) + "Task"


def _title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("_") if part)


def _copy(text: str, edits: list[_Replace], source: Path) -> str:
    """Apply each edit exactly once. Fail loudly if the source no longer has the anchor."""
    for edit in edits:
        if text.count(edit.old) != 1:
            raise ScaffoldError(f"{source} changed: cannot find {edit.old!r} exactly once")
        text = text.replace(edit.old, edit.new)
    return text


def _check_name(name: str, root: Path) -> None:
    if not _NAME.match(name) or keyword.iskeyword(name):
        raise ScaffoldError(
            f"bad name {name!r}: use lowercase letters, digits and '_', starting with a letter"
        )
    if name in {ref.split("@")[0] for ref in list_tasks()}:
        raise ScaffoldError(f"a task named {name!r} is already registered")
    folder = root / "src" / "crucible" / "envs" / name
    if folder.exists():
        raise ScaffoldError(f"{folder} already exists")


def _add_entry_point(pyproject: Path, name: str) -> None:
    """Add `name = "crucible.envs.<name>"` under the `crucible.envs` entry point table."""
    text = pyproject.read_text()
    if text.count(_GROUP_HEADER) != 1:
        raise ScaffoldError(f"{pyproject}: cannot find {_GROUP_HEADER} exactly once")
    line = f'{name} = "crucible.envs.{name}"'
    if re.search(rf"^{re.escape(name)}\s*=", text, flags=re.M):
        raise ScaffoldError(f"{pyproject} already has an entry point named {name!r}")
    head, tail = text.split(_GROUP_HEADER + "\n", 1)
    table, sep, rest = tail.partition("\n\n")
    pyproject.write_text(f"{head}{_GROUP_HEADER}\n{table}\n{line}{sep}{rest}")


def _test_module(name: str, cls: str) -> str:
    return f'''"""Task-specific tests for `{name}@1`.

The generic checks in `tests/generic/` run on it too.
"""

import asyncio
from pathlib import Path

from crucible.core.registry import get_policy, get_task, list_tasks
from crucible.envs.{name}.task import {cls}
from crucible.testing import play


def test_registered() -> None:
    assert "{name}@1" in list_tasks()
    assert isinstance(get_task("{name}@1"), {cls})


def test_reference_scores_above_zero(tmp_path: Path) -> None:
    policy = get_policy("reference", "{name}@1")
    result = asyncio.run(play(policy, 0, tmp_path / "t.jsonl", "{name}@1"))
    assert result.score.total > 0
'''


def _card(name: str) -> str:
    return f"""# {name}@1: {_title(name)}

Copied from `guess@1` by `crucible new-env`. Replace this card as the environment grows.

| | |
|---|---|
| Ref | `{name}@1` |
| Entry point | `crucible.envs` → `{name} = "crucible.envs.{name}"` |

## Tools

## Scoring

## Policies

## Layout

- `task.py`: the task. `policies/reference.py`: the reference policy.
- `tests/`: task-specific tests. The generic checks in `engine/tests/generic/` also run on it.
"""


def new_env(name: str, root: Path) -> list[Path]:
    """Copy `guess@1` into a new environment `name@1` under `root`; return the files written."""
    _check_name(name, root)
    envs = root / "src" / "crucible" / "envs"
    pyproject = root / "pyproject.toml"
    cls = _class_name(name)
    guess_task = root / "tests" / "fixtures" / _SOURCE / "task.py"
    guess_policy = root / "tests" / "fixtures" / _SOURCE / "policies" / "reference.py"
    for path in (guess_task, guess_policy, pyproject):
        if not path.is_file():
            raise ScaffoldError(f"{path} not found; is {root} the engine folder?")

    task_text = _copy(
        guess_task.read_text(),
        [
            _Replace(
                '"""`guess@1`: find a hidden number in 1..100 with higher/lower feedback."""',
                f'"""`{name}@1`, copied from `guess@1` by `crucible new-env`.\n\n'
                "Replace the game and keep the `Task` interface.\n"
                '"""',
            ),
            _Replace("class GuessNumber(Task):", f"class {cls}(Task):"),
            _Replace('id="guess",', f'id="{name}",'),
            _Replace('title="Guess the number",', f'title="{_title(name)}",'),
        ],
        guess_task,
    )
    policy_text = _copy(
        guess_policy.read_text(),
        [
            _Replace(
                '"""Binary-search reference for `guess`. '
                'Uses only tool results, never task internals."""',
                f'"""Reference for `{name}` (copied from `guess`).\n\n'
                "Uses only tool results, never task internals.\n"
                '"""',
            ),
            _Replace(
                '@register_policy("reference", "guess")', f'@register_policy("reference", "{name}")'
            ),
        ],
        guess_policy,
    )
    pkg = envs / name
    files = {
        pkg / "__init__.py": (
            f'"""`{name}@1`. Importing this package registers the task and its policies."""\n\n'
            f"from crucible.envs.{name} import policies\n"
            f"from crucible.envs.{name}.task import {cls}\n\n"
            f'__all__ = ["{cls}", "policies"]\n'
        ),
        pkg / "task.py": task_text,
        pkg / "policies" / "__init__.py": (
            f'"""Policies for `{name}`. Importing a module registers its policies."""\n\n'
            f"from crucible.envs.{name}.policies import reference\n\n"
            '__all__ = ["reference"]\n'
        ),
        pkg / "policies" / "reference.py": policy_text,
        pkg / "tests" / "__init__.py": "",
        pkg / "tests" / "test_task.py": _test_module(name, cls),
        pkg / "README.md": _card(name),
    }
    _add_entry_point(pyproject, name)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return [*files, pyproject]

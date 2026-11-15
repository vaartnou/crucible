"""Registries for tasks and policies, filled at import time.

Environments are found through the `crucible.envs` entry point group: each entry point names a
module whose import registers that environment's tasks and policies. The built-in environments
are declared the same way in `pyproject.toml`, so a plugin package adds an environment with

    [project.entry-points."crucible.envs"]
    myenv = "my_package.myenv"

Lookup errors are `KeyError` subclasses (`UnknownTaskError`, `UnknownPolicyError`). Malformed
refs raise `ValueError`. An entry point that fails to import raises `EnvLoadError`.
"""

import importlib
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from importlib.metadata import EntryPoint, entry_points

from crucible.core.policy import Policy
from crucible.core.task import Task

type PolicyFactory = Callable[[], Policy]

_tasks: dict[str, type[Task]] = {}
_policies: dict[tuple[str, str | None], PolicyFactory] = {}
_loaded = False

ENTRY_POINT_GROUP = "crucible.envs"


class UnknownTaskError(KeyError):
    """No task is registered under the requested id or ref."""

    def __str__(self) -> str:
        return str(self.args[0])


class UnknownPolicyError(KeyError):
    """No policy is registered under the requested name."""

    def __str__(self) -> str:
        return str(self.args[0])


class EnvLoadError(ImportError):
    """An environment named by a `crucible.envs` entry point could not be loaded."""


def _env_entry_points() -> list[EntryPoint]:
    """Entry points in the `crucible.envs` group, sorted by name so load order is stable."""
    available = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name)
    configured = os.environ.get("CRUCIBLE_ENVIRONMENTS")
    if configured is None:
        return available
    selected = {name.strip() for name in configured.split(",") if name.strip()}
    unknown = selected - {ep.name for ep in available}
    if not selected or unknown:
        raise EnvLoadError(
            "CRUCIBLE_ENVIRONMENTS must name installed environment entry points; "
            f"available: {', '.join(ep.name for ep in available)}"
        )
    return [ep for ep in available if ep.name in selected]


def _load_builtins() -> None:
    """Import the generic policies, then every environment entry point. Runs once.

    The flag is set first so a plugin that calls back into the registry while importing does not
    start a second load; if a plugin fails, the flag is cleared and the next lookup retries.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        importlib.import_module("crucible.policies")
        for ep in _env_entry_points():
            try:
                ep.load()
            except Exception as exc:
                raise EnvLoadError(
                    f"Could not load environment {ep.name!r} ({ep.value}) from entry point "
                    f"group {ENTRY_POINT_GROUP!r}: {exc}"
                ) from exc
    except BaseException:
        _loaded = False
        raise


def list_envs() -> list[tuple[str, str]]:
    """`(name, module)` for every environment entry point, sorted by name."""
    return [(ep.name, ep.value) for ep in _env_entry_points()]


@contextmanager
def _reset_for_tests() -> Generator[None]:
    """Run with empty registries and no built-in loading; restore state on exit."""
    global _loaded
    saved = (dict(_tasks), dict(_policies), _loaded)
    _tasks.clear()
    _policies.clear()
    _loaded = True
    try:
        yield
    finally:
        _tasks.clear()
        _tasks.update(saved[0])
        _policies.clear()
        _policies.update(saved[1])
        _loaded = saved[2]


def _split_ref(ref: str) -> tuple[str, int | None]:
    """Split `"id"` or `"id@version"`. Raise `ValueError` if malformed."""
    task_id, sep, version = ref.partition("@")
    if not task_id or (sep and (not version.isascii() or not version.isdecimal())):
        raise ValueError(f"Malformed task ref {ref!r}: expected 'id' or 'id@<integer version>'")
    return task_id, int(version) if sep else None


def register_task[T: type[Task]](cls: T) -> T:
    """Class decorator. Registers a task under `cls.spec.ref`."""
    ref = cls.spec.ref
    if ref in _tasks:
        raise ValueError(f"Task {ref!r} is already registered ({_tasks[ref].__name__})")
    _tasks[ref] = cls
    return cls


def list_tasks() -> list[str]:
    """Sorted refs of all registered tasks (by id, then numeric version)."""
    _load_builtins()
    return sorted(_tasks, key=lambda r: (_tasks[r].spec.id, _tasks[r].spec.version))


def get_task(ref: str) -> Task:
    """New instance for `"id@version"`, or the newest version for a bare `"id"`."""
    task_id, version = _split_ref(ref)
    _load_builtins()
    if version is not None:
        cls = _tasks.get(f"{task_id}@{version}")
    else:
        versions = [c for c in _tasks.values() if c.spec.id == task_id]
        cls = max(versions, key=lambda c: c.spec.version, default=None)
    if cls is None:
        known = ", ".join(list_tasks()) or "none"
        raise UnknownTaskError(f"Unknown task {ref!r}. Known tasks: {known}")
    return cls()


def register_policy[F: Callable[[], Policy]](
    name: str, task_id: str | None = None
) -> Callable[[F], F]:
    """Decorator for a policy class or factory. `task_id=None` makes it task-agnostic."""

    def decorator(factory: F) -> F:
        key = (name, task_id)
        if key in _policies:
            raise ValueError(f"Policy {name!r} for task {task_id!r} is already registered")
        _policies[key] = factory
        return factory

    return decorator


def get_policy(name: str, task_ref: str) -> Policy:
    """Build a policy: `(name, task_id)` first, then the task-agnostic `(name, None)`."""
    task_id, _ = _split_ref(task_ref)
    _load_builtins()
    factory = _policies.get((name, task_id)) or _policies.get((name, None))
    if factory is None:
        options = sorted(
            f"{n} (task: {t})" if t else f"{n} (any task)"
            for (n, t) in _policies
            if t is None or t == task_id
        )
        raise UnknownPolicyError(
            f"Unknown policy {name!r} for task {task_ref!r}. "
            f"Available: {', '.join(options) or 'none'}"
        )
    return factory()


def list_policies(task_ref: str) -> list[str]:
    """Sorted names `get_policy` accepts for this task (task-specific and task-agnostic)."""
    task_id, _ = _split_ref(task_ref)
    _load_builtins()
    return sorted({n for (n, t) in _policies if t is None or t == task_id})


__all__ = [
    "ENTRY_POINT_GROUP",
    "EnvLoadError",
    "PolicyFactory",
    "UnknownPolicyError",
    "UnknownTaskError",
    "get_policy",
    "get_task",
    "list_envs",
    "list_policies",
    "list_tasks",
    "register_policy",
    "register_task",
]

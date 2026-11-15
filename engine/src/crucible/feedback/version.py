"""Identify simulator source independently of uncommitted Git state."""

import hashlib
import inspect
from pathlib import Path

from crucible.core.task import Task


def simulator_hash(task: Task) -> str:
    """Legacy full environment package hash, including baseline policy code."""
    return _source_hash(task, exclude_policies=False)


def environment_hash(task: Task) -> str:
    """World, dynamics, tools and grader identity, independent of baseline policy changes."""
    return _source_hash(task, exclude_policies=True)


def _source_hash(task: Task, *, exclude_policies: bool) -> str:
    source = inspect.getsourcefile(type(task))
    if source is None:
        raise ValueError("Task has no inspectable simulator source")
    root = Path(source).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if (
            path.suffix not in (".py", ".yaml")
            or "tests" in parts
            or (exclude_policies and "policies" in parts)
        ):
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()

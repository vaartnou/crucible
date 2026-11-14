"""Registry integration for Last Siding."""

from crucible.core.registry import get_task, list_tasks
from crucible.envs.last_siding.task import LastSidingTask


def test_registered() -> None:
    assert "last_siding@1" in list_tasks()
    assert isinstance(get_task("last_siding@1"), LastSidingTask)

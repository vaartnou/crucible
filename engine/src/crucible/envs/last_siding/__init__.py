"""`last_siding@1`. Importing this package registers the task and its policies."""

from crucible.envs.last_siding import policies
from crucible.envs.last_siding.task import LastSidingTask

__all__ = ["LastSidingTask", "policies"]

"""A deterministic on-call evaluation environment."""

from crucible.envs.oncall import policies
from crucible.envs.oncall.task import OncallTask

__all__ = ["OncallTask", "policies"]

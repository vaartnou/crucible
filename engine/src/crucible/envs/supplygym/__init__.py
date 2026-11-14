"""`supplygym@1` and `supplygym-fresh@1`: weekly inbound-supply control under hidden disruptions.

Importing this package registers the task and its policies.
"""

from crucible.envs.supplygym import policies
from crucible.envs.supplygym.cala_task import SupplyGymCalaTask
from crucible.envs.supplygym.task import SupplyGymFreshTask, SupplyGymTask

__all__ = ["SupplyGymCalaTask", "SupplyGymFreshTask", "SupplyGymTask", "policies"]

"""`guess@1`: throwaway number-guessing task used to exercise the core loop.

Importing this package registers the task and its policies.
"""

from tests.fixtures.guess import policies
from tests.fixtures.guess.task import GuessNumber

__all__ = ["GuessNumber", "policies"]

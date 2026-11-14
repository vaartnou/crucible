"""Policies for `supplygym`. Importing a module registers its policies.

Policies here see tool results only: they never import the task's `task`, `world`, `sim`, `state`,
`play` or `grader` modules (an `ast` test enforces it). `views` parses tool results; `reference`
holds the deciders (the PPO reference and the default rule).
"""

from crucible.envs.supplygym.policies import baselines, reference, views

__all__ = ["baselines", "reference", "views"]

# Concepts

An **environment** contains a task, its public-tool policies, a grader and tests. `oncall@1` is the shipped task. A **task ref** combines the task ID and version. Environments register through the `crucible.envs` entry-point group.

A **world** is deterministic for a seed and optional private salt. A **rollout** resets that world, passes observations to a policy, executes tool calls, and grades the resulting episode. Tools have typed JSON arguments and read/action/clock/end kinds. Policies see only public observations; the grader can inspect private state.

The **step budget** limits calls, including invalid calls. The simulator clock advances on tool calls rather than wall time. A **transcript** records the task, seed, calls, results and final score. Salted headers indicate that a salt was used without exposing it.

A **reference** policy must solve from public signals. **Baselines** include doing nothing and cheap exploit strategies. An **oracle bound** can use private information in tests to anchor the score; Harbor's oracle instead plays the public reference policy.

The **score** has a total reward in [0, 1], numeric components and boolean flags. Oncall measures impact, verified recovery, diagnosis and action penalties. A successful-looking action is insufficient if the fault recurs during verification.

A **Harbor export** packages one task per seed with a simulator sidecar. The agent accesses MCP at `/mcp`. The private verifier uses `/grade` with a grade token. The agent container does not contain the simulator wheel. Generated exports contain secrets and must remain untracked.

A **job** is either a Crucible evaluation specification or a Harbor batch. In-process evaluation is the fast feedback loop; Harbor runs provide the reported model comparison. Do not substitute bot results for measured model results.

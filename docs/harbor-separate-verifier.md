# Separate Harbor verification

New exports use Harbor 0.26.0's documented separate-verifier workflow. The agent
plays the existing simulation through MCP; the simulator and reward formula are unchanged.

## Evidence and grading

1. Harbor stops the agent container after completion, early exit, or timeout.
2. A `[[verifier.collect]]` hook runs `crucible.export.collect_episode` inside
   `mcp-server`. It calls the authenticated local `/grade` endpoint, finalizes the
   episode, and atomically writes `/tmp/verification/episode.json`.
3. Harbor collects that file directly from the sidecar and transfers it into a fresh
   verifier container built from `tests/Dockerfile`. The verifier image contains the
   same frozen engine wheel and locked requirements, installed at build time.
4. `crucible.export.verify_episode` checks task and seed identity, the scoring hash,
   transcript ordering, step limits, environment errors, exact tool results, and
   final outcome while replaying the calls. The salt comes from verifier configuration.
5. The independently computed reward is written to `/logs/verifier/reward.txt`.
   `grade.json` and `transcript.json` preserve diagnostics and replayable actions.

The verifier does not trust the numeric reward in the collected response or any file
written inside the agent container. It requires neither the running simulator nor a
model API credential during verification. Missing or malformed evidence and replay
mismatches produce `grade_error.txt` and fail verification without a successful reward.
Agent timeouts remain recorded as agent exceptions even when the captured episode can
be graded successfully. Do not count infrastructure failures as model-performance zeros.

This follows Harbor's supported grading architecture; it does not claim organizer
approval or complete protection against every attack. Published demonstration tokens
and salts remain readable from the repository. Use a fresh private export for held-out
evaluation. Base-image tags and package download services are not fully immutable.

## Run and regrade

```sh
uv run --directory engine crucible export harbor --task oncall@2 --seeds 0 \
  --out ../runs/oncall-separate
sh runs/oncall-separate/build-sidecar.sh
harbor lint runs/oncall-separate/oncall-2-seed-0
harbor run -p runs/oncall-separate -a oracle -n 1 -o runs/harbor-jobs
```

Run a model with the command in the root README. Once a trial has collected its
sidecar artifact, native Harbor regrading starts only the independent verifier:

```sh
harbor trials regrade runs/harbor-jobs/JOB/TRIAL \
  -p runs/oncall-separate/oncall-2-seed-0 -o runs/regrades
```

Keep the original export, including its salt, for that trial. A different salted
world will fail replay validation. The current verifier requires exact replay on
the same Linux runtime; the separate cross-platform comparison tool retains its
explicit numeric tolerance.

## Validation, 15 November 2026

- Full default Harbor lint passes without an ignored `shared-verifier` rule.
- 96 affected export, MCP, grade, lifecycle, launcher, and overlap tests pass.
- Oracle reward is `0.8261707424624951`, exactly matching the original shared grader
  on the same private world. Nop reward is zero. Neither trial has an exception.
- Native Harbor oracle regrade reproduces `0.8261707424624951` without an agent run.
- Forged agent-side evidence, a fake reward file, and a poisoned agent Python
  launcher do not affect the independently recomputed oracle score.
- A forced agent timeout preserves its unfinished episode and independently scores
  zero, with `AgentTimeoutError` retained in the result.
- A missing transcript footer causes an explicit failed native regrade, a diagnostic
  file, and no reward file.
- A fresh Gemini 3.8 Flash/OpenCode 1.19.5 trial scores `0.644313711357698`, with
  verified recovery, complete diagnosis, zero penalties, 60 calls, and no exceptions.

References: [Harbor separate verifier](https://docs.harborframework.com/tasks/separate-verifier)
and [artifact collection](https://docs.harborframework.com/tasks/artifacts).

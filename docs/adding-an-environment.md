# Adding an environment

## When an environment fits

Crucible fits a task when all of these hold:

- The agent acts through a small set of typed tools, not a free shell.
- A program can simulate the world, and the seed fixes it.
- A program can grade the result from the hidden world and the episode record, with no human or model judge.
- Doing nothing is a real choice with a known cost, so it can anchor 0.
- There is a best possible play (an oracle) or a strong fixed anchor for the top of the scale.

If the task needs a real shell, real files or a model as judge, use a plain Harbor task instead.

## Start with `new-env`

```bash
uv run --directory engine crucible new-env triage
```

This copies the test fixture `guess@1` into a working environment:

```text
engine/src/crucible/envs/triage/
  __init__.py            # imports task and policies (that registers them)
  task.py                # TriageTask, a copy of the guess game
  policies/reference.py  # the reference policy
  tests/test_task.py     # task-specific tests
  README.md              # the environment card, to fill in
```

It also adds `triage = "crucible.envs.triage"` to the `crucible.envs` entry points in `engine/pyproject.toml`. The next `uv run` reinstalls the project, so `crucible tasks` lists `triage@1`. The copy passes every generic check as it is:

```bash
uv run --directory engine pytest tests/generic src/crucible/envs/triage
```

Names are lowercase letters, digits and `_`, start with a letter and have at most 24 characters.

## The contract

A task subclasses `crucible.core.task.Task` and sets `spec: ClassVar[TaskSpec]`.

| Method | Must do |
|---|---|
| `reset(seed, difficulty=0, *, salt=None)` | Build the hidden world from `RngStreams(seed, salt)`. Return the first `Observation`. |
| `tools()` | Return the `ToolSpec` list. Argument models give the JSON Schema. |
| `step(call)` | Validate with `validate_call`; a bad call returns `ok=False` and changes nothing. Return a `ToolResult`. |
| `done()` | True when the episode is over. |
| `grade()` | Return a `Score` (total, breakdown, flags). Must not change state. |
| `instruction()` | The text the agent gets. Explain tools, time and scoring. |

Optional:

| Member | Use |
|---|---|
| `hidden_strings()` | Words from the hidden state that no successful result may contain. The leak test uses it. |
| `solved(score)` | When a rollout counts as solved (default: `score.total > 0`). |
| `checks: ClassVar[TaskChecks]` | Seeds for the generic checks, exploit policy names, and switches for checks that cannot apply. |
| `scoring_hash` | Hash of the scoring config, written into every transcript. |

`TaskSpec` holds `id`, `version`, `max_steps`, `title`, `description`, `keywords`, and optional Harbor resources `cpus`, `memory_mb`, `timeout_sec`, `docker_image`.

### Grade guarantees

The generic checks and the golden scores hold every environment to these rules:

1. **Do nothing scores exactly 0.** `do_nothing` stops at once; its reward must be 0.0.
2. **The reward is in 0–1.** Clamp the raw value. The checks also run a seeded fuzzer.
3. **The reference scores below 1.** 1 belongs to the oracle. A reference at 1 leaves no room to see a better agent. (`guess@1` breaks this on seeds where the first guess is right; it is only a demo.)
4. **Secondary parts multiply.** A bonus for diagnosis or style scales the main outcome. It never adds score on its own, so a policy cannot earn points without doing the job. See [scoring.md](scoring.md).
5. **The same input gives the same output.** Same seed, salt and calls: same transcript and score.

### Never leak hidden state

- Build every tool result from public values only. In `oncall@1`, results come from view models in `state.py`, never from `World`.
- Error messages must not name hidden values. `rollback` of an unknown id lists only the visible deploy ids.
- Policies import only `crucible.core`, `crucible.policies` and their own `policies/` package. `tests/policies/test_no_hidden_imports.py` checks this with `ast`.
- Test scripts that need the truth (calibration solvers) live in `tests/`, which the wheel leaves out.

## Steps

Each step uses `guess@1` as the worked example. Look at [envs/oncall](../engine/src/crucible/envs/oncall/) for a full one.

1. **World (with salt).** Draw everything random in `reset` from `RngStreams(seed, salt).exogenous`. Never use `hash()` or the global `random`.
   *guess:* `self._hidden = _Hidden(target=RngStreams(seed, salt).exogenous.randint(1, 100))`.

2. **Simulation.** Write the rules as plain functions of the world and the actions. Pure functions make the oracle and do-nothing easy to replay.
   *guess:* a guess is `higher`, `lower` or `correct`.

3. **Tools.** One `ToolSpec` per action, with a pydantic argument model. Use `Field` bounds and `Literal` types so the schema refuses bad values. Use `extra="forbid"` to refuse unknown arguments.
   *guess:* `guess(n)` with `n: int = Field(ge=1, le=100)`, and `give_up()`.

4. **Instruction.** Say what the job is, what each tool does and costs, the budget, and how scoring works. Do not name this seed's answer.
   *guess:* "Guess the hidden whole number from 1 to 100 with the `guess` tool. Fewer guesses score higher."

5. **Grader.** Return `Score(total, breakdown, flags)`. Put raw parts in `breakdown` and booleans in `flags`. Keep weights in a YAML file and set `scoring_hash` if they may change.
   *guess:* `total = 1 - (guesses - 1) / 10` if solved, else 0.

6. **Reference.** A policy in `policies/reference.py`, registered with `@register_policy("reference", "<id>")`. It reads only `obs.last_result`.
   *guess:* binary search; at most 7 guesses.

7. **Baselines and exploits.** Write policies that try cheap tricks: guess early, try every action, stop at once. Register them and list them in `checks = TaskChecks(exploit_policies=(...))`. Each must score below the reference on every seed.
   *oncall:* `shotgun`, `restart_all` and `rollback_all`.

8. **Register.** `new-env` already wrote the entry point and the package `__init__.py` imports. A plugin package outside this repo declares its own:

   ```toml
   [project.entry-points."crucible.envs"]
   myenv = "my_package.myenv"
   ```

9. **Checks.** Run the generic checks and your own tests, then pin the golden scores:

   ```bash
   uv run --directory engine pytest tests/generic src/crucible/envs/triage
   ```

   ```bash
   CRUCIBLE_UPDATE_GOLDEN=1 uv run --directory engine pytest tests/policies/test_golden.py
   ```

   After that, any score change without a version bump fails CI.

10. **Export.**

    ```bash
    uv run --directory engine crucible export harbor --task triage@1 --seeds 0-9 --out ../harbor/triage-1
    ```

11. **Harbor runs.** Run `oracle` and `nop` first. The oracle must match your reference closely, and `nop` must score 0. Then run a model agent. See [using-environments.md](using-environments.md#run-in-harbor). Add the scores to [harbor-runs.md](harbor-runs.md).

12. **Environment card.** Fill in `README.md` in the environment folder: the job, tools, why it is hard, scoring, results, exploits, limits, seeds and versions. Use [envs/oncall/README.md](../engine/src/crucible/envs/oncall/README.md) as the model.

## The generic checks

`engine/tests/generic/test_generic.py` runs on every registered task, over `checks.seeds` (seeds 0–19 by default):

| Check | Rule |
|---|---|
| `test_determinism` | same seed and calls → same transcript and score |
| `test_do_nothing_scores_zero` | `do_nothing` scores exactly 0 |
| `test_reward_within_0_and_1` | reference, do-nothing, exploits and a fuzzer all score in 0–1 |
| `test_no_hidden_state_in_tool_results` | no word from `hidden_strings()` in a successful result, unless the call said it |
| `test_reference_solves_every_seed` | `solved(score)` is true for the reference on every seed |
| `test_exploits_score_below_reference` | each exploit policy scores below the reference on every seed |
| `test_oracle_scores_one` | a task's `oracle` policy, if it has one, scores exactly 1 |

Turn a check off in `checks` only when it cannot apply to your task.

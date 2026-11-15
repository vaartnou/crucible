# Cala live demo and upper-bound evaluation

Cala simulates a year of buying one electronics product. The buyer chooses
weekly purchasing controls and sees public inventory, pipeline and supplier
signals. The simulator places orders and advances seven days per decision.

`supplygym-cala@3` computes a deterministic theoretical surplus bound for the
exact seed and scenario salt before the first decision. Harbor reports
`clamp(operating_surplus / surplus_upper_bound, 0, 1)` for a completed year
with a positive bound. Unfinished years and nonpositive bounds score zero.
The bound relaxes shipment constraints, so a strategy using the weekly controls
may top out below 1.0. For a suite, take the mean of the scheduled seed scores.
Resolve infrastructure failures before publishing a comparison.

Operating surplus in USD is sales revenue less purchases, holding, penalties
for unmet demand, expediting and switching. Terminal stock has zero value for
this calculation. The simulator accounts for every week from the unrounded
ledger. The verified annual score, surplus and share of demand served are in
Harbor's verifier result.

## Run the demo

Run from the worktree root in Bash or WSL with Docker Desktop running, Harbor
installed, and Python dependencies available through `uv`. Pick a new output
directory. This example requests three seeds starting at 62000, OpenCode with
Gemini 4 Hydrogen, and two concurrent trials.

```bash
uv sync --directory engine --locked
uv run --directory engine python scripts/run_cala_demo.py \
  --out ../runs/cala-demo \
  --start-seed 62000 --count 3 \
  --model google/gemini-4-hydrogen \
  --key-env GOOGLE_GENERATIVE_AI_API_KEY --prompt-key \
  --concurrency 2
```

The key prompt accepts the key without displaying it. If the selected key
variable is already exported on the host, omit `--prompt-key`. Use a
`provider/model` supported by OpenCode and the API-key variable for that
provider. The runner creates one evaluation export, reuses its seed and salt
for the deterministic strategies, and starts the live model through Harbor.
Use concurrency 1 for a stage run focused on a single seed.

The runner writes `surplus-bounds.json` before any strategy plays. The simulator
independently recomputes each bound on reset, so a standalone Harbor export needs
only the task files to run and score. After Harbor finishes, `report.json`
contains the verified results and, when all scheduled results are available, the
mean score and mean operating surplus for each strategy.

For a standard Harbor evaluation package without host-mounted spectator files,
export the same task version directly.

```bash
uv run --directory engine crucible export harbor \
  --task supplygym-cala@3 --seeds 62000-62002 --out ../harbor/cala-v3
```

## Viewer contract

The trusted demo host reads append-only JSONL from the viewer output directory.
The Docker sidecar writes and flushes the live file while Harbor is running.
The four deterministic files are computed before the live run. Share only
the viewer's public data with the presentation layer. The Harbor export,
verifier credentials and scenario salts belong to the evaluator.

`spectator-manifest.json` lists each public comparison ID and the five relative
event paths. Live events are in `live/seed-<seed>/events.jsonl`. Baselines are in
`baselines/seed-<seed>/<strategy>/events.jsonl`.

The common event envelope has `schema_version`, `run_id`, `strategy`,
`scenario_comparison_id`, `task`, `simulator_version`, `scorer_version`, and
`timestamp`. Group the five strategies by `scenario_comparison_id` and key
chart points by `(run_id, week)`. A reconnect can read from the beginning and
upsert those keys. Read only complete newline-terminated records.

| Event | Fields to use |
| --- | --- |
| `week` | `week`, `end_day`, `weekly_operating_surplus`, `cumulative_operating_surplus` |
| `week` decision details | `observation` contains the public view before the action. `requested_controls` and `applied_controls` show the decision. `weekly_result` contains the resulting public ledger and status. |
| `final` | `completed`, `stop_reason`, `end_day`, `score`, `verified_accounting` |

For the chart, use completed week on the horizontal axis and cumulative
operating surplus in USD on the vertical axis. Show negative values and
falling weeks. The live strategy is labeled `opencode`. Reveal the matching
baseline points only up to the live strategy's latest completed week. A
completed year has 53 weekly points, with week 53 ending on day 365.

Pausing is a viewer action. Hold the displayed week while continuing to read
and buffer events. For the audience decision, render only the pre-decision
`observation` first. Reveal the controls and the matching weekly result on
request, then advance the display or catch it up with the live feed. After an
interruption, leave the chart at the last completed week. Use the final record
to show verified surplus and demand served. A separately verified hindsight
plan can be displayed as an additional annual endpoint.

## Smoke test the live path

```bash
uv run --directory engine python scripts/check_cala_live.py \
  --out ../runs/cala-live-smoke
```

The smoke test uses a scripted Harbor oracle and pauses it after the first
weekly decision. A host reader checks that week 1 is visible before week 2
advances. It also checks that the 53rd point ends on day 365, that the final
cumulative surplus matches the verifier, and that the Harbor reward uses the
upper-bound score.

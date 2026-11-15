# SupplyGym outcome scoring

## Cala hindsight benchmark

For a single Cala seed, run the frontier model and any other scheduled external
agents on the same Harbor export. Then pass one `--job NAME=PATH` per external
method. The evaluator verifies their saved tool transcripts and runs the
availability reference and four deterministic formulas on the same seed and
salt. You can also supply a saved job for a deterministic policy by using its
registered name. The search starts only after all supplied jobs have a verified
outcome. An unfinished run earns zero.

```bash
uv run --directory engine python scripts/score_cala_hindsight.py \
  --task-dir "$PWD/harbor/YOUR_DATASET/supplygym-cala-2-seed-62000" \
  --job "gemini=$PWD/jobs/YOUR_GEMINI_JOB"
```

The hindsight search sees the realized demand and supplier health for the year.
It starts with the highest-surplus completed plan and tests changes to one
week's controls at a time. The best plan is replayed through the Cala public
tools before its surplus enters the report. Use `--sweeps` to change the number
of search passes. The oracle plan changes orders only through weekly controls.

The lower bound is the best verified feasible plan. To compute an upper bound,
the evaluator temporarily relaxes the ordering constraints in its calculation
and assigns each demand unit the cheapest possible landed cost through that day,
including holding. It also credits an optimistic value for prepaid inventory.
The bound can assume shipment quantities and freight choices that the weekly
controls cannot produce. It is reported as a dollars figure and never executed
as an oracle action.

The best verified weekly plan sets the scoring benchmark and earns 1.0.
For a positive benchmark, a completed method's score is its operating surplus
divided by the benchmark, with a floor of zero. The report also shows the
dollar shortfall. For a benchmark of zero or less, the score starts at 1.0
and subtracts the dollar shortfall divided by the larger of one cent and the
absolute benchmark, with a floor of zero. Unfinished runs score zero.

The upper bound and gap show how much room the search may have missed.
`global_optimum_certified` records whether the bounds agree to the cent.
Scores use the verified plan even when a gap remains. Record the search
settings with the scores since additional sweeps can change the benchmark.

## Cala paired score

Use `supplygym-cala@2` for new paired evaluations. Export the scheduled Cala
scenarios once and keep those exports. The paired scorer replays the agent's
verifier transcript and runs `ss`, `base_stock`, `fixed_lot`, and
`fill_adjusted_base_stock` locally on each matching seed and salt. It uses the
same task and tool-call budget. The baseline replays require no model calls.

For a completed scenario, let `B = service × max(0, surplus_ratio)`. Count
`W` baselines that the agent beats on `B` and `L` baselines that beat the agent.
Treat differences of at most `1e-9` as ties.

```text
paired_score = (0.60 - 0.10 × L) × B + 0.10 × W
```

An unfinished agent scenario earns zero. For a completed scenario, the
multiplier ranges from 0.20 to 0.60 and the win bonus ranges from 0 to 0.40.
The score stays in [0, 1]. Operating surplus in dollars is reported alongside
the score. A win is based on the service-adjusted score, which incorporates
both demand served and annual surplus.

From the worktree root, pass the dataset used for the Harbor job and the job
directory. The exports contain the private scenario salts. Keep them with the
trusted evaluator.

```bash
uv run --directory engine python scripts/score_cala_paired.py \
  --dataset "$PWD/harbor/YOUR_DATASET" \
  --job "$PWD/jobs/YOUR_JOB"
```

The report averages over all Cala entries in the supplied dataset. Unresolved
infrastructure failures keep their scheduled slots and make the aggregate
provisional. Reusing the saved quick job produces an exploratory reanalysis.
Freeze the rubric and the Cala schedule before the next held-out run. Harbor's
`reward.txt` contains the single-agent v2 grade. The paired score is in this
script's report.

## Version 2 verifier and historical comparison

The version 2 tasks award credit for completing a procurement year and meeting
published service and operating-surplus targets. The environment's authoritative
ledger determines the result. The same verified state always receives the same
score. The rubric is stored in `outcome_scoring.yaml`.

| Verified outcome | Scenario score |
| --- | ---: |
| Complete all 365 days, serve at least 98% of demand, and achieve an operating-surplus ratio of at least 20% | 1.0 |
| Complete all 365 days, serve at least 95% of demand, and achieve a nonnegative operating-surplus ratio, while falling short of full credit | 0.5 |
| Any other agent outcome, including an unfinished year | 0.0 |

These thresholds are initial benchmark design choices. They express service and
financial targets. They require a new rubric and manifest version if changed
after evaluation. Full credit is a target outcome and can be achieved by more
than one strategy. A reference policy's score has no role in calculating another
agent's score.

The verifier computes the following quantities from the complete ledger.

```text
service = units_sold / total_demand
operating_surplus = sales_revenue
                  - purchase_cost
                  - holding_cost
                  - lost_sale_penalties
                  - expedite_cost
                  - switching_cost
potential_revenue = selling_price * total_demand
surplus_ratio = operating_surplus / potential_revenue
```

Operating surplus measures cash generated over the scenario horizon. Opening
inventory and freight are prepaid scenario resources. Purchases during the year
are charged in full. Terminal inventory has zero value for this rubric. This
valuation deliberately makes late purchases costly and should be considered when
interpreting behavior near the end of the year. Simulator profit remains a
separate accounting quantity.

Threshold comparisons use unrounded ledger values and inclusive bounds. Full
credit takes precedence over partial credit. Completion requires the authoritative
clock to reach day 365 through 53 successful weekly decisions. Budget exhaustion
on the final successful decision still earns the verified outcome. An early
`finish`, an agent returning no action, and budget exhaustion before completion
earn zero. Grading reads the existing state without advancing the environment.

The verifier checks that ledger values are finite and nonnegative where required,
units sold plus units lost equal demand, and sales revenue reconciles to units
sold. An invalid authoritative ledger is recorded as a verifier infrastructure
failure. Agent-written explanations and claimed scores are excluded from grading.
The suite includes positive-demand scenarios. A zero-demand episode earns zero.

The frozen schedule contains the following equally weighted scenario families.

| Task | Development seeds | Held-out seeds | Weight per scenario |
| --- | --- | --- | ---: |
| `supplygym@2` | 1000–1009 | 62000–62009 | 1 |
| `supplygym-fresh@2` | 1000–1009 | 62000–62009 | 1 |
| `supplygym-cala@2` | 1000–1009 | 62000–62009 | 1 |

Each split contains 30 scheduled scenarios. Development and held-out results are
reported separately. The manifest freezes scenario IDs, seeds, world/task
versions, initial-state hashes, role, rubric identities, weights, runtime versions,
source digest, tool budget, agent time budget, and retry limit. It includes every
scenario before the first evaluation begins. Store the held-out manifest and
audit files with the trusted evaluator. Publish the rubric, family composition,
weights, budgets, and retry policy before participants run.

Every agent receives the same initial inventory, prepaid orders, supplier terms,
signals, and exogenous scenario realization for a given schedule entry. The
initial-state hash is checked on every attempt. SupplyGym has one buyer role, and
all agents occupy that role in every scenario. Each agent faces all three opening
conditions with equal total weight. If a future environment adds asymmetric
roles, include every role assignment with equal total weight per underlying
scenario before freezing the schedule. The current schema accepts the buyer role.

The suite permits 120 tool calls and 3,600 seconds spent awaiting agent reset and
action selection per scenario. Failed tool calls consume the tool budget. Each
scenario starts a fresh policy instance. The sequential runner requires registered
Python policies to yield to the async event loop. External agent processes need
their adapter to enforce process termination on timeout. Operating purchases and
other simulated costs are scored through the rubric. Model usage cost, execution
time, and invalid-action counts are separate telemetry.

For scenario scores `q_i` and predetermined positive weights `w_i`, aggregation is

```text
overall_score = sum(w_i * q_i) / sum(w_i for every scheduled scenario)
```

The default weights make this the arithmetic mean of all 30 scenario scores in a
split. Missing and unfinished scenarios contribute zero and retain their weight.
Four completed successes out of ten scheduled scenarios produce 0.40. A report
saved during execution also contains every pending scenario with zero current
credit and is marked provisional.

The infrastructure retry policy allows one initial attempt and two retries. An
explicit transport `InfraError`, environment exception, or verifier failure
triggers a fresh attempt with the same seed, initial state, and budgets. Every
attempt is retained. The first attempt with an agent outcome is final for that
scenario. Agent exceptions, invalid actions, voluntary stops, and exhausted
budgets receive their defined outcome without a retry. Exceptions raised by a
trusted agent transport adapter may use `InfraError` only for infrastructure
failures.

After three infrastructure failures, the scenario score is recorded as null and
the failure remains separate from agent failure. The report retains the scheduled
weight and shows a provisional lower-bound overall score with
`valid_for_comparison=false`. An official comparison requires every scenario to
have a verified agent outcome. The runner continues the remaining schedule.

Each report records the overall score, denominator, per-scenario outcome, verified
metrics, and every attempt. Attempt artifacts contain the full tool-call/result
trace, initial and final authoritative states, final-state hash, elapsed time,
invalid actions, tool usage, policy implementation, factory configuration, and
instruction text. The manifest records Python and numerical-library versions,
and a source digest that covers local changes and packaged data. Reference and
control policies use registered deterministic defaults and incur zero provider
cost. Provider cost and token usage for other adapters are null when unavailable.
Adapters introducing external models must retain their resolved model, generation
settings, prompts, and usage receipts alongside the evaluation artifacts.

The `audit` command reconstructs the environment from the manifest, replays every
tool call, compares tool results and final state, recomputes the rubric, and checks
the aggregate. Audit files belong to the trusted evaluator and should be kept
outside the evaluated agent's writable filesystem. Source or runtime changes
require the matching archived checkout to replay an existing result.

Four replenishment formulas run on the same schedule with default supplier
allocation, safety stock, order multiplier, expediting, and supplier approvals.
All four use the same daily order cap and per-supplier rounding. Here, inventory
position is on-hand stock plus the estimated pipeline, and `s` and `S` are the
simulator's existing reorder point and order-up-to level.

| Policy | Daily order quantity before cap and rounding |
| --- | --- |
| `ss` | Order up to `S` if inventory position is at or below `s`. |
| `base_stock` | Order the positive shortfall from inventory position to target `s`. |
| `fixed_lot` | Order 100 units (one day of mean demand) if inventory position is at or below `s`. |
| `fill_adjusted_base_stock` | Order the positive shortfall to `s` after multiplying each unacknowledged order's quantity by its supplier's observed fill ratio. Acknowledged quantities enter in full. |

The deterministic `reference` policy shifts default `ss` supplier allocation
using public fill ratios and risk. `do_nothing` makes no calls. `stop_early`
uses the reference for four weeks and finishes. Both incomplete controls must
earn zero on every scheduled scenario.

Run these commands from `engine` in this checkout after installing the locked
project environment. Use the supplied frozen manifest to reproduce this suite.

```sh
uv run python -m crucible.envs.supplygym.scenarios run \
  --manifest ../configs/supplygym-formula-comparison-v1.json \
  --split dev --out runs/formula-comparison-dev

uv run python -m crucible.envs.supplygym.scenarios run \
  --manifest ../configs/supplygym-formula-comparison-v1.json \
  --split heldout --out runs/formula-comparison-heldout

uv run python -m crucible.envs.supplygym.scenarios audit \
  --report runs/formula-comparison-heldout/agent-0/report.json
```

The default order is the four formulas in table order, followed by
`reference`, `do_nothing`, and `stop_early`. Pass `--policies`
to select registered agents. Every output directory must be new, so concurrent
or repeated invocations cannot overwrite an earlier evaluation. A terminated
runner leaves its latest report provisional. Keep that artifact and rerun the
suite under a newly identified evaluation after resolving the infrastructure
failure. This implementation supports in-run retries and fresh complete runs.

To prepare a new suite after a deliberate source or runtime change, freeze it
before evaluation and publish its identity. Review development results before
running held-out scenarios. Once held-out results have informed a design change,
use a new reserved seed bank for the subsequent held-out assessment.

```sh
uv run python -m crucible.envs.supplygym.scenarios freeze --out new-suite.json
```

The formula comparison on 2026-11-14 produced the following results. All agents
used the manifest with SHA-256
`eb011c2b245274bbca0a97cfb19d4bcd942cb8d77a6ac27891e023e4199bd469`.

| Policy | Development mean, 30 scenarios | Held-out mean, 30 scenarios |
| --- | ---: | ---: |
| Default (s,S) controls | 0.483333 | 0.450000 |
| Daily base-stock | 0.400000 | 0.333333 |
| Fixed lot size | 0.150000 | 0.083333 |
| Fill-adjusted base-stock | 0.633333 | 0.600000 |
| Availability reference | 0.600000 | 0.600000 |
| Do nothing | 0.000000 | 0.000000 |
| Stop after four weeks | 0.000000 | 0.000000 |

Both completed splits had zero infrastructure failures and zero invalid actions.
The 60-row comparison is in
[`formula-comparison.csv`](../../engine/runs/formula-comparison.csv). Detailed
reports and traces are in `engine/runs/formula-comparison-dev` and
`engine/runs/formula-comparison-heldout`, with folders `agent-0` through
`agent-6` in the policy order shown above. Generated run artifacts are local,
gitignored files.

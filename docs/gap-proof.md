# Reproducible paired gap proof

Implements [issue #21](https://github.com/vaartnou/crucible/issues/21). This analysis is
independent of whether SupplyGym retains PPO or changes its world/data. Select and freeze
the world, scoring version, default, hindsight ceiling and reference before collecting
confirmatory runs. No SupplyGym performance claim is made by adding this script.

## Protocol to freeze before LLM runs

Commit the actual task version, engine commit, scoring hash, dataset/world version, agent
models/prompts/configuration, seed list, repeat IDs, primary comparison and direction to the
experiment record before starting. Use the same world and exogenous random streams for
every agent on a seed. A matching integer seed alone does not establish common random
numbers: interventions must not change the stream used for exogenous events.

Default prospective plan: baseline runs on seeds **0–299**; the LLM and all comparison agents
on seeds **0–24**, collected in five batches of five, in that order. **Stop at 25 complete
paired seeds**, with no early stopping for significance or effect size. Batch reports are
operational checks only. If cost, outages or safety force an early stop, mark the result
incomplete/exploratory; do not claim the planned confirmatory result. Resolve infrastructure
failures on the same seeds with the frozen retry policy; do not replace difficult seeds.

The primary hypothesis is a positive paired mean score difference for one named agent A
versus one named default B. Use **alpha = 0.05**, 10,000 paired bootstrap replicates, RNG seed
21, and a two-sided exact sign test with ties removed. Require both a positive lower 95%
bootstrap bound and sign-test p < 0.05 for the planned gap claim. The sign test measures
win probability among nonties, whereas the bootstrap estimates the mean score difference.
These answer different questions; report both even when they disagree. Bootstrap coverage
is approximate, especially for small samples. Other agent pairs are exploratory: intervals
are pointwise and p-values are unadjusted, so they do not support multiple confirmatory claims.

Choose hard-seed criteria from independent baseline evidence before seeing LLM outcomes,
and record the exact subset. Always report all 25 seeds beside this subset. A retrospective
subset is exploratory. The script labels every hard subset exploratory conservatively.
Baseline-only comparisons on 300 seeds must be a separate report from LLM comparisons on
25 seeds; do not silently intersect them or treat 300 as the LLM sample size.

## Reproduce from saved runs

From the repository root, after saving a `crucible eval` run:

```sh
uv run --directory engine python -m crucible.gap_proof --runs ../runs/experiment --task incident@1 --agents reference,do_nothing --seeds 0-24
```

The script prints the full Markdown report to stdout, including every agent pair and every
per-seed difference. Redirect stdout to save it. It never calls a model or runs a simulation.
Use `--hard-seeds 3,7,12` to add a subset, and `--default do_nothing --ceiling hindsight`
to add the normalized gap diagnostic. Both anchor agents must have saved results on every
requested seed/repeat; a hindsight ceiling must be supplied by the environment and must not
be assumed equal to an ordinary reference policy.

`--runs` accepts multiple CSV files or run directories containing `summary.csv`. Native eval
columns are `task,policy,seed,repeat,total` (extra columns are allowed); external exports may
use `agent` and `score` instead of `policy` and `total`. Select only the requested task,
agents and seeds. Duplicate records, missing seeds, mismatched repeat IDs, nonfinite scores
and episodes with `stop_reason=error` fail the report. Repeat IDs may vary between seeds,
but all agents must have the same repeat IDs on each seed. Repeats are averaged per seed,
then seeds are resampled together, preserving pairing and avoiding pseudoreplication.

Win rate is strict wins divided by all seeds, with ties reported separately. The normalized
gap is the mean of per-seed `(agent - default) / (hindsight ceiling - default)`, not a ratio
of pooled means and not a reward. Nonpositive denominators are excluded only from this
diagnostic and their count is printed; the raw paired report still includes those seeds.

CSV summaries do not establish world identity. The experiment owner must check archived
transcripts/manifests for matching engine/world/scoring versions and random streams before
combining files. In particular, separately exported Harbor datasets have different secret
salts: compare agents using the **same exported dataset**, and never pair salted Harbor runs
with unsalted local eval runs solely by numeric seed. Harbor JSON is not read directly;
export its verified scores and task/agent/seed/repeat identifiers to the CSV schema above.

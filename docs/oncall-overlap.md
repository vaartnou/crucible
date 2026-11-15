# Overlapping incidents: oncall@2

Completed matched comparisons across Gemini Pro, Flash, and Flash-Lite are in
[the model comparison report](oncall-model-comparison.md), including recovery impact separately
from overall reward and the limits of the observed workflow gap.

`oncall@2` adds a second release regression to the existing 23-service simulation. It preserves
the time and call budgets, physical model, intervention semantics, and reward weights. The
original single-fault task remains available as `oncall@1`; bare `oncall` selects the newest
registered version, so use explicit version references in comparisons.

The first fault comes from the existing six-fault distribution. Another owner's recent release
develops a canary memory leak during investigation. If the first fault is already a memory leak,
the second release has a partial rollout regression instead. Deployment history exposes ordinary
version changes; logs and runtime metrics provide the evidence of faulty behavior. Both causes
must be repaired for sustained recovery. Two overlapping regressions are a deliberate challenge
distribution, not an estimate of how often production incidents have multiple causes.

This tests prioritization, developing symptoms, residual-impact investigation, and closure after
sustained recovery. It is still a small deterministic fluid model, with a regular dependency
graph, a narrow fault catalogue, synthetic memory growth, and no human escalation or operational
communications. It does not establish a broad on-call capability ranking.

## Comparison workflow

The existing graph walk and reference rules now retain diagnoses across multiple repairs.
`incident_runbook` additionally checks memory trends before tracing current errors. This catches
a developing regression early enough to avoid its later outage. All policies use the same public
tools, and their reads and actions incur the same costs. The [runbook](oncall-runbook.md) explains
the operational workflow and the scope of its executable approximation.

Diagnosis credit uses the best one-to-one assignment between reports and causes. Missing causes,
duplicate reports, and extra guesses cannot receive full credit. The impact score remains avoided
failed requests relative to the same world's no-action trajectory.

## Reproduce a matched comparison

Run from the repository root. Exported salts and verifier tokens are private; retain the original
export and do not publish its folder.

```sh
uv run --directory engine crucible export harbor --task oncall@2 --seeds 0-9 \
  --out ../runs/oncall-overlap-export
sh runs/oncall-overlap-export/build-sidecar.sh
uv run --directory engine python scripts/measure_oncall_export.py \
  --export ../runs/oncall-overlap-export --out ../runs/oncall-overlap-rules
harbor run -p runs/oncall-overlap-export -a opencode -m google/gemini-3.8-flash \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY --ak version=1.19.5 -n 2 \
  -o runs/harbor-jobs --job-name oncall-overlap
uv run --directory engine crucible regrade ../runs/harbor-jobs/oncall-overlap \
  --export ../runs/oncall-overlap-export --numeric-tolerance 1e-12 \
  --out ../runs/oncall-overlap-replay.json
```

Set the provider credential in the launching process's environment, or use Harbor's `--env-file`.
The explicit credential selector is needed when Harbor cannot identify the native Google model
slug. Provider authentication and schema errors are excluded from agent-performance measurements.
Tool schemas express optional empty strings with patterns because Google rejects empty enum values.

Linux and macOS can differ by a few floating-point units in edge telemetry. Replay is exact by
default. The explicit tolerance above permits only float differences within absolute/relative
`1e-12`; strings, list structure, integer counters, and tool status remain exact. Reports retain
the first exact divergence separately, so numeric roundoff is visible. The UI uses the same bounded
tolerance when reconstructing saved worlds and refuses a comparison if recorded evidence does not
match an available world. Rewards are not recomputed with rounded physics.

## Demo

```sh
cd web && npm ci && npm run build && cd ..
uv run --directory engine crucible ui --jobs-dir ../runs/harbor-jobs --exports-dir ../runs
```

In Play, select **Overlapping incidents**, then **New Episode**. Agent view exposes only discovered
evidence; Full cluster and Reveal Secret are presentation controls. Reveal Secret highlights both
causes. Add each cause to the diagnosis list before closing.

In Runs, open a completed Harbor transcript and select Compare, then `incident_runbook`. The
comparison reconstructs the recorded task, seed, and private salt before running the workflow.
Show the impact curve and action timing, then scrub back to the memory trend that distinguishes
early prevention from repairing the second fault after it becomes visible in errors.

The ten-world private development export used here is `runs/oncall-overlap-export-v2`; rule
transcripts and machine-readable results are in `runs/oncall-overlap-paired-v2`. Those rules
recover and correctly diagnose 10/10 worlds: graph walk/reference mean 0.772753, runbook mean
0.872343, newest-change mean 0.028538, do-nothing zero. All fifty rule transcripts replay exactly
on the local runtime. A separate unsalted development set is not a matched model comparison.

The first two valid Harbor screening trials score 0.733571 and 0.690495, against runbook scores
0.900476 and 0.886279 on those exact worlds. Both model trials recover with correct diagnoses and
zero penalties. This two-world pilot gap is 0.181344, below the historical 0.20 mean-gap target;
it must not be presented as a passed hard-evaluation gate. The larger matched result is recorded
separately when available.

For the pilot's first world, the runbook reverses the developing release at minute 10 and the
configuration fault at minute 23. Graph walk repairs the configuration at minute 18 and the release
at minute 64. The standalone model repairs at minutes 44 and 48, scoring 0.733571. This gives a
concrete causal demo of the measured difference, with identical tools and action costs.

## Measurement checkpoint

All ten Gemini 3.8 Flash Harbor trials have completed: model mean **0.672316** versus runbook
**0.872343**, a paired gap of **0.200027**. Only four of the ten gaps reach 0.20, so the historical
gate requiring seven of ten such gaps fails despite narrowly meeting the mean-gap threshold.
Do not describe the mean gap alone as passing that gate. The model restores service in all ten
worlds and completes the correct diagnosis in nine; one trial reaches the eighty-call limit
without submitting its report.

The separate 50-world unsalted rule audit has no environment errors. Reference averages 0.771718
and the runbook 0.870816. Both restore service in 50/50 worlds and fully diagnose 49/50. On seed 49,
both also report a helpful mitigation as an independent trigger; the extra report reduces diagnosis
credit to two thirds. Every generated world in this scenario family contains a memory leak, and
the preflight rule is tailored to that family. Broader on-call rankings need other fault families.

Validation: 252 final targeted tests, seven oncall golden checks, 27 API/MCP integration checks,
six web tests, strict Pyright, Ruff, and the UI build pass. The earlier full run had 711 passes,
13 failures, 31 skips, and three expected failures. Two failures were missing expectations for the
new task, now fixed and retested; eleven were sandbox socket-bind denials, covered by the permitted
integration rerun. This is a reconciled validation record, not a second green full-suite run.

The UI has been checked with an actual salted model transcript and the runbook comparison.
To reproduce the completed Flash comparison:

```sh
uv run --directory engine python scripts/compare_oncall_harbor.py \
  --bots ../runs/oncall-overlap-paired-v2/results.json \
  --jobs ../runs/harbor-jobs/oncall-overlap-valid-2 ../runs/harbor-jobs/oncall-overlap-holdout-8 \
  --export ../runs/oncall-overlap-export-v2 --out ../docs/oncall-overlap-measurements.json
```

The comparison checks simulator source directly from the exported wheel, private world salt
digests, model/agent identity, and observation/reward replay. It rejects duplicate attempts and
excludes trials with provider or environment exceptions. The four earlier authentication/schema
failures are retained in separate job folders and excluded from performance measurements.

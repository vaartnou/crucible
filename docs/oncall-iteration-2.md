# Oncall causal-model iteration

Requested 2026-11-15: replace shortcuts with coherent timeout/pool/retry dynamics and ambiguous, truthful change histories; rerun paired measurements without weakening the public baseline.

## Evidence from the previous ten Flash successes

Every run began with alerts followed by the global change list. None needed the topology tool. Fault changes had numeric IDs while distractors used `chg-other-*`; config incidents often had only one config change. Pod versions reflected the culprit deployment but not the unrelated deployments advertised in history. All root services were workers; equal-severity alerts retained topological order. These were implementation shortcuts, not evidence of strong incident reasoning.

The previous result remains in `docs/oncall-measurements.json`: Flash mean 0.931506, graph walk 0.950403, gap 0.018897, zero seeds with a 0.20 gap. That baseline must not be overwritten or presented as the revised model's result.

## Acceptance criteria

- Arrival rates propagate from ingress through explicit edges. Requests, retry attempts and successful completions have distinct meanings.
- Timeouts depend on observed callee latency and edge deadlines; healthy callees can coexist with timeout failures at callers.
- Pool occupancy follows rate times holding time. Pool admission limits calls before they reach the callee.
- Retries create extra offered load, and load influences latency/errors. This interaction must have a bounded deterministic solver and tests for stability.
- Changes update actual configuration/deployment state. Reverting a revision changes those parameters; merely recognizing a hidden fault label cannot restore health.
- Multiple plausible changes, including later changes on the same service, use the same identifier format. Their timing, effective values and pod versions remain inspectable.
- Public telemetry exposes enough evidence for graph walk/reference to select the triggering change and demonstrate recovery. No provider-specific prompts, hidden metadata access, or reduced baseline checks.
- Preserve reward weights. Validate healthy worlds, causal interventions, determinism, stale/wrong rollbacks and exploitation before spending on models.

## Measurement protocol

Freeze simulator source before measurement. Run all bots on 50 salted seeds, then Flash on ten of those identical worlds; the comparison verifies source and salt digests. Inspect every failed or anomalous episode. A provider error does not count as a model loss. Regrade current transcripts for exact replay; old-simulator transcripts should visibly diverge. Harbor oracle/nop remains an independent wiring check. Harbor model runs require the direct screening gap first.

## Implementation

Timeouts depend on callee latency; pool occupancy equals offered attempts times connection holding time, and only admitted attempts reach the callee. A damped fixed-point solver couples those rates back to service utilisation, latency and errors. Unconverged states raise an explicit simulator error; they are not silently scored as agent failures. Public edge telemetry includes the residual and iteration count. Tests cover healthy demand conservation, timeout-only caller failures, pool admission with healthy callees, retry amplification, capacity monotonicity, exact chronological settings, overload during partial rollout, and recovery with a draining queue.

Change IDs and author formats no longer distinguish triggers. Healthy owners and the culprit have real, safe timeout/pool reductions and retry increases before the incident, plus later unrelated changes. Pod versions reflect deployments. Leaks begin at deployment and accumulate memory over time; public deploy diffs contain release versions, not omniscient bug coefficients. Historical metric series and edge observations remain available to both bots and models.

Graph walk now selects a revision from runtime evidence, onset and change history, and verifies all services after recovery. Reference additionally checks leak memory slope. `newest_change` is a separate diagnostic baseline using the same traversal but reverting the latest local change. It does not replace any requested gate.

Reward weights and numeric gates are unchanged. `solved` now explicitly means verified recovery **and complete correct diagnosis**, rather than imposing a per-episode reward floor; this does not change raw scores or gate outcomes. Persisting backlog can make a correct repair earn a low impact score. The original plan's graph/reference gates are means, not per-episode floors.

Remaining bounds: one generated fault; regular branch topology; synthetic latency distribution and correlated-retry approximation; workers represented in a synchronous dependency graph; cache restart is a transient cache degradation, not explicit cache-miss traffic into a backing database. No traces, full fault catalogue, or generation rejection filter.

## Frozen paired results

The 50-seed batch ran all nine policies (450 episodes) in **186 seconds** with no environment errors. Graph walk and reference both recovered and correctly diagnosed **50/50** cases.

| Policy | Seeds | Mean reward |
| --- | ---: | ---: |
| Graph walk | 50 | 0.873600 |
| Reference | 50 | 0.873600 |
| Newest local change | 50 | 0.273136 |
| Do nothing / loudest alert / each exploit | 50 each | 0 |

The diagnostic newest-change bot recovered 38/50 but correctly diagnosed only 36/50; unnecessary reversals and delays reduced reward. This supports the narrower claim that chronological/runtime evidence matters. It does not establish that a capable model loses.

On **the same ten salted worlds**, graph walk averaged **0.881700**, while Gemini 3.8 Flash averaged **0.858337**. Mean gap **0.023363**; **1/10** seeds reached a 0.20 gap. The target is 0.20 mean and at least seven such seeds, so **the hard-eval screening gate fails**. Flash recovered all ten, with complete correct diagnosis on nine. There were no provider/environment errors. Direct model wall time: **239 seconds**. These are direct API results, not Harbor model results. Pro and Harbor model batches were not run after this failed screening gate.

The bot mean reward gates pass. The strict exploit-below-loudest gate still fails because every exploit and loudest alert score zero; it was not silently changed to a non-strict comparison.

Detailed paired rows, scores, source identities and timing are in [oncall-causal-measurements.json](oncall-causal-measurements.json). Original first-iteration measurements remain separate. Final run directories: `runs/oncall-causal-v2-bots`, `runs/oncall-causal-v2-flash`. The private salt is retained outside tracked artifacts.

## What the remaining model errors show

- Seed 2: the consumer crash required rollback and queue drainage. Flash investigated for 19 calls before repairing, then reported `partial_rollout` instead of `queue_backlog`. Reward 0.572024 versus graph walk 0.780985; this is the one threshold-sized gap.
- Seed 8: Flash correctly reverted the retry configuration, then restarted a healthy worker while waiting for existing backlog to drain. The unnecessary restart cost 0.07. Reward 0.495590 versus graph walk 0.612576. Much of both agents' loss is queued work, not failure to identify the cause.
- In several other cases Flash repaired sooner than graph walk and scored higher. The loudest-alert bot remains a poor proxy for this model.

## Feedback-loop validation and provenance

The first holdout sweep exposed three baseline mistakes: the bot compared the triggering caller change with the later overloaded-callee onset. The corrected public rule uses caller timing, retains earlier onset evidence, and queries pre-onset edge latency. Shifted-timestamp unit cases guard against seed-specific logic. World generation and thresholds were not changed to erase these failures.

Initial exploit sweeps were too slow because every harmless action ran two thirty-minute projections. Fast paths now skip projections only when effective behavior is provably unchanged; 19 slow/fast equivalence cases cover all six faults across three salts. A bounded baseline cache keys the complete immutable world plus horizon; four tests cover equality, nested mutation, different worlds sharing a fingerprint, and eviction. A representative shotgun episode fell from 33.88s to 5.26s under differing concurrent load; this is a rough microbenchmark, not the full-loop timing.

All ten pre-optimization model transcripts reproduced identical observations and scores under the optimized code. The final matched model batch was nevertheless rerun fresh against the exact same source as its bot comparison. Environment fingerprints are separate from policy-package fingerprints so future baseline improvements can be tracked without misidentifying unchanged dynamics.

Harbor oracle/nop and replay checks are recorded separately in [Harbor runs](harbor-runs.md). Its fresh private salts differ from this batch, so raw means must not be compared across those exports.

## Final verification

- Full suite: **431 passed, 3 skipped**; Ruff lint/format, strict Pyright and pre-commit passed.
- The completed full suite took 448s because the scaffold subprocess redundantly reran oncall. A subsequent test-only selector fix retains all outer oncall checks and selects the generated task inside the subprocess; all 10 scaffold tests then passed in 1.45s. Runtime files were unchanged.
- Final ten Flash transcripts replay with identical observations and scores. All four optimized Harbor oracle/nop trials also replay exactly against their original export salts.
- The failed model screening gate is retained. No Pro or Harbor model benchmark was launched, and no reward threshold was relaxed.

# Oncall implementation and feedback loop

The new experimental `oncall@2` adds overlapping regressions and a public-tool runbook workflow.
See [scenario design, measurement scope and demo instructions](oncall-overlap.md). The original
single-fault results below describe `oncall@1` and must not be carried over to the new task.
The [completed four-model comparison](oncall-model-comparison.md) shows a large gap against
Flash-Lite, while Pro's recovery impact remains competitive with the runbook.

Status: experimental, 2026-11-15. **The hard-eval claim has not passed.** The [causal-model iteration](oncall-iteration-2.md) is implemented and measured: paired Flash 0.8583 versus graph walk 0.8817 (gap 0.0234; 1/10 seeds at the target gap).

The implementation replaces the old environments with `oncall@1`, preserves guess as a test fixture, and adds Harbor export, replay, and bot gate commands. Direct model screening was used for the historical measurements and has since been removed. The original proposal is preserved in [oncall-proposal.md](plans/oncall-proposal.md).

## What exists

- Salted 23-service worlds; an explicit 8-service thin slice via `reset(difficulty=1)`.
- One fault per world: partial rollout, config push, memory leak, pool exhaustion, retry storm, or queue backlog. Cache restart can cause an additional temporary outage.
- Minute snapshots shared by metrics, logs, alerts and pod/event views; strict command grammar, call/horizon budgets, delayed actions, structured diagnosis, post-resolution recovery probes.
- Public-tool graph walk/reference, naive loudest-alert baseline and exploit policies.
- Harbor 1.4 task schema, named reward components, one shared image build per export, token-gated in-process oracle, and transcript export.
- `crucible gates`, historical `compare`, and `regrade`; Harbor handles model evaluations. Batch comparisons require identical salt and simulator source hashes. Provider errors cannot count as model reasoning failures.

The revised simulator uses a **deterministic fluid network** with explicit edges, finite connection pools, latency-derived timeouts, retry load feedback, and persistent worker queues. Changes update effective settings and version-keyed deployment behavior. Public tools expose current and historical edge settings, offered/admitted rates, pool rejection, and numerical convergence diagnostics. Service names and root depth vary, but graph structure remains regular. Reference extends public graph walk with memory-slope verification. See [the causal iteration](oncall-iteration-2.md) for evidence and fresh measurements. There are no traces, two-fault generated episodes, full fault catalogue, generation rejection filter, extra-capacity actions, or replay viewer. Do not describe these milestones as complete.

## Current workflow

All new model evaluations run in Harbor with OpenCode and Docker. The direct-model runner and
`crucible agent` command have been removed. Historical screening results below remain evidence
from their original harness, not Harbor results. See [UI setup and saved runs](using-environments.md#ui-and-saved-evaluations).

## Fast loop

Run from the repository root. Set a private `CRUCIBLE_EVAL_SALT` once for a paired comparison. Preserve it outside transcripts; only its digest is recorded.

```sh
uv run --directory engine crucible gates --seeds 0-49 --out ../runs/oncall-bots
uv run --directory engine crucible export harbor --task oncall@1 --seeds 0-9 --out ../runs/oncall-export
sh runs/oncall-export/build-sidecar.sh
harbor run -p runs/oncall-export -a opencode -m google/gemini-3.8-flash --ak version=1.19.5 -n 2 -o runs/harbor-jobs
uv run --directory engine crucible regrade ../runs/harbor-jobs/JOB \
  --export ../runs/ORIGINAL-EXPORT --out ../runs/harbor-regrade.json
```

`gates` returns exit code 1 if any requested gate fails, including the strict exploit gate. With nonnegative rewards and loudest-alert reward zero, exploits can only tie zero; the proposal's strict “below loudest” requirement cannot pass on that batch. This is reported explicitly, not silently changed to `<=`.

`regrade` runs recorded actions on current code, reports the first changed observation, and returns exit code 1 on divergence or unreadable transcripts. A changed simulator must be rerun before scores are used as a matched comparison. It never overwrites old trial transcripts.

Run bot checks before Harbor model batches. Harbor oracle/nop checks are wiring tests, not difficulty measurements. Docker has about 7.75 GiB allocated, so measured smoke runs use concurrency 2. The model API/mcp-server allowlist experiment failed on internal DNS; default networking remains and the integrity gap is documented in [reward-integrity.md](reward-integrity.md).

## First-iteration findings (superseded simulator)

The frozen 50-seed bot batch (`runs/oncall-gates-2`) has graph walk/reference mean **0.950403**, with do-nothing, loudest alert and all exploits at **0**. No bot episode errors. The strict exploit-below-loudest gate fails because of the zero tie.

The matched ten-seed Gemini 3.8 Flash direct-API screening scored **0.931506** versus graph walk **0.950403**: mean gap **0.018897**, with **0/10** seeds reaching the required 0.20 gap. All ten had verified recovery, correct diagnosis, zero penalties and no provider errors. All ten replayed with identical observations and scores. See [machine-readable measurements](oncall-measurements.json). This is a failed screening gate, not a Harbor model benchmark.

Flash transcripts show successful fault localization, pod-level inspection, correct rollback/config revert and global verification. The loudest-alert bot is therefore not a faithful proxy for this model on the current world distribution. The proposal's hypothesized alert-chasing failure has not been established.

The next design iteration should start from these successful transcripts: build coherent edge timeout/pool/retry dynamics and genuinely ambiguous change histories, then rerun the same paired checks. Do not inflate difficulty by hiding evidence or weakening the baseline.

First-iteration validation: 267 tests passed, 3 skipped; Ruff lint/format, strict Pyright and pre-commit passed. Final Harbor oracle/nop wiring results are in [Harbor runs](harbor-runs.md).

The first iteration stopped before Pro, a larger fault catalogue or demo expansion. The user then requested the causal-model iteration linked above. Changes are prepared together on `codex/oncall-gym`; the proposal’s four independently green commits have not been created.

# Oncall bug fixes and validation — 15 November 2026

**The grading-convergence and empty-incident bugs are fixed in the current working tree.** A fresh full suite passes: **444 passed, 3 skipped, zero failures/errors**, in 625.48 seconds. Both 1,000-world chronological audits pass without a convergence error or zero-impact incident. The model difficulty gate still fails; that experimental result was not treated as a software bug or tuned away.

This follows [the original revalidation](oncall-revalidation-20261115.md). Its measurements remain historical and were not overwritten.

## Changes

### Numerical convergence

The fluid solver retains its original fixed-point path for ordinary cases. When that iteration fails to reach the target residual, a bounded, damped Newton fallback solves the same traffic-balance equations using a finite-difference Jacobian, partial pivoting, and a backtracking line search. Candidate rates remain nonnegative; a line-search step must reduce the residual. The target residual remains **1e-7**, and the existing rejection threshold remains **1e-5**. No new numerical dependency was added.

This addresses both slow convergence and oscillation at steep timeout/pool transitions. Merely increasing the old iteration budget did not address the latter. The expanded audit exposed unsalted seed 942 and comparison-salted seed 668 in addition to the original unsalted seed 5; all now complete their full no-action grading horizon. Counterfactual convergence failures still raise an environment error rather than becoming model reasoning losses.

### Real incidents on lightly loaded services

Queue faults now limit processing relative to the worker's offered demand, including subsequent capacity increases. The bad consumer's processing capacity is capped at 65% of minimum post-incident demand, with the original 30% throughput fraction retained as an upper bound. Pods continue to run while backlog and user-facing failures develop; rollback restores throughput and drains the existing queue.

Pool-exhaustion faults now scale fluid pool capacity to offered concurrency without the absolute two-connection floor that could leave a low-traffic edge healthy. The larger audit exposed this second instance of the empty-incident bug. Neither fix changes fault selection, hides telemetry, or rejects inconvenient seeds. Scoring weights, convergence tolerance, bot policies and difficulty thresholds are unchanged by these fixes.

Implementation: `engine/src/crucible/envs/oncall/model.py` and `world.py`. Regression coverage is in `engine/src/crucible/envs/oncall/tests/test_physics.py`, including full-horizon grading on unsalted seeds 5 and 942, queue generation/rollback across unsalted and holdout worlds, and low-traffic pool faults on seeds 116 and 554.

## Validation

- Full pytest suite with loopback access: **444 passed, 3 skipped, zero failures/errors**. This is a complete final-source run, not a reconciliation of separate subsets.
- Focused dynamics/regression suite: **113 passed**.
- Ruff lint and formatting: passed; 104 files formatted correctly.
- Strict Pyright: zero errors and warnings. `git diff --check` passes.
- The CI CLI smoke command passes eight episodes across reference, graph walk, do-nothing and shotgun on unsalted seeds 0–1.
- Salted seeds 0–999: **1,000/1,000** full no-action horizons complete with positive baseline user-facing loss.
- Unsalted seeds 0–999: **1,000/1,000** full no-action horizons complete with positive baseline user-facing loss.
- Audit trajectories run chronologically from minute -30 to minute 150, preserving backlog between minutes. Maximum solver evaluation counts are 2,216 (salted) and 2,183 (unsalted). Every completed audit stays below the unchanged residual acceptance threshold. This audit covers no-action trajectories, not every possible intervention sequence.
- Fresh transcripts: **450/450 bot and 10/10 Gemini replays** have identical observations and exact reward equality.

## Fresh strategy and model results

Nine strategies were rerun on the same private salted seeds 0–49 in five worker processes. Wall time: 68.09 seconds. No episode errors; all configured bot gates pass.

| Strategy | Mean reward, 50 seeds | Solved |
|---|---:|---:|
| do_nothing | 0.000000 | 0/50 |
| loudest_alert | 0.014772 | 0/50 |
| graph_walk | 0.904941 | 50/50 |
| newest_change | 0.004928 | 1/50 |
| reference | 0.904941 | 50/50 |
| shotgun | 0.000000 | 0/50 |
| restart_all | 0.000000 | 0/50 |
| rollback_all | 0.000000 | 0/50 |
| guess_report | 0.000000 | 0/50 |

Gemini 3.8 Flash was rerun fresh on matching seeds 0–9, concurrency five, in 152.30 seconds:

| Policy | Same ten seeds: mean reward |
|---|---:|
| Graph walk | 0.908036 |
| Gemini 3.8 Flash | 0.902703 |

Gemini has **10/10 correct diagnoses**, **10/10 verified recoveries**, and **0 provider/environment errors**. The paired graph-walk-minus-Gemini mean gap is **0.005333**, with **0/10** seeds reaching a 0.20 gap. The existing hard-evaluation gate therefore **fails**. This is direct API screening, not a Harbor benchmark; no subsequent Harbor model batch was run.

## Provenance and artifacts

- Git HEAD: `1a941bca6a055ab2f2f954330b6664d418dd8cd7`, with the user's existing uncommitted changes preserved.
- Environment SHA-256: `6e33d26d8ba096d84033e57bd6a04776c345cad2c3f7c97924757a0f479ef8c1`.
- Full environment/policy package SHA-256: `b817db2634f3d3c0103f7108f65c2c1f49106132c61570eda118031e3903affd`.
- Both final bot and Gemini manifests match those source hashes. Comparisons reuse the earlier private salt, retained locally in ignored mode-0600 `.env.revalidation-20261115`; the salt and API key are excluded from the report and transcripts.
- Consolidated data: `runs/bugfix-20261115/results.json`.
- Test evidence: `runs/bugfix-20261115/pytest.xml`, `pytest.log`, `focused-tests.log`.
- World audits: `runs/bugfix-20261115/world-audit-extended.json` and `world-audit-extended-unsalted.json`.
- Final bot/model results: `runs/bugfix-20261115/bots/gates.json`, `gemini/summary.json`, `comparison.json`.
- Replay reports: `runs/bugfix-20261115/bots-replay.json` and `gemini-replay.json`.

Intermediate measurements are retained separately under `runs/bugfix-20261115/attempt-1`; they are excluded from the final results above. No commit or deployment was performed.

# Oncall simulation revalidation — 15 November 2026

The bugs recorded below were subsequently fixed; see [the follow-up validation](oncall-bugfix-validation-20261115.md) for the current source and fresh results.

**Verdict: the simulation is not fully validated.** The configured 50-seed bot gates pass, but two real test failures remain, generated queue incidents can have no effect, and the Gemini difficulty gate fails. No simulator, strategy, scoring, or test source was changed during this validation.

## Measurement scope

- Current working tree, including the pre-existing uncommitted hard-dynamics changes; Git HEAD `1a941bca6a055ab2f2f954330b6664d418dd8cd7`.
- Environment source SHA-256: `d727cfdab3058b2b286495957df42ce2ed72f5e910a313e18c6a35c5c4a35c9b`.
- Full environment/policy package SHA-256: `1bbbfa991da04721c4196e7fabfbb467f2649a4b6699278be9ab6057c23cf79a`. Confirmed unchanged at the end of evaluation.
- Python 3.15.0 from `engine/.venv`.
- One fresh private salt shared across every paired strategy/model world; fresh output directories. The salt is retained locally in ignored `.env.revalidation-20261115` with mode 0600; its value is excluded from this report and transcripts.
- Nine strategies × seeds 0–49 = **450 episodes**, using five independent ten-seed worker processes. Sweep wall time **43.68 seconds**.
- **Ten Gemini 3.8 Flash episodes**, seeds 0–9, concurrency five, covering all six fault types. Model wall time **138.44 seconds**. These are direct API screening trials, not Harbor benchmarks.
- Two further chronological no-action world audits: 100 salted and 100 unsalted worlds, from minute -30 through minute 150.

## Strategy results

`Solved` requires verified recovery and every diagnosis field correct. A verified flag alone does not establish that an incident was repaired: a world with no loss can also satisfy it.

| Strategy | Episodes | Mean reward | Solved |
|---|---:|---:|---:|
| do_nothing | 50 | 0.000000 | 0/50 |
| loudest_alert | 50 | 0.014772 | 0/50 |
| graph_walk | 50 | 0.888021 | 49/50 |
| newest_change | 50 | 0.004928 | 1/50 |
| reference | 50 | 0.888021 | 49/50 |
| shotgun | 50 | 0.000000 | 0/50 |
| restart_all | 50 | 0.000000 | 0/50 |
| rollback_all | 50 | 0.000000 | 0/50 |
| guess_report | 50 | 0.000000 | 0/50 |

All six configured bot checks pass, including the original strict exploit-below-loudest check. Loudest alert scores above zero on one world, so the all-zero exploit means are strictly below it in this batch. This is a batch result, not a general proof against exploits. There were **zero episode errors** in the salted strategy batch.

Graph walk/reference solve 49/50 worlds. Salted seed 31 is a queue-backlog world with no user-facing impact: both policies close with an empty diagnosis and receive zero. Seed 10 scores 0.601928 despite a correct diagnosis and recovery: it first reverts another service's configuration, waits, then finds the true trigger. The lower score reflects delayed impact reduction.

## Matched Gemini comparison

| Policy | Same ten seeds | Mean reward |
|---|---:|---:|
| Graph walk | 0–9 | 0.908036 |
| Gemini 3.8 Flash | 0–9 | 0.909850 |

The paired graph-walk-minus-Gemini mean gap is **-0.001814**. Gemini is slightly ahead on this sample. **0/10 seeds** have the required 0.20 gap. The existing gate requires a mean gap of at least 0.20 and at least seven seeds meeting that gap; it **fails**.

Gemini has **10/10 correct diagnoses**, **10/10 verified recoveries**, zero penalties, zero invalid tool calls, and zero provider/environment errors. It uses 13–27 tool calls per episode. Ten trials establish a screening result, not a precise model ranking or broad difficulty estimate.

The initial sandboxed model attempt produced ten connection errors. It is retained under `gemini-network-failed` and excluded from all performance calculations. The fresh retry with API network access produced the valid ten-episode batch. No Pro or Harbor model batch was launched.

## Validation and remaining failures

- Full pytest invocation: **425 passed, 13 failed, 3 skipped**, 260.86 seconds, out of 441 collected tests.
- Eleven failures were local-port permission errors in the sandbox. Rerunning the complete MCP/CLI live-server subset with loopback permission gives **18 passed, zero failed** in 3.44 seconds; seven of those had already passed. Across the full suite and that subset retest, the reconciled outcome is **436 passed, 2 real failures, 3 skipped**, rather than a separate green full-suite run.
- Ruff lint passes; Ruff formatting passes for 104 files; strict Pyright reports zero errors and warnings.
- **All 450 bot transcripts** replay with identical observations and exact reward equality.
- **All ten valid Gemini transcripts** also replay with identical observations and exact reward equality.

The two genuine failures are:

1. `tests/generic/test_generic.py::test_reference_solves_every_seed[oncall@1]`
2. `tests/generic/test_generic.py::test_exploits_score_below_reference[oncall@1]`

Both trace to **unsalted seed 5**, a retry storm at `profile-46`. The reference correctly reverts `chg-871282` at minute 16, verifies recovery, and submits the correct report. Grading then crashes while simulating its *no-action counterfactual*, at minute 77, with `ValueError: Fluid network did not converge: residual=4.04461e-05`. The episode framework converts the grading exception to an environment-error zero; the exploit test then compares zero against that invalid zero baseline. This is an environment/grader failure, not an agent reasoning failure. See `runs/revalidation-20261115/seed5-regression.log`; rerun `engine/.venv/bin/python runs/revalidation-20261115/reproduce_seed5.py`.

The broader world audit independently reproduces that numerical failure: **0/100 salted failures; 1/100 unsalted failures**. Every completed audited world has residual below the existing 1e-5 acceptance threshold. The salted audit's maximum residual is approximately 1.0e-7, with up to 1495 iterations. These checks cover the generated no-action trajectories; they do not enumerate every possible intervention.

## Generated incident validity

Three queue-backlog worlds have **exactly zero user-facing loss across the complete evaluation horizon**:

| World distribution | Seed | Service |
|---|---:|---|
| Fresh comparison salt | 31 | pricing-54 |
| Fresh comparison salt | 98 | cart-68 |
| Unsalted | 28 | relay-29 |

That is 2/100 salted worlds and 1 of the 99 completed unsalted worlds. The other unsalted world crashes as described above. These counts describe this audit sample, not the population rate.

For salted seed 31, the purported faulty consumer runs at 30% capacity but still has about **38.19 requests/s** aggregate processing capacity versus only **27–29 requests/s** offered traffic. It never accumulates a queue, errors, or gateway loss. The fixed throttle therefore does not guarantee an actual incident. The generator has no rejection/validation filter for this case; the grader deliberately returns zero impact when its counterfactual loss is zero. The reference's verified-recovery flag on this world should not be interpreted as successful remediation.

## Recommended follow-up

1. Fix convergence through the full counterfactual grading horizon and add the unsalted-seed-5 regression. Preserve numerical acceptance and reward thresholds.
2. Make queue-fault generation depend on offered demand and validate that accepted incidents have measurable baseline loss and recover after the intended intervention. Preserve visibility of invalid-world rejection rates.
3. Rerun the same deterministic checks, bot gates and a fresh matched model batch after either simulator change. Current scores describe the exact hashes recorded above and must not be carried over to changed dynamics.

## Local artifacts

- `runs/revalidation-20261115/results.json`: consolidated machine-readable measurements and provenance.
- `runs/revalidation-20261115/bots/gates.json`: combined 50-seed gate result; individual transcripts are under `bots-*/episodes/`.
- `runs/revalidation-20261115/gemini/`: valid model manifest, summary, timing and transcripts.
- `runs/revalidation-20261115/comparison.json`: existing paired difficulty-gate calculation.
- `runs/revalidation-20261115/bots-replay.json` and `gemini-replay.json`: replay evidence.
- `runs/revalidation-20261115/world-audit.json` and `world-audit-unsalted.json`: complete generation-audit rows.
- `runs/revalidation-20261115/live-server-tests.xml` and `.log`: permitted integration-test rerun.

`runs/` and the private salt file are ignored by Git. The report is a new documentation file; pre-existing implementation changes remain intact.

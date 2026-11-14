# Last Siding: demo and evidence

**Measured:** the capacity trap changes the shortest completion. **Unmeasured:** whether a frontier agent falls for it. The [environment card](../../engine/src/crucible/envs/last_siding/README.md) describes the rules and tools. Run these commands from the repository root.

## Ten-minute demo

```sh
uv run --directory engine python -m crucible.envs.last_siding.tests.demo
```

Pause at the prompt. Each line shows a buffer on the left and an accessible end on the right. The first train must be **AB**, the second **CDE**.

```text
D | E D >  2/3
L | B C >  2/3
R | A   >  1/3
```

Ask the judge to choose **stage** (D→L ×1: place D after C) or **clear** (L→R ×2: move B and C onto A). The command shows both continuations, chosen one first, resets to the same board for each, then repeats with L capacity 4. Use `--choose stage` for a noninteractive presentation. Every displayed step re-executes a stored native tool call and compares its result and final grade.

| First shunt | Tight: remaining shunts | Extra L slot: remaining shunts |
|---|---:|---:|
| D→L ×1 (stage) | 6 | 5 |
| D→L ×2 | illegal | 7 |
| D→R ×1 or ×2 | 6 | 6 |
| L→D ×1 | 6 | 6 |
| L→R ×1 | 7 | 7 |
| L→R ×2 (clear) | 5 | 5 |
| R→D ×1 or R→L ×1 | 7 | 7 |

Both initial distances are 6. At [tight/stage, step 1](../../engine/src/crucible/envs/last_siding/tests/evidence/tight-stage.jsonl), C-D are correctly ordered on L, but B cannot leave until the staged wagons move off it. L is now full. E cannot join C-D there; the saved continuation parks E on R and later moves C-D off L at step 4. Staging costs **1 + 6 − 6 = 1 extra shunt** (score 0.971429). [Clear](../../engine/src/crucible/envs/last_siding/tests/evidence/tight-clear.jsonl) costs 1 + 5 = 6, score 1. With [one extra slot](../../engine/src/crucible/envs/last_siding/tests/evidence/control-stage.jsonl), step 2 parks E on L and step 4 lifts C-D-E together, exposing B; both branches now score 1. This is a necessary change of **placement** on the stage branch. It need not break the C-D coupling or reverse the exact last move.

## Local results

One deterministic runner episode per policy per unsalted seed 0–299, repeat 0; 900 episodes in total. No error or harness budget exhaustion. [Every episode's score, dispatch count and shunt count](local-scores.csv); [release identity, prompt and settings](experiment.json).

| Policy | Both dispatched | Mean score | Mean actual shunts | Mean excess over oracle, completed only |
|---|---:|---:|---:|---:|
| Exact calibration ceiling | 300/300 feasible | 1.000000 | 6.863 | 0 |
| Public reference | 300/300 | 0.970393 | 8.133 | +1.270 |
| Stop after first dispatch | 0/300 | 0.100000 | 5.027 | N/A |
| Do nothing | 0/300 | 0.000000 | 0 | N/A |

Reference ties the oracle on **60/300**, loses on 240, wins on 0. The ceiling row is exact search, not an extra runner policy. The bank has 170 distinct puzzle layouts and repeats across epochs. On unsalted seeds **0–24** alone, the reference completed 25/25, mean score 0.967870, mean excess +1.400 (4 oracle ties). Neither result is a paired comparison with a model.

One actual reference mistake: [seed 0, step 3](reference-seed-0.jsonl). Before the shunt, D=AEF, L empty, R=CBD, with ABC then DEF required. Removing EF from D exposes A, but the exact distance stays at 5. Paying one shunt to go from distance 5 to 5 costs +1. Every other call on this trajectory costs +0; it finishes in 8 against 7, score 0.975. Verify this attribution:

```sh
uv run --directory engine python -m crucible.envs.last_siding.tests.demo --replay ../docs/last-siding/reference-seed-0.jsonl
```

To reproduce in a fresh output directory (the native executor resumes existing transcripts):

```sh
uv run --directory engine crucible tasks
uv run --directory engine crucible eval --task last_siding@1 --policy reference,do_nothing,stop_early --seeds 0-299 --out ../runs/last-siding-reproduction
uv run --directory engine pytest -q
```

Final verification: **559 passed, 4 skipped** in the full engine suite. The skips are generic checks for absent optional policies. Live MCP reference and stop-early episodes on three seeds matched local scores, step counts and stop reasons. Ruff and strict Pyright passed on the gym and judge driver. The generic, policy boundary, golden-score, calibration, integrity and JSONL replay checks are included. The gym tests rebuild the entire canonical bank offline and match its contents. Fresh resets only index the saved bank. Five and six wagons keep exhaustive search practical.

## Live judging

```sh
uv run --directory engine python scripts/last_siding_judge.py
```

Requires Docker running Linux containers, Harbor **0.26.0** on PATH, and `GOOGLE_GENERATIVE_AI_API_KEY` with access to **google/gemini-3.8-flash**. Uses the repo's OpenCode **1.19.5** setup, web search disabled, 600-second agent timeout, provider sampling defaults. The task prompt is verbatim in [experiment.json](experiment.json). The driver runs Harbor oracle playing the **reference**, three independent OpenCode trials (repeat labels 0, 1, 2), Harbor nop, then Harbor oracle playing **stop_early**. The latter uses a byte-for-byte copy of the original sidecar deployment, changing only solution selection and its manifest. All see the same salted seed-0 world. Do not compare it to unsalted seed 0.

**Verified here:** real wheel build and Harbor export, script packaging without calibration tests, generation and fingerprinting of both matched datasets and all six command lines (`--prepare`), and the live command's prerequisite gate. **Not executed:** Docker/Harbor trials or the model provider. This host lacked the runtime and provider key. There are no frontier completions, wins, ties, losses or excess-shunt measurements to report. The gate does not replace missing trials with zero scores.

The driver writes the frozen prompt, settings, commit, wheel hash and commands to private `harbor/last-siding-judge/plan.json`. It prints the verified dispatch count, score, excess shunts and trace directory for each completed trial, then the frontier wins/ties/losses. Keep the exported tree private and unchanged across agents. Re-exporting creates new salts. Raw Harbor grades and transcripts stay under `jobs/last-siding-judge/`. A provider or verifier failure is an incomplete experiment; preserve the logs.

For a standalone export:

```sh
uv run --directory engine crucible export harbor --task last_siding@1 --seeds 0 --out ../harbor/last-siding-export
```

## Interpreting a future experiment

The six-trial demonstration tests repeatability on **one** world. It cannot establish a broader gap. Neither demo fixture is drawn by the evaluation generator. For a broader claim, freeze a fresh 25-world Harbor dataset, wheel hash, agent configuration and verifier records **before** running any frontier trials. Use [the paired protocol](../gap-proof.md): primary pair reference minus the named frontier agent, seeds 0–24, five ordered batches of five, three repeats per agent/seed, no hard subset, no stopping early. Retry infrastructure failures once on the identical world; retain incomplete status if unresolved. Compare three real reference runs per seed to three real frontier runs; do not invent repeats or pair unrelated salts. The 300 unsalted baseline seeds are a separate descriptive report.

After converting verified Harbor trial data to native CSV columns `task,policy,seed,repeat,total,stop_reason`, run:

```sh
uv run --directory engine python -m crucible.gap_proof --runs ../runs/last-siding-paired/summary.csv --task last_siding@1 --agents reference,frontier --seeds 0-24
```

Average repeats per seed. Require both a positive lower 95% bootstrap bound (10,000 paired resamples, RNG 21) and exact two-sided sign-test p < 0.05 before asserting the planned gap. Report all wins, losses and ties regardless of direction. For any behavioral explanation, quote the actual turn and compute shunt cost plus the change in exact distance from that board. A timeout, invalid call or failed verifier alone is not evidence that a model misunderstood the siding.


To run Gemma 4 with a Google AI Studio key, use PowerShell 7 from the repository root:

```powershell
$env:GOOGLE_GENERATIVE_AI_API_KEY = Read-Host "Google AI Studio API key" -MaskInput
$env:LAST_SIDING_MODEL = "google/gemma-4-31b-it"
uv run --directory engine python scripts/last_siding_judge.py
```

The driver records the selected model and writes Gemma trials to a separate job directory. Live Gemma performance is unmeasured.

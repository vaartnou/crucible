# AfterQuery submission check

## Current separate-verifier package

The current [submission](../submissions/oncall-v2/README.md) is
`oncall-v2-separate-7b0512f.tar.gz`, frozen from the committed separate-verifier
implementation. Full default Harbor lint passes without exceptions. A fresh
Gemini Flash/OpenCode run scores `0.644313711357698`, with correct diagnoses,
verified recovery, 60 calls, and zero Harbor exceptions. Native Harbor regrading
against the exact frozen verifier reproduces that score. The evidence is in
`submission-evidence-separate.zip`; [verification details](harbor-separate-verifier.md)
cover collection, isolation checks, and error handling.

The root README explains the task, difficulty, scoring, project setup, bot
evaluations, workbench, Harbor model runs, and development checks. Both task
archives intentionally publish their demo credentials and world salt; provider
API credentials are excluded. Use a fresh private export for held-out evaluation.

## Historical shared-verifier package

Checked on 15 November 2026 against main at `011cfa1` (PR #56).

The submission environment is `oncall@2`: diagnose and repair overlapping service
regressions with public tools under a simulated time and call budget. The root README
explains the task, its difficulty, and scoring. The [model comparison](oncall-model-comparison.md)
records forty Harbor/OpenCode trials; the [scenario guide](oncall-overlap.md) includes
the demo screenshot and reproduction commands.

The generated submission export is `harbor/submission-oncall-v2`. Its seed-0 task contains:

```text
oncall-2-seed-0/
  instruction.md
  task.toml
  environment/Dockerfile
  environment/docker-compose.yaml
  environment/mcp-server/  # Dockerfile, engine wheel, locked requirements
  tests/test.sh
  solution/solve.sh
```

Run `sh harbor/submission-oncall-v2/build-sidecar.sh` before Harbor. The Compose
sidecar uses the image built by that script. Deliver the entire export, including
the vendored wheel, requirements, and build script, to the evaluator. A frozen
submission copy and scored run evidence are now included under
[`submissions/oncall-v2/`](../submissions/oncall-v2/README.md). That copy intentionally
publishes this demonstration world's salt and task-specific tokens; use a fresh
export for held-out evaluation. Provider API credentials are excluded.

The verifier writes one finite number in [0, 1] to `/logs/verifier/reward.txt`.
Partial credit is supported. Breakdown and transcript files are separate diagnostics.
Historical PR #56 exports used `reward.json`; the fresh export uses the corrected
scalar format. Include a fresh run's `verifier/reward.txt`, `grade.json`,
`transcript.json`, agent trajectory, and trial result with the submission.

Validation on this checkout:

- 69 Harbor/export, verifier, MCP, grading, launcher, and overlap tests passed,
  plus seven subtests.
- Harbor 0.26.0 structural/configuration lint passed. Default lint flags
  `shared-verifier`: verification runs in the agent sandbox and reads the live
  simulator sidecar. All other default lint rules pass. This is a remaining
  isolation limitation, not a missing task-format field. Switching to a fresh
  sidecar for verification requires preserving the played episode state.
- The Docker sidecar image built successfully. A fresh Harbor oracle trial
  completed without exceptions and wrote `reward.txt` = `0.8261707424624951`.
- On the screenshot's original private seed-0 world, `incident_runbook` scored
  `0.9004757202439811`; do-nothing scored zero. Both rule transcripts replayed exactly.
- A fresh Gemini 3.8 Flash/OpenCode 1.19.5 run on that world scored
  `0.714490883784987`, with verified recovery, complete diagnosis, zero penalties,
  54 calls, and zero Harbor exceptions. Runtime including setup was 4m 29s.
  Reward replay is exact and observation replay passes the explicit `1e-12`
  Linux/macOS float tolerance. The runbook comparison checks simulator source
  and the private world identity. A new stochastic model playthrough need not
  reproduce the original screenshot's model actions or score.

Fresh local artifacts:

- Private task export: `harbor/submission-oncall-v2/`.
- Full model job: `runs/harbor-jobs/submission-oncall-v2-flash-20261115/`.
- Comparison/replay report: `runs/submission-oncall-v2-comparison.json`.
- Submission evidence: `runs/submission-evidence/` and `runs/submission-evidence.zip`.
  These include the scalar reward, grade, public-tool transcript, OpenCode trajectory,
  and a summary. They omit task configuration and credentials.

Attach the private task export and the evidence archive to the submission. The
generated README in the export root includes build/run commands. The named frozen
task archive and evidence ZIP are tracked in `submissions/oncall-v2/`; working
exports and raw job directories remain ignored.

The original screenshot model trial scored `0.7335710961165985` with no exception.
Its raw run artifacts remain in the original local measurement checkout; committed
measurement JSON is a summary and does not replace a run-log attachment.

The challenge does not require a particular minimum model score or a passed internal
difficulty gate. The measured limitations of the scenario family remain in the model
comparison report. A demo playthrough is encouraged. The requested submission
destination is this repository; no external submission portal has been used.

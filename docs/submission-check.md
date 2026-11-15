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
evaluations, workbench, Harbor model runs, and development checks. The task
archive intentionally publishes its demo credentials and world salt; provider
API credentials are excluded. Use a fresh private export for held-out evaluation.


Validation includes 802 passing local engine tests (25 skipped and three expected
failures), passing engine/web/Docker CI, exact oracle reward parity, a zero no-action
reward, agent-file tampering checks, timeout evidence collection, and explicit rejection
of corrupted evidence. The extracted evidence ZIP supports native Harbor regrading.

The requested submission destination is this repository; no external submission
portal has been used.

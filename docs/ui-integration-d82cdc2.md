# Harbor UI integration — 15 November 2026

Integrated commit `d82cdc27742cd052f494d656cb6b79cc14e7d9d1` by fast-forwarding `codex/oncall-gym`, including its UI parent `6421bdd`. The OpenAI-compatible direct model runner remains removed. Model evaluations use Harbor and OpenCode; manual play and built-in strategy comparisons remain available.

All pre-existing simulator changes and bug fixes were preserved byte-for-byte. Their environment and full package hashes match the previous successful bug-fix validation:

- Environment: `6e33d26d8ba096d84033e57bd6a04776c345cad2c3f7c97924757a0f479ef8c1`.
- Full simulator/policy package: `b817db2634f3d3c0103f7108f65c2c1f49106132c61570eda118031e3903affd`.

## Compatibility fixes

- The service graph now lays out the actual DAG, including deeper layers and shared dependencies, without assigning coordinates from service-name suffixes or overwriting shared nodes.
- Agent-view discovery accepts `changes list SERVICE`. Later cluster-wide change headers preserve previously discovered scoped diffs. Header-only changes have an explicit undiscovered-diff label rather than blank or undefined summaries.
- Full-cluster and observed-edge call paths include secondary branches. Every path has a unique selection ID, and the selector scrolls within a bounded height so the waterfall remains visible.
- Saved replay verifies all recorded observations before reconstructing a selected prefix. Scrubbing an incompatible recording to step zero cannot falsely validate a different world. Incompatible recordings remain readable from recorded agent observations, with a clear status label; empty recordings do not claim a verified world.

The integration fixes build on the requested commit. This report records validation completed before commit and push.

## Validation

- **223 backend tests passed**, zero failures/skips: server, Harbor launch configuration, workbench discovery/replay, core lifecycle, feedback, CLI, smoke and export tests. Evidence: `runs/integration-d82cdc2/backend.xml` and `backend.log`.
- **6 frontend regression tests passed**: deep/shared DAG placement, card separation, shuffled service order, partial/cyclic observations and full observed-edge path coverage.
- `npm run build` passes TypeScript checking and Vite production compilation.
- Ruff lint, formatting and strict Pyright pass. `git diff --check` passes.
- Browser checks passed on the production build: Harbor setup and prepared exports; manual topology discovery; scoped change discovery followed by global refresh; all 23 full-cluster services; secondary path selection; baseline comparison; listing 17 existing Harbor trials; replay scrubbing with incompatible-world status retained. No browser console errors/warnings were observed.
- Screenshot: `runs/integration-d82cdc2/ui-final.jpg`. Consolidated evidence: `runs/integration-d82cdc2/results.json`.

No new paid model trial was launched during this integration check. Harbor command construction and persistent job handling are covered by launcher tests; browser replay used existing Harbor jobs. Historical transcripts that differ from the revised simulator correctly remain in recorded agent view.

## Ongoing checks

The GitHub Actions workflow now triggers for `web/**` changes and runs a Node 24 frontend job with `npm ci`, `npm test` and `npm run build`. The Docker job requires both backend and frontend jobs to pass. The UI setup instructions include the frontend test command.

The local validation server is available at [127.0.0.1:8765](http://127.0.0.1:8765). It reads existing Harbor outputs from the configured jobs directory and writes manual test sessions separately under `runs/integration-d82cdc2/sessions`.

## Merge compatibility with main

Merged `cea3499` from main while preserving SupplyGym/Cala registrations, pretrained policies, golden scores, and spectator support alongside oncall. Retired environment registrations and the removed direct model runner remain absent. Sidecar cleanup retains error-aware transcript finalization and spectator finalization.

Post-merge validation: **297 core/API/UI/spectator/golden tests passed** and **213 SupplyGym/environment-generic tests passed**, with 23 skips and 3 expected failures inherited from that test set. Seven unchanged oncall generic cases were deselected from the SupplyGym run; the oncall simulator/package hashes match the prior full validation. All six frontend tests, production build, Ruff, formatting, and strict Pyright pass. Detailed local evidence is under `runs/merge-main-pr50/`.

# Fleetbase backend implementation plan

## Scope and baseline

Start from `origin/main` (`59b15f2`) on `codex/fleetbase-backend`. Do not copy,
register, or maintain the former `dispatch@1` or `dispatch@2` environments.
Existing main environments (`guess`, `incident`) retain their behavior. Fleetbase
will be a separate `fleetbase_dispatch@1` environment. The supplied brief's
preserve-dispatch requirement is superseded by this scope decision.

Implementation is delegated in ordered change sets. Each change set must have
reviewable tests and an explicit status. External proofs and measurements cannot
be replaced by mocked tests or invented results.

## Ordered change sets

1. **Resource lifecycle.** Add default `Task.aclose()`. Stop actions before grading,
   cache grades, close transcripts, release episode resources on normal finish,
   failed reset, exceptions, cancellation, expiration, and server shutdown. Record
   cleanup failures without masking primary failures. Verify existing scoring.
2. **Reusable backend runtime.** Private connection configuration, readiness,
   episode leases, artifacts, idempotent release. Separate attached deployments
   from managed service ownership. Require exclusive access or proven isolation;
   reject overlapping leases before mutations. Use a small independent HTTP
   service for contract tests, with no logistics logic in core.
3. **Deployment bundles.** Optional environment-owned services/files/configuration,
   strict composition and path validation, reserved Crucible service names,
   fingerprints, no host ports/container names/shared storage. Keep the existing
   exporter identical for ordinary environments. Harbor owns services.
4. **External-service proof.** A fresh minimal FastAPI fixture (not the former
   dispatch environments) proves readiness, initialization, state access,
   artifact collection, isolation and teardown locally and in export. Pin the
   Harbor version used for actual validation. Record unavailable infrastructure.
5. **Fleetbase native audit and one-delivery proof.** Pin upstream source/images,
   inspect native APIs and permissions, deployment dependencies and jobs. Implement
   request-to-native-ID mapping, safe mutation read-back, native order creation,
   assignment and dispatch, private progress synchronization, and a physically
   verified delivery. Do not claim real API support based only on mocks.
6. **Determinism and grading integrity.** Explicit virtual time; reconcile,
   simulate, synchronize, settle. Trusted physical events determine economics.
   Frozen evidence and idempotent synchronization. Independent reset/replay,
   duplicate-demand protection, cross-episode isolation, worker audit.
7. **Complete workflow and reference.** Three vehicles/drivers, twelve requests in
   two waves, 180-minute horizon. Same public tools for competent reference,
   greedy, no-op, stop-early and model. Calibrate native call budget. Preserve
   utility = realized value - actual driving cost and clipped normalized reward.
8. **Paired pilot and findings.** Freeze candidate/configuration first. Three fresh
   worlds, three model repeats each; average repeats within each world. Include
   failures/retries and all required economic/service/workflow outcomes. Publish
   one of the brief's three conclusions, including inconclusive when appropriate.
9. **Conditional fresh-world validation.** Only if justified by a valid pilot,
   predeclare at least 30 new worlds, paired uncertainty and distributions. A
   changed candidate gets a new experiment identity and fresh worlds.

## Validation gates

For each code change: focused tests, Ruff formatting/lint and strict Pyright;
then the repository test suite after integration. Infrastructure acceptance
requires real clean local and pinned-Harbor runs. Pilot acceptance requires actual
model trials with recorded model settings; missing credentials or services are
reported as incomplete, never as successful benchmark evidence.

## Progress

- Baseline: created fresh branch from `origin/main`; clean worktree, no dispatch
  environments on baseline.
- Steps 1–3: implemented and reviewed. Lifecycle, real HTTP leases/artifacts,
  private bundles and unchanged ordinary exports verified; review also corrected
  cleanup-error masking and literal-dollar handling in Compose configuration.
- Step 4: new `backend_probe@1` passed separate-process local and Harbor 0.26.0
  reference/no-op runs (rewards 0.9/0); episode resources and containers removed.
- Step 5: native Fleetbase clean-stack one-delivery and full twelve-delivery
  workflows passed locally and in a self-contained Harbor export.
- Step 6: native resource counts return to the empty active baseline after every
  local episode; independent reset/replay produces exactly equal semantic events,
  world and economics. A failed API-throttle attempt remains recorded separately.
- Step 7: three-vehicle, twelve-request world and full public-tool reference,
  greedy, no-op and stop-early verified. Development reference uses 50 calls,
  with 128 available to all operators. No old dispatch code was copied.
- Step 8: candidate `fleetbase-native-v1-pilot-b1718743d43d` frozen after clean
  validation. The user stopped evaluation after twelve valid scripted trials
  and three valid model repeats on world 1. The active world-2 model trial was
  cancelled; five remaining model trials were not started. The findings report
  selects Evaluation inconclusive because only one paired world is complete.
  No parallel runner changes were implemented before the stop request.
- Step 9: conditional; predeclare fresh-world validation only if the pilot
  justifies it, using a new experiment identity and at least 30 fresh worlds.

Last full code checks: Ruff lint/format and strict Pyright clean; 595 tests passed,
17 generic external-backend checks explicitly skipped in favor of real deployment
integration checks. Main revision verified against the live remote before pilot.

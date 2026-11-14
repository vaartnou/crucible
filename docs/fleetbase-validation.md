# Fleetbase infrastructure validation

This document records implementation checks, not model benchmark results. A
successful scripted operator does not establish an agent-performance gap.

## Reproduction

Use the locked engine dependencies, Docker Compose, and Harbor **0.26.0**.
The exporter packages pinned application images, bootstrap files and private
credentials. Generated exports and run directories are gitignored and private.

```sh
cd engine
UV_CACHE_DIR=/tmp/crucible-uv-cache uv sync --locked --python 3.14
UV_CACHE_DIR=/tmp/crucible-uv-cache .venv/bin/crucible export harbor \
  --task fleetbase_dispatch@1 --seeds 0 --out ../harbor/fleetbase-dev
.venv/bin/python scripts/fleetbase_smoke.py \
  --task-dir ../harbor/fleetbase-dev/fleetbase_dispatch-1-seed-0 \
  --out ../runs/fleetbase-validation-UNIQUE --harbor
```

Use a new output directory for every attempt. The smoke runner refuses to
overwrite evidence. It generates a separate Compose project with no host ports
or shared volumes, starts application dependencies, runs the local Crucible CLI
sequentially for reference, greedy, do-nothing and stop-early, and replays the
reference after an independent cleanup/reset. Semantic events, world and economics
must match while incidental native record identifiers may differ. It then runs
a separate clean Harbor oracle task. Its finally block removes only its own
Compose project's services and volumes.

The stop-early baseline creates, assigns and dispatches one order, then invokes
`finish`; the published continuation advances committed work through the horizon.
Do-nothing creates no orders and earns zero. Reference route planning includes
every terminal delivery when testing route feasibility and uses only public tool
results. The isolated policy/simulator fixtures are unit tests, not substitutes
for the native-service proof.

## Results

The v1 (`fleetbase-physical-economics-v1`) attempts, local baseline numbers and
Harbor proof are archived in
[archive/fleetbase-validation-v1.md](archive/fleetbase-validation-v1.md). They are
superseded by the v2 economics (`fleetbase-physical-economics-v2`).

Native source/API audit and pinned versions are documented separately in
[fleetbase-api-audit.md](fleetbase-api-audit.md). Final native cleanup checks count
active business resources, including payload/tracking children, rather than
assuming that deleting an order removes every dependent record. Native
soft-deleted audit rows may remain; active cross-episode state must not.

## Current acceptance status

The pure simulator/policy checks pass, including physical motion, economic
accounting, late/unserved demand, capacity, irreversible pickup, reassignment
before pickup, duplicate protection and semantic replay. The reusable runtime
passes real HTTP subprocess tests for preparation, leases, artifacts and cleanup.
Local native baselines, independent-reset semantic replay, and clean Harbor
acceptance all pass.
No model-performance conclusion is available from these checks.

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

## Attempts

| Attempt | Outcome | Evidence and action |
|---|---|---|
| 1 | Invalid infrastructure result | Clean build and database/bootstrap readiness passed. The first local reference exceeded Fleetbase's default wall-clock HTTP throttle, causing rejected operations, grade failure and cleanup failures. The zero `env_error` result is excluded from economics. All owned services/volumes were torn down. Logs and transcript are retained in `runs/fleetbase-validation-attempt-1`. The dedicated offline deployment must disable the audited throttle before retrying. |
| 2 | Passed local, independent replay, and clean Harbor | Supported native rate-limit settings disabled in the dedicated offline installation. A fresh image/export and empty database were used. All four local CLI baselines completed and the reference reproduced identical frozen world, semantic events and economics after an independent reset; after every episode, active native business rows (including payload/tracking children) were zero and only the bootstrap user remained. A separately started self-contained Harbor 0.26.0 oracle also completed with trusted evidence and positive reward. Both the local runner and Harbor tore down their own services and storage. Evidence: `runs/fleetbase-validation-attempt-2`. |

## Verified local development scenario

Unsalted development seed 0, identical generated conditions for all four scripted
operators; 840 total generated request value. These are real native Fleetbase
runs, not agent model measurements.

| Operator | Calls | Reward | Raw utility | On-time deliveries | Distance |
|---|---:|---:|---:|---:|---:|
| Reference | 50 | 0.950194 | 798.162808 | 12 | 209.185960 |
| Greedy | 50 | 0.945219 | 793.983689 | 12 | 230.081555 |
| Do-nothing | 0 | 0 | 0 | 0 | 0 |
| Stop-early | 4 | 0.029951 | 25.158689 | 1 | 24.206556 |

Every valid local baseline had zero rejected operations and duplicate orders.
Reference and greedy had zero late or unserved requests. Stop-early left eleven
unserved, and do-nothing left twelve. Native completion records were synchronized
only after trusted pickup/delivery events. Their economic results exactly match
the separate public-workflow simulator checks for this development world.

## Clean Harbor proof

Harbor 0.26.0 ran the generated task with a separate application/database/cache
installation and the oracle operator. The task's private salt makes its world
different from the unsalted local seed 0 above; their scores are not a paired
comparison. Docker 29.9.3 and Compose 5.7.0 were used.

| Measure | Verified value |
|---|---:|
| Reward | 0.9607588085931846 |
| Raw utility | 827.213334198732 |
| Generated / realized value | 861 / 861 |
| Actual driving cost | 33.78666580126801 |
| On-time deliveries | 12 / 12 |
| Late / unserved / rejected / duplicate | 0 / 0 / 0 / 0 |
| Distance | 168.93332900634002 |
| MCP calls | 51 |
| Harbor wall time | 129.612 seconds |

The verifier saved `grade.json`, `transcript.json`, and `evidence.json`; evidence
contains the full frozen world and 36 semantic release/pickup/delivery events.
Harbor reported zero errored trials, and the trial ended `task_done` without
exhausting its call budget. The runner then removed its independent local proof
stack. No services from the proof remain.

The tested engine wheel SHA256 is
`2a2ebfab4681e6f53dc5f10668c6051309cffe1df4950a1431f259c9434e68cf`;
the export manifest preserves wheel, source and deployment asset fingerprints.
Subsequent pilot freezes must build their own recorded candidate artifact. These
infrastructure/development results must not be counted as fresh evaluation worlds.

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

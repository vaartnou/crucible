# Fleetbase infrastructure validation: v1 results

> **Archived.** Superseded by the v2 economics (`fleetbase-physical-economics-v2`).
> These v1 (`fleetbase-physical-economics-v1`) attempts and numbers are kept for history only;
> do not compare them with v2 scores. The validation process is described in
> [fleetbase-validation.md](../fleetbase-validation.md).

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

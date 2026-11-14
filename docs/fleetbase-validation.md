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

## v2 heuristic sweep

Plan step 4 tuned and froze the `WorldConfig` defaults with scripted policies on the
in-memory backend (`fleetbase_sim@1`). These are policy results, not agent results.
Reward is `clip(U / U_hindsight, 0, 1)`; `U_hindsight` is the executed utility of the
reference's planner with the whole world known at t=0 (`hindsight.py`).

Command (50 dev seeds 0–49, all policies, defaults, budget 530):

```sh
cd engine
uv run python scripts/fleetbase_sweep.py --seeds 50 --json ../runs/fleetbase-sweep.json
```

Frozen defaults (`world.py`): vehicles=6, capacity 2–8, cost per distance 0.15–0.45,
shift_end_min=150, 40–50 requests in 6 waves, horizon=240, speed=1,
demand_capacity_ratio=1.4, value_min=10, value_spread=8, 3 urgent requests (×3 value,
slack 20), **ready_delay_max=25, deadline_slack 25–80** (were 15 and 20–60),
late_window=30, cancellation_fee=8, forecast_grid=3, breakdown_rate=0.35 (20–45 min),
cancellation_rate=0.08, dropoff_change_rate=0.08, traffic_rate=0.4 (×0.5 for 45 min),
silent_share=1.0. Budget `MAX_STEPS=530`.

| Policy | Mean | SD |
|---|---:|---:|
| do_nothing | 0.000 | 0.000 |
| stop_early | 0.028 | 0.019 |
| fifo | 0.140 | 0.111 |
| greedy_no_replan | 0.332 | 0.121 |
| greedy_nearest | 0.393 | 0.118 |
| reference | 0.807 | 0.091 |
| one_vehicle | 0.006 | 0.012 |
| create_then_cancel | 0.000 | 0.000 |
| reposition_spam | 0.000 | 0.000 |
| estimate_spam | 0.000 | 0.000 |
| finish_immediately | 0.000 | 0.000 |
| assign_broken_vehicle | 0.003 | 0.011 |
| reroute_after_pickup | 0.006 | 0.012 |

| Gate | Target | Value | Status |
|---|---|---:|---|
| do_nothing | 0 | 0.000 | PASS |
| greedy | 0.35–0.55 | 0.393 | PASS |
| reference | 0.75–0.85 | 0.807 | PASS |
| reference − greedy | ≥ 0.25 | +0.413 | PASS |
| greedy − greedy_no_replan (disruptions on) | ≥ 0.05 | +0.061 | PASS |
| max exploit < greedy | < 0.393 | 0.006 (one_vehicle) | PASS |
| no grader errors | none | none | PASS |

Budget: the reference uses 317 calls on average (max 382), 60 % of 530. The reference
reached or beat `U_hindsight` on 0 of 50 seeds (highest reward 0.943).

Sensitivity (same 50 seeds; knobs of `fleetbase_sweep.py`: `ratio` sets
`demand_capacity_ratio`, `disruption` multiplies the breakdown, cancellation, dropoff
change and traffic rates, `tightness` multiplies the deadline slack, `silent` sets
`silent_share`; "≥H" counts seeds where the reference reached `U_hindsight`):

| Config | fifo | greedy_no_replan | greedy | reference | ref − greedy | greedy − no_replan | ≥H | ref calls | Failing gates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ratio=1.2, disruption=0 | 0.316 | 0.557 | 0.557 | 0.916 | +0.360 | +0.000 | 1 | 323 | greedy, reference |
| ratio=1.2, disruption=1 | 0.235 | 0.429 | 0.497 | 0.828 | +0.331 | +0.068 | 0 | 335 | none |
| ratio=1.2, disruption=2 | 0.196 | 0.336 | 0.430 | 0.762 | +0.332 | +0.093 | 2 | 342 | none |
| ratio=1.4, disruption=0 | 0.193 | 0.453 | 0.453 | 0.891 | +0.439 | +0.000 | 1 | 306 | reference |
| **ratio=1.4, disruption=1** | 0.140 | 0.332 | 0.393 | 0.807 | +0.413 | +0.061 | 0 | 317 | none |
| ratio=1.4, disruption=2 | 0.108 | 0.260 | 0.361 | 0.713 | +0.352 | +0.100 | 0 | 327 | reference |
| ratio=1.6, disruption=0 | 0.115 | 0.389 | 0.389 | 0.864 | +0.475 | +0.000 | 1 | 293 | reference |
| ratio=1.6, disruption=1 | 0.073 | 0.268 | 0.318 | 0.753 | +0.435 | +0.050 | 2 | 300 | greedy |
| ratio=1.6, disruption=2 | 0.062 | 0.196 | 0.288 | 0.688 | +0.400 | +0.093 | 1 | 311 | greedy, reference |
| tightness=0.8 | 0.108 | 0.305 | 0.369 | 0.803 | +0.434 | +0.064 | 0 | 312 | none |
| tightness=1.25 | 0.163 | 0.362 | 0.415 | 0.795 | +0.380 | +0.053 | 1 | 320 | none |
| silent=0 | 0.140 | 0.332 | 0.393 | 0.809 | +0.416 | +0.061 | 0 | 321 | none |

Findings:

- The reference − greedy gap comes from choosing what to serve (value, deadlines,
  cost), pooling several orders per route, and replanning on releases and breakdowns.
  The reference − hindsight gap is the value of knowing the future: the hindsight
  plan is at the pickup when an order becomes ready and keeps trucks free for later,
  better orders.
- `greedy − greedy_no_replan` comes from breakdowns and traffic (the open-loop greedy
  keeps loading broken or slowed vehicles and never re-serves unassigned orders). It
  is 0 without disruptions, as it should be.
- `silent_share` barely matters (0.809 vs 0.807 for the reference, no change for
  greedy): the physics already drops cancelled stops and drives to the new dropoff,
  so reading silent changes only improves the reference's planning slightly.
- `ready_delay_max` and the deadline slack were raised because with the old values
  (15, 20–60) greedy scored 0.344 (below the 0.35 gate; reference 0.777,
  greedy − no_replan +0.055). More lead time per order mostly helps the
  online policies, which learn of an order only at its release.

## Current acceptance status

The pure simulator/policy checks pass, including physical motion, economic
accounting, late/unserved demand, capacity, irreversible pickup, reassignment
before pickup, duplicate protection and semantic replay. The reusable runtime
passes real HTTP subprocess tests for preparation, leases, artifacts and cleanup.
Local native baselines, independent-reset semantic replay, and clean Harbor
acceptance all pass.
No model-performance conclusion is available from these checks.

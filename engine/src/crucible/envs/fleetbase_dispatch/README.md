# Fleetbase Dispatch v1

`fleetbase_dispatch@1` is a new environment built from main. It does not import or
register the previous dispatch environments. `guess@1` and `incident@1` retain
their scoring and historical golden results.

The operator creates native Fleetbase orders, assigns matching drivers and
vehicles, dispatches them, and advances explicit virtual time. A private bridge
reads native commitments, simulates pickup and travel, then applies verified
progress through Fleetbase's native activity flow. Native `completed` status
cannot earn delivery value by itself.

## Rules

- Three vehicles have capacities 2, 4 and 6, with matching drivers.
- Twelve requests arrive in two waves at minutes 0 and 60. The horizon is 180.
- Public observations show released demand, vehicle positions, assignment,
  dispatch, and physical progress. Only the next release time is disclosed.
- Each vehicle serves one order at a time. Capacity must cover the request load.
  Assignment can change before pickup; picked-up cargo cannot transfer vehicles.
- Physical travel uses straight-line scenario coordinates, one distance unit per
  minute. Fleetbase map coordinates are a display mapping of that geometry.
  Native distance estimates do not determine physical costs or travel time.
- Tool calls do not advance time. `advance(minutes)` reconciles native records,
  applies legal commitments, moves vehicles, and synchronizes physical progress.
- Delivery by the deadline earns the request value. Late delivery earns zero;
  all actual driving costs 0.2 per distance unit.
- `utility = realized delivery value - actual driving cost`.
  `reward = clip(utility / total generated request value, 0, 1)`.
- `finish`, policy stop, and budget exhaustion continue already dispatched work
  to the horizon. They do not create or dispatch new work. Future unserved demand
  remains in the reward denominator. Workflow rejections have no added penalty.
- The budget is 128 calls: the native workflow requires three mutations per
  request, plus observation, clock management and recovery. Development reference
  tests verify completion within 100 calls; actual clean-stack calibration is
  recorded separately before any model comparison.

Public tools are `observe`, `create_order(request_id)`,
`assign_order(request_id, vehicle_id)`, `dispatch_order(request_id)`,
`advance(minutes)`, and `finish`. Successful results include the complete public
observation. The agent cannot submit private physical events or completion proof.

## Deployment

See [the native audit](../../../../../docs/fleetbase-api-audit.md) for immutable
image/source pins and audited endpoints. The private bundle starts MySQL, Redis,
and the API. It has no host ports, host mounts or persistent volumes. It uses
synchronous jobs, disables the scheduler, automatic reallocation, outbound
notifications and native wall-clock throttling. Both operators use this same
configuration. Application mutations and progress use native APIs; private
installation bootstrap creates the company, permissions and credential.

From the repository root:

```bash
uv run --directory engine crucible export harbor --task fleetbase_dispatch@1 \
  --seeds 0 --out ../harbor/fleetbase-dev
uv run --directory engine python scripts/fleetbase_smoke.py \
  --task-dir ../harbor/fleetbase-dev/fleetbase_dispatch-1-seed-0 \
  --out ../runs/fleetbase-dev-proof --harbor
```

The smoke runner owns the Compose stack and destroys it after collecting logs.
The episode runtime only acquires a lease, connects, seeds owned native resources,
collects evidence, and releases its resources. For an attached isolated test
installation, provide private `FLEETBASE_URL`, `FLEETBASE_TOKEN`,
`FLEETBASE_DEPLOYMENT_ID`, and explicitly set `FLEETBASE_EXCLUSIVE=1`. All
cooperating clients must share `FLEETBASE_LOCK_DIR` and deployment identity. Do
not attach to a working installation with other clients. Evaluation concurrency
must be 1. `FLEETBASE_ARTIFACT_DIR` optionally retains private frozen evidence.

The reference uses only public tools and observations. It plans feasible routes
with complete pickup and terminal delivery legs; greedy uses the nearest feasible
request. `do_nothing` stops immediately; `stop_early` stops after the first
committed dispatch. The same continuation rules apply to every operator.

## Validation and comparison

Offline simulator/adapter/task tests check trust, deterministic physics,
read-back after ambiguous mutations, duplicates and frozen evidence. Generic
offline environment tests opt out because they cannot provision the application;
they are supplemented by real HTTP, clean local and Harbor proofs rather than
mocked benchmark scores. See [validation](../../../../../docs/fleetbase-validation.md).

After clean reference validation, freeze a candidate and three fresh paired
worlds. Preserve private exports and run records in ignored folders:

```bash
uv run --directory engine python scripts/fleetbase_pilot.py freeze \
  --out ../runs/fleetbase-pilot-v1 --reference-verified
uv run --directory engine python scripts/fleetbase_pilot.py run \
  --out ../runs/fleetbase-pilot-v1 --credential-file ../.env
```

The runner uses Harbor 0.26.0, OpenCode 1.19.5, and
`google/gemini-3.8-flash`, with three independent agent repeats per world.
Every attempt is retained. A failed comparison stops the run; a changed candidate
requires a new identity and fresh worlds. The paired analysis averages repeats
within each world before estimating the reference-minus-agent gap. Three repeats
on three worlds remain three independent worlds. A larger evaluation requires a
new predeclared analysis and at least 30 fresh worlds if the pilot justifies it.

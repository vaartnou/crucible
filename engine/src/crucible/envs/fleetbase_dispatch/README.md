# Fleetbase Dispatch

`fleetbase_dispatch@1` is a new environment built from main. It does not import or
register the previous dispatch environments. `guess@1` and `incident@1` retain
their scoring and historical golden results.

The operator manages orders through order-management tools: it creates orders,
assigns them to vehicles, orders each vehicle's stops, dispatches, and advances
explicit virtual time. A private simulator reads the committed order records,
moves the vehicles and applies verified progress back to the records. Application
status (for example a native `completed`) never earns value by itself.

This is the v2 world ("make it hard" plan, steps 2 and 3). Plan step 4 replaces the
temporary reward bound with a hindsight solver and tunes the defaults; step 5 ports
the new operations to native Fleetbase.

## Rules

World (`world.py`). Every setting is a field of the frozen `WorldConfig`; override
it with `dataclasses.replace`. `make_world(seed, salt, config)` is deterministic.
Defaults:

- 6 vehicles with capacities spread over 2–8, a cost per distance unit
  (0.15–0.45, larger vehicles cost more) and a shift end (150–240; one vehicle works
  the full horizon). The shift end is on the vehicle record from the start.
- 40–50 requests in 6 waves (minutes 0, 40, …, 200), horizon 240. Each request has
  a load (1–4), a value (10–80, spread 8×), a ready time (up to 15 minutes after
  release) and a deadline (ready time + direct trip + 20–60 minutes).
- 3 urgent requests with 3× value and a 20-minute slack are released between waves.
- The service area is sized so that demand is about 1.4× the fleet's shift minutes
  (see the `world.py` docstring for the exact definition).
- Disruptions are a seeded schedule inside the world, revealed when their time comes:
  breakdowns (35 % of vehicles, 20–45 minutes, announced in the event feed), customer
  cancellations (8 % of requests) and dropoff changes (8 %), silent by default
  (`silent_share = 1.0`: only the order record changes), and traffic zones (40 % of
  waves, a rectangle with 0.5× speed for 45 minutes, announced in the event feed).
- `get_forecast` gives the expected number of requests per zone (3×3 grid) for the
  next wave only. Future requests are never visible.

Physics (`sim.py`):

- A vehicle follows its route: the rank-ordered pickup and dropoff stops of its
  dispatched orders. `assign_order` appends the order's stops; `set_vehicle_route`
  reorders them (the rank is kept in the order's `meta`). Assigned but undispatched
  orders are not served.
- Capacity is enforced at every stop. A route whose pickup would exceed capacity is
  rejected (tools refuse it; the simulator also rejects such a committed order).
- Straight-line travel at 1 unit per minute, times the factor of every active traffic
  zone containing the vehicle (integrated exactly per segment). A vehicle waits at a
  pickup until the ready time. Stops take no time.
- Breakdown: the vehicle stops until the announced repair time, then resumes. Orders
  assigned to it and not yet picked up become unassigned; cargo on board waits.
- Shift end: the vehicle stops for good; cargo still on board is never delivered.
- Customer cancellation: an order not yet picked up leaves its route. Cargo already
  on board is unloaded where the vehicle is, frees capacity and earns nothing. The
  vehicle record shows the changed route, because the vehicle no longer drives there.
- Dropoff change: every route uses the new dropoff from that moment.
- Before pickup an order can be reassigned, unassigned or cancelled; after pickup it
  stays with its vehicle (its dropoff can still be reordered).

Economics:

- `U = on-time value + late value − Σ per-vehicle driving cost − cancellation fees`.
- Late delivery earns the value reduced linearly to 0 over `late_window` (30) minutes.
- Cancelling an order that was *ever* dispatched costs the agent 8 (once per order;
  unassigning first does not avoid it). Customer cancellations cost nothing.
- `reward = clip(U / u_bound, 0, 1)`. Until plan step 4, `u_bound` is a temporary
  optimistic bound: every request served directly at its earliest possible time with
  no driving cost, unless the customer cancels it first.
- `finish`, policy stop and budget exhaustion continue dispatched work to the
  horizon. Rejected calls have no added penalty. The score breakdown has
  `on_time_value`, `late_value`, `driving_cost`, `cancellation_fees`, `utility`,
  `u_bound`, `reward` and counts.

Tools (`task.py`). Actions return only the record they touched; lists return 10
records per page. The reset observation has only the time, horizon, counts and
budget (300 calls, provisional).

| Group | Tools |
|---|---|
| Orders | `list_orders(status?, page)`, `get_order`, `create_order`, `update_order`, `cancel_order` |
| Assignment | `assign_order`, `unassign_order`, `dispatch_order`, `set_vehicle_route(vehicle_id, stops[])` |
| Fleet | `list_vehicles(page)`, `get_vehicle`, `reposition_vehicle(vehicle_id, point)` |
| Info | `list_events(since)`, `get_forecast`, `estimate_route(vehicle_id, stops[])` |
| Time | `advance(minutes)`, `finish` |

- `update_order` changes only `note` (free text, at most 200 characters) and
  `priority` (`low`, `normal`, `high`). They are stored on the order record for the
  operator's own bookkeeping and do not affect physics or economics.
- `set_vehicle_route` must list every remaining stop of the orders assigned to the
  vehicle exactly once, pickup before dropoff, within capacity at every stop.
- `reposition_vehicle` sends an idle vehicle (no dispatched stops) toward a point
  inside the service area. Dispatching work to it cancels the reposition.
- `estimate_route` uses only public knowledge: current dropoffs and announced
  traffic zones. It changes nothing.
- `list_events(since)` returns public events with a sequence number above `since`:
  releases, pickups, deliveries, breakdowns and repairs, unassignments, shift ends,
  traffic zones, the agent's own cancellations and rejected commitments.
- The order record has a public `status`: `requested` (no native order yet),
  `created`, `assigned`, `dispatched`, `picked_up`, `delivered`, `delivered_late` or
  `cancelled` (with `cancelled_by`).

`hidden_strings()` lists the IDs and coordinates of requests released after the last
successful tool result, every disruption ID not announced by then (silent ones are
never announced), and private field names; the generic no-hidden-state check
searches every tool result for them.

The reference uses only public tools. Every 10 minutes it reads the vehicles and the
open orders, then appends orders to the vehicle where they add the most profit per
minute; greedy gives each idle vehicle the order with the nearest pickup;
`do_nothing` stops immediately; `stop_early` stops after the first dispatch. Plan
step 4 adds the strong reference, the policy ladder and the exploit policies.

## Deployment

Until plan step 5, `FleetbaseAdapter` raises `NotImplementedError` for the v2
operations (cancel, update, unassign, routes and world-driven record changes), so v2
episodes need `FLEETBASE_BACKEND=memory`. The deployment below describes the native
stack that step 5 targets.

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

## Backends and `fleetbase_sim@1`

`DispatchTaskBase` (`task.py`) holds the tools, physics bridge and grading. The
order backend is a `DispatchAdapter` (`adapter_protocol.py`):

- `FleetbaseAdapter` (`adapter.py`) talks to native Fleetbase over HTTP.
- `InMemoryAdapter` (`memory_adapter.py`) keeps orders in dicts with
  deterministic IDs. It returns the same `committed()`/`snapshot()` shapes and
  rejects the same operations as native: unknown order or vehicle, dispatch
  before assignment, change after pickup, and cancelled orders. Create and
  repeated dispatch are idempotent, as in native. It also implements the v2
  operations: cancel, update, unassign, routes (ranks in order `meta`) and many
  orders per vehicle.

`fleetbase_dispatch@1` uses native by default; `FLEETBASE_BACKEND=memory` selects
the in-memory backend without the HTTP runtime. `fleetbase_sim@1` has the same
tools, instruction and reward, always uses memory, has no deployment bundle, and
runs the generic task checks. `tests/test_parity.py` replays one recorded tool
trace on every backend and requires equal events, economics and rejection counts.
Its native halves are skipped until step 5.

## Validation and comparison

Offline world/simulator/adapter/task tests check trust, deterministic physics,
read-back after ambiguous mutations, duplicates and frozen evidence.
`fleetbase_dispatch@1` opts out of the generic offline tests because they cannot
provision the application; `fleetbase_sim@1` runs them on the same logic. See [validation](../../../../../docs/fleetbase-validation.md).

The pilot tooling below (`scripts/fleetbase_pilot.py`,
`scripts/fleetbase_counterfactual.py`) was written for the v1 world and v1 tools and
does not run against v2 yet. After clean reference validation, freeze a candidate
and three fresh paired worlds. Preserve private exports and run records in ignored folders:

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

# Fleetbase Dispatch

`fleetbase_dispatch@1` is a new environment built from main. It does not import or
register the previous dispatch environments. `guess@1` and `incident@1` retain
their scoring and historical golden results.

The operator manages orders through order-management tools: it creates orders,
assigns them to vehicles, orders each vehicle's stops, dispatches, and advances
explicit virtual time. A private simulator reads the committed order records,
moves the vehicles and applies verified progress back to the records. Application
status (for example a native `completed`) never earns value by itself.

This is the v2 world ("make it hard" plan, steps 2–4): the defaults below are tuned and
frozen with the policy sweep (see [validation](../../../../../docs/fleetbase-validation.md),
"v2 heuristic sweep"), and the reward is normalised by a hindsight planner. Plan step 5
ported the new operations to native Fleetbase; Track C confirmed that port on a live
stack (see "Deployment" and "Track C" below).

## Rules

World (`world.py`). Every setting is a field of the frozen `WorldConfig`; override
it with `dataclasses.replace`. `make_world(seed, salt, config)` is deterministic.
Defaults:

- 6 vehicles with capacities spread over 2–8, a cost per distance unit
  (0.15–0.45, larger vehicles cost more) and a shift end (150–240; one vehicle works
  the full horizon). The shift end is on the vehicle record from the start.
- 40–50 requests in 6 waves (minutes 0, 40, …, 200), horizon 240. Each request has
  a load (1–4), a value (10–80, spread 8×), a ready time (up to 25 minutes after
  release) and a deadline (ready time + direct trip + 25–80 minutes).
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
- `reward = clip(U / U_hindsight, 0, 1)`. `U_hindsight` (`hindsight.py`, private) is
  the utility of the reference's route planner when it knows the whole world at t=0:
  all requests, cancellations, dropoff changes, traffic zones and breakdowns. The plan
  is executed in a fresh simulator with the real physics (the released prefix of each
  route is committed every minute; an empty truck may be sent ahead to a pickup that
  is not released yet, as `reposition_vehicle` allows), so `U_hindsight` is achieved,
  not estimated. Four planning variants run (one static plan and rolling replans) and
  the best executed utility counts. It is cached per world. An online policy can in
  principle beat it (the reward is then clipped to 1); the reference never did on the
  50 dev seeds.
- `finish`, policy stop and budget exhaustion continue dispatched work to the
  horizon. Rejected calls have no added penalty. The score breakdown has
  `on_time_value`, `late_value`, `driving_cost`, `cancellation_fees`, `utility`,
  `u_hindsight`, `reward` and counts.

Tools (`task.py`). Actions return only the record they touched; lists return 10
records per page. The reset observation has only the time, horizon, counts and
budget (530 calls; the reference uses about 60 % of it on the dev sweep).

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

Policies (`policies/`, public tools only; registered for both tasks):

| Policy | What it does |
|---|---|
| `do_nothing` | Stops at once (generic). |
| `stop_early` | The reference until its first dispatch, then `finish`. |
| `fifo` | Each vehicle whose route ends within 10 minutes gets the oldest open order. |
| `greedy_no_replan` | Nearest pickup, open loop: reads the vehicles once and predicts their availability from its own dispatches; never reads events or dispatched orders. |
| `greedy_nearest` (alias `greedy`) | Each vehicle whose route ends within 10 minutes gets the open order with the nearest pickup; re-reads vehicles and unassigned orders every epoch. Ignores value, deadlines and cost. |
| `reference` | Online regret insertion + 2-opt (`policies/solver.py`). Every 5 minutes, when events arrived: polls `list_events`, the vehicles, new and unassigned orders and the in-progress orders (silent changes), plans incrementally and from scratch, keeps the better plan, and applies only the difference (unassign, create/assign/dispatch, `set_vehicle_route`). Counts its calls and keeps a reserve. |

Exploits (each must end cleanly and score below `greedy_nearest`): `one_vehicle`,
`create_then_cancel`, `reposition_spam`, `estimate_spam` (until the budget runs out),
`finish_immediately`, `assign_broken_vehicle`, `reroute_after_pickup`.
`fleetbase_sim@1` names them, with `stop_early`, as its `TaskChecks.exploit_policies`.

`scripts/fleetbase_sweep.py` runs the ladder and the exploits over a `WorldConfig` grid
on the in-memory backend and checks the gates.

## Deployment

`FleetbaseAdapter` implements every v2 operation (plan step 5): cancel through
`DELETE /v1/orders/:id/cancel`, note/priority and route ranks in order `meta`,
unassign by clearing `driver`/`vehicle`, several orders per driver, and world changes
mirrored into native records (customer cancellation cancels the order, a dropoff
change moves the dropoff place, a breakdown unassigns, and breakdown/repair/shift end
set the vehicle status). Native Fleetbase has no stop sequencing and no un-dispatch,
so route ranks and an un-dispatch marker live in `meta`. Native vehicle status is a
validated enum without an off-shift value, so the vehicle's world state (`available`,
`broken`, `off_shift`) is in vehicle `meta.crucible_state` and native status is
`available` or `out_of_service`. The endpoints beyond the v1 audit were verified on a
live stack in Track C: see the audit's "v2 operations" section for each assumption.
`deployment/native-proof.php` probes them, and the smoke runner below runs it first.

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

The policies above use only public tools and observations, on either backend. The
same continuation rules apply to every operator.

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
The trace includes cancellations, reroutes, unassigns, notes and every world
disruption. Against the offline HTTP fake of the native contract it also runs with
injected post-commit HTTP 500s and requires cleanup to leave zero records, and an
adapter-level script compares `committed()` and rejections step by step with memory.
The live half runs only with `FLEETBASE_URL`, `FLEETBASE_TOKEN`,
`FLEETBASE_DEPLOYMENT_ID` and `FLEETBASE_EXCLUSIVE=1`.

### Track C: live stack checks

The fake proves the adapter logic against the documented contract, not that the pinned
Fleetbase behaves as documented. Track C shows, on a real stack (items 1–4 are recorded in
[validation](../../../../../docs/fleetbase-validation.md), "v2 native confirmation";
item 5 is the pilot):

1. The native contract probe passes (`native-proof.php`, every v2 assumption), followed
   by a clean `count-state.php`.
2. Live parity: `test_live_native_matches_memory` passes against an exclusive stack.
3. A clean-stack episode (reference, greedy, do-nothing, stop-early) with clean
   cleanup counts after each, and reset-and-replay with equal events, economics and
   world.
4. The Harbor oracle on an exported `fleetbase_dispatch@1` task: positive reward, no
   env error, no cleanup errors.
5. A new experiment identity (`fleetbase-native-v2-pilot-<candidate>`, from
   `scripts/fleetbase_pilot.py freeze`) with 3 fresh salted worlds.

`scripts/fleetbase_counterfactual.py` is v1-only and refuses to run.

## Validation and comparison

Offline world/simulator/adapter/task tests check trust, deterministic physics,
read-back after ambiguous mutations, duplicates and frozen evidence.
`fleetbase_dispatch@1` opts out of the generic offline tests because they cannot
provision the application; `fleetbase_sim@1` runs them on the same logic. See [validation](../../../../../docs/fleetbase-validation.md).

`scripts/fleetbase_pilot.py` is tool-agnostic and ported to v2: a freeze creates the
experiment identity `fleetbase-native-v2-pilot-<candidate>` and `run` refuses v1
experiments. `scripts/fleetbase_counterfactual.py` is v1-only and exits with a message.
After clean reference validation (Track C), freeze a candidate and three fresh salted
worlds. Preserve private exports and run records in ignored folders:

```bash
uv run --directory engine python scripts/fleetbase_pilot.py freeze \
  --out ../runs/fleetbase-pilot-v2 --reference-verified
uv run --directory engine python scripts/fleetbase_pilot.py run \
  --out ../runs/fleetbase-pilot-v2 --credential-file ../.env
```

The runner uses Harbor 0.26.0, OpenCode 1.19.5, and
`google/gemini-3.8-flash`, with three independent agent repeats per world.
Every attempt is retained. A failed comparison stops the run; a changed candidate
requires a new identity and fresh worlds. The paired analysis averages repeats
within each world before estimating the reference-minus-agent gap. Three repeats
on three worlds remain three independent worlds. A larger evaluation requires a
new predeclared analysis and at least 30 fresh worlds if the pilot justifies it.

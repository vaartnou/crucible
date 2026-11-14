# Plan: make `fleetbase_dispatch@1` hard

Overwrite v1 in place and keep the ID `fleetbase_dispatch@1`. The agent still talks
only to our MCP tools. Tune difficulty with scripted policies on the in-memory
backend first. Confirm on native Fleetbase last.

Package: `engine/src/crucible/envs/fleetbase_dispatch/`. Tests: `uv run --directory engine pytest -q`.
Baseline before this work: 595 passed, 17 skipped.

## Gates (dev seeds)

| Gate | Target |
|---|---|
| Do-nothing | 0 |
| Greedy | 0.35–0.55 |
| Reference | 0.75–0.85 |
| Agent | ≥ 0.15 below the reference on every world |
| Exploit policies | below greedy, never crash the grader |

## Steps

Each step ends with the full test suite green and one commit.

### Step 0: housekeeping
1. Move `docs/fleetbase-findings.md`, `docs/fleetbase-pilot-results.json` and the v1
   numbers section of `docs/fleetbase-validation.md` to `docs/archive/`. Add a
   "superseded by v2 economics" note at the top of each archived file.
2. Check every link to `fleetbase-api-audit.md` (and to the moved files) and fix it.
3. Set `scoring_hash = "fleetbase-physical-economics-v2"`.

### Step 1: adapter protocol and in-memory backend
1. `adapter_protocol.py`: `class DispatchAdapter(Protocol)` with the async methods
   `FleetbaseAdapter` already has (`prepare`, `create_order`, `assign_order`,
   `dispatch_order`, `committed`, `sync_events`, `snapshot`, `cleanup`).
2. `memory_adapter.py`: `InMemoryAdapter` that uses dicts, no HTTP. It raises
   `FleetbaseOperationError` in the same cases native does: unknown order,
   duplicate create, dispatch before assign, change after pickup, cancelled order.
3. Refactor `task.py`: a shared base class picks the adapter. `FLEETBASE_BACKEND=memory|native`
   (default native for `fleetbase_dispatch@1`).
4. Register `fleetbase_sim@1`: same tools, instruction and reward. It always uses memory,
   is not an external backend and has no deployment bundle. Turn on the generic
   `TaskChecks` for it (determinism, do-nothing = 0, reward bounds, no hidden
   state, reference solves, exploits below reference).
5. Parity test: replay a recorded tool trace against the memory adapter and a
   native adapter. Assert equal events, economics and rejected-call counts. Skip
   the native half unless a stack is configured (`FLEETBASE_URL`).

### Step 2 + 3: world v2, simulator v2 and tools v2 (one step, they share the data model)
World (`world.py`): all settings in one frozen `WorldConfig` dataclass with defaults.
- 6 vehicles. Capacities 2–8. Per-vehicle `cost_per_distance`. A `shift_end` for each vehicle.
- 40–50 requests in 6 waves, horizon 240. Demand ≈ 1.4× capacity.
- Values vary 5–10×. Add a few urgent high-value requests.
- Each request has `ready_time` and `deadline`. Late delivery earns value that decays
  linearly to 0 over `late_window` (default 30) minutes.
- Disruptions, seeded through `RngStreams` (salted): vehicle breakdown (event feed),
  shift end (on vehicle record), customer cancellation (silent, order record only),
  dropoff change (silent, order record only), traffic zone 0.5× speed for a window
  (event feed), urgent request released mid-wave.
- Public zone-level forecast of next-wave expected demand.

Simulator (`sim.py`):
- Multi-stop: a vehicle follows an ordered stop list and carries several orders up
  to capacity. Capacity is enforced at every stop.
- Breakdown: the vehicle is offline for N minutes. Orders not yet picked up drop back
  to unassigned. Cargo on board waits.
- Shift end: the vehicle stops at the end of its shift.
- U = on-time value + decayed late value − driving cost − cancellation fees.
  `reward = clip(U / U_hindsight, 0, 1)`. Put all raw parts in the breakdown.
  Until step 4 adds the hindsight solver, use a temporary upper bound and mark it with a TODO.

Tools (`task.py`): actions return only the record they changed. Lists return 10 records
per page.
- Orders: `list_orders(status?, page)`, `get_order`, `create_order`, `update_order`, `cancel_order`
- Assignment: `assign_order`, `unassign_order`, `dispatch_order`, `set_vehicle_route(vehicle_id, stops[])`
- Fleet: `list_vehicles`, `get_vehicle`, `reposition_vehicle(vehicle_id, point)`
- Info: `list_events(since)`, `get_forecast`, `estimate_route(vehicle_id, stops[])`
- Time: `advance(minutes)`, `finish`
- Cancelling an order after dispatch costs a fee. Rejected calls have no reward penalty.
- Provisional budget 300. Step 4 sets the final budget.
- Rewrite the instruction. It describes the rules and economics only and gives no strategy hints.
- Extend the memory adapter with cancel, update, unassign, routes and multiple orders per vehicle.

### Step 4: policy ladder, hindsight and sweep
Policies (`policies/`): do-nothing, stop-early, FIFO, greedy-nearest, greedy-no-replan,
reference (online regret insertion + 2-opt for each vehicle, replans on every
release or event, reads `list_events` and changed orders, uses public tools only, stays in budget).
Exploit policies: everything to one vehicle, create-all-then-cancel, reposition spam,
estimate_route spam until the budget runs out, finish immediately, assign to a
broken-down vehicle, change the route after pickup. Register them in `TaskChecks.exploit_policies`.

Hindsight: the same solver with the full world at t=0. It is not a tool policy and
gives `U_hindsight`. Replace the temporary denominator with it.

Budget: set `max_steps` so the reference uses about 60%.

`engine/scripts/fleetbase_sweep.py`: a grid over `WorldConfig` (demand/capacity ratio,
deadline tightness, disruption rate, share of silent events). It runs the ladder on
50 dev seeds with the memory backend. It prints mean and SD for each policy and the
reference − greedy gap, and checks the Track A gate: gap ≥ 0.25, greedy-no-replan
clearly below greedy with disruptions on, exploits below greedy.
Tune the defaults until the gates on this page pass. Then freeze them and record the
table in `docs/fleetbase-validation.md`.

### Step 5: native port (code only here; live verification requires a Docker stack)
Add cancel, update, unassign, multiple orders per driver and the stop list in order
meta to `FleetbaseAdapter`. Mirror disruptions into native records. Live parity, the
clean-stack episode, reset-and-replay, cleanup counts and the Harbor oracle are Track C.

Status: code done and offline-verified (fake-contract parity with injected 500s, zero
records after cleanup). Endpoint assumptions are listed in
`docs/fleetbase-api-audit.md`, "v2 operations (unverified against live stack)"; the
Track C commands are in `docs/fleetbase-validation.md`.

## Tracks needing infrastructure (not done by the coding steps)
- **Track B**: run the agent on `fleetbase_sim@1` with `crucible export harbor` and `harbor run -a opencode`
  using Gemini Flash plus one stronger model. Log the results in `runs/fleetbase-sweep-log.md`.
  Gate: agent ≤ reference − 0.15 on all 3 seeds for both models.
- **Track C**: native parity and Harbor runs on 3 fresh salted worlds with a new
  experiment identity. Use one Compose project per trial and about 4 GB RAM per stack.

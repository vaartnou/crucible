# SupplyGym system map

Snapshot: 2026-11-14. This map describes the inspected upstream baseline, not every change in this worktree. Source baseline: `origin/supplygym-task` at `1e830d4d78ca38c8cded26461420a03e3d3aa07b`. GitHub issues #18–23 and PRs #24–28 were read through the GitHub connector on this date. No benchmark run was performed for this document.

## What exists in the baseline

SupplyGym is an environment inside the Python Crucible engine. It is not yet on `main` at this snapshot: `supplygym-task` starts after main commit `59b15f2`. The baseline has two registered tasks, `supplygym@1` and `supplygym-fresh@1`, using the same simulator, controls, grader and policies. The fresh task selects a different synthetic world; `difficulty` is ignored.

The baseline includes these commits after its initial `480d4a9` implementation:

| Commit | Actual addition |
|---|---|
| `597536e` | Terminal credit for stock on hand and shipped inventory in transit |
| `8876bd6` | Tool schema accepts exactly the representable control ranges |
| `d0d16f7` | Degenerate-policy integrity gate, with known holes marked xfail |
| `1e830d4` | Weekly realised cost ledger in status |

Some upstream README text predates these commits: its statement that leftover inventory has no terminal value is stale. Code credits it at $10/unit. PR descriptions that say branches need rebasing are also partly stale; inspect actual refs and diffs.

## Engine integration

Paths below are relative to the repository root.

| Layer | Files | Contract |
|---|---|---|
| Discovery | `engine/pyproject.toml`, `engine/src/crucible/core/registry.py`, `engine/src/crucible/envs/supplygym/__init__.py` | `crucible.envs` entry point imports SupplyGym, registering both tasks and their policies |
| Task API | `engine/src/crucible/core/task.py`, `engine/src/crucible/envs/supplygym/task.py` | Frozen `TaskSpec`; reset, tools, step, done, grade, instruction; task ref includes version |
| Session | `engine/src/crucible/core/session.py` | Shared local/HTTP/MCP call counting, transcript and grading; 120-call budget |
| Local execution | `engine/src/crucible/core/episode.py`, `engine/src/crucible/core/executor.py` | Policy observes tool results; executor runs jobs and saves transcripts/summary |
| Server | `engine/src/crucible/server/` | HTTP/MCP transports use the same task and session behavior |
| Harbor | `engine/src/crucible/export/harbor.py`, `engine/src/crucible/export/templates/harbor/` | Agent container talks to simulator sidecar; wheel/locked dependencies vendored; verifier grade token and per-task world salt |

Every successful or rejected in-budget tool call consumes a session step. A refused SupplyGym action changes no simulation time, controls or switching fee, but does consume a tool-call budget slot. Calls to a closed/over-budget/already-finished session are refused without counting. Environment exceptions produce an error outcome with zero reward; only infrastructure failures have the executor retry contract.

Harbor installs the wheel in the simulator sidecar and includes a separate copy in `solution/` for oracle runs; ordinary agent runs have no engine installed in their main container. The verifier writes `reward.txt`, `grade.json` and `transcript.json` from the token-protected grade endpoint. Harbor manifests record task ref, seeds, engine version, source commit and wheel SHA256. World salt is sidecar-only, passed through `reset`; separate exports with the same numeric seed do not identify the same tape. Keep comparisons within the same exported dataset or explicitly verified unsalted local world.

## Episode and action flow

`task.reset(seed, salt=...) -> Play -> SupplyEnv.reset -> make_world + make_tape`

`set_controls -> schema validation -> supplier/approval validation -> Play.apply -> to_action -> SupplyEnv.step -> daily ordering, arrivals, demand and signals -> public status`

`grade -> default-complete remaining days -> replay default and reference on same regime/seed/salt -> Score`

One SKU, 365 days, 7-day decisions (53 control actions, final interval shorter), 10 direct suppliers and 2 sequentially approved alternatives. The agent has four tools: `get_briefing`, `get_status`, `set_controls`, `finish`. Reads do not advance the simulation. `finish` stops agent interaction; grading runs the remaining days under default controls.

`set_controls` accepts standing supplier weights, safety-stock days, order multiplier, expedite fraction, and approval of the next alternative. Omitted fields retain their applied value; `{}` means default only from the initial default state, not a reset of previously changed dials. The grader's `run_default` deliberately passes the zero simulator action instead. Applied controls are echoed publicly.

| Field | Accepted range / behavior |
|---|---|
| Supplier weights | `exp(-4)` through `exp(4)`, IDs S01–S10, A01, A02; active capacity shares are reweighted |
| Safety stock | 0–3 extra days of demand |
| Order multiplier | `exp(-0.5)` through `exp(0.5)` |
| Expedite | 0–0.3 of each order at half lead time, +$3/unit |
| Approve alternative | A01 then A02; $2,000 each and 14 days onboarding; repeats/order errors refused |

Simulator action dimension is 17. The public task does not expose index 16's standalone pass-through mode. PPO emits 16 controls; its output is converted into the same public `set_controls` fields. The default zero action is a daily base-stock ordering rule, not absence of purchasing.

## Static world and exogenous tape

`engine/src/crucible/envs/supplygym/world.py` currently creates a synthetic graph, not a real company map or acquired dataset. There are 32 nodes: 10 direct, 2 alternative, 12 tier-2, 8 tier-3. Public static data covers the 12 direct/alternative suppliers; tier-2/3 and dependency edges are hidden. Five coarse regions are NA, EU, APAC-N, APAC-S and LATAM. Concentration is synthetic: two tier-2 hubs each feed at least four directs. Parent weights are normalized; upstream health losses propagate with coefficient 0.8.

`World.nodes` carries identity, kind/tier, region, coordinates, capacity, lead time, price, failure rate and port flag; `parents` carries weighted upstream edges. `World.public()` whitelists static public fields. `make_world` and array indexing currently assume the fixed direct/alternative/tier ordering; a data loader must preserve or explicitly adapt that contract.

`Tape` carries events, rumors (including hidden outcomes), public news, demand, hidden propagated health, risk noise and risk staleness. `make_tape` pre-generates 365+90 days. Independent PCG64 streams separate event, demand, news, false-alarm, rumor and risk generation. Agent actions consume no exogenous randomness, supporting paired common-random-number comparisons. Overlapping disruptions already occur in the standard generator: independent event starts are combined by the maximum severity for each affected node/day.

Salted tape seed is the first 8 bytes of SHA256(`seed:salt`), interpreted big-endian; empty salt retains the integer seed. Static world generation is keyed separately. The simulator supports signal ablations (`full`, `no_news`, `none`, `placebo`) and privileged `truth_in_info` for internal use; normal task output does not enable privileged truth.

| Regime | Static graph seed | Direct price range | Disruption rate scale |
|---|---:|---:|---:|
| `supplygym@1` | 7 | $9.60–10.40 | 1.0 |
| `supplygym-fresh@1` | 11 | $9.00–11.00 | 1.5 |

Economics: mean demand 100/day, sale price $15, holding $0.20/unit/day, lost-sale penalty $10/unit in addition to forgone revenue. Alternative prices are $12–13. Initial inventory is 700 units with an empty pipeline, a known cold-start condition. Terminal credit is $10 times on-hand plus actually shipped inbound units, applied once; this is an accounting approximation, not exact per-unit acquisition-cost valuation.

## Public observations and reference parity

`state.py` builds public status and converts controls; `play.py` owns episode state and public logs. Status exposes day/weeks left, inventory position, on-hand, base-stock level, supplier risk/late orders/fill/pipeline/arrival/onboarding, regional signal summaries, recent signals, current controls and profit to date. Logs strip internal node/severity fields; signal texts remain noisy public information.

`last_week` adds realised revenue, supplier purchase cost, expedite, holding, lost-sale penalty, switching, profit, previous controls and a count of changed control categories. It also contains `delta_vs_default`, a one-week same-state counterfactual using only the week just realised. It is not the full episode's paired default delta. Four control categories are counted, including the entire supplier-weight mapping as one category; approval is not recorded in that previous-controls mapping.

`policies/views.py` models status, and `to_vector` reconstructs the original 112-number PPO observation. The numpy PPO is a 64–64 tanh MLP loaded from `policies/data/supplygym_ppo.npz`; it receives public information only. The new ledger is available in status but deliberately ignored by the frozen PPO vector, so availability parity does not mean the reference uses every public field. No hindsight oracle is used as the reward reference.

Tests pin observation reconstruction, in-process/tool-path reference agreement, salt determinism, refusal behavior, disjoint splits, no-future ledger behavior, ledger invariance of reference decisions and exact representable control ranges. These contracts need preserving when a real-data world replaces the synthetic graph.

## Scoring and integrity limitations

Baseline `grader.py` loads hashed `scoring.yaml` (`headroom_floor: 1000`, `reference: ppo`):

```text
reward = clip((profit - profit_ss) / max(profit_ref - profit_ss, 1000), 0, 1)
```

Both anchors replay the same regime, seed and salt. The grader default-completes unplayed weeks. No calls, immediate finish and all-default full-year controls score exactly zero. A profitable controlled prefix followed by default completion can receive positive reward in this baseline: its shipped `stop_early` policy plays PPO for four weeks and can match or beat the full-year reference. Thus the instruction's generic claim that stopping early earns zero overstates the actual baseline.

The breakdown reports profit, both anchors, both deltas, service, lost units, weeks played and five cost lines. Terminal credit affects profit but is not a separately named breakdown line; an accounting report must include it explicitly when bridging revenue minus costs to profit. Reward clipping and the $1,000 denominator floor can conceal small gains or reference losses. Use raw paired profit/deltas for performance claims and recalibrate after changing world/economics.

The task disables `reference_solves` and `exploits_below_reference` generic checks because the reference does not reliably dominate. Existing README claims of roughly $2k dev gains and 70% wins are historical, not remeasured evidence for a changed world or even an assurance after terminal-credit changes. The standalone PPO's old gain claims also used a different default and overlapping training/dev bank.

`tests/test_integrity.py` tests nine dumb strategies across the first six dev seeds and both regimes. It requires exact zero for no calls/immediate finish/all-default, and otherwise mean gain below $300 and mean reward below 0.2. However, it skips execution with `pytest.xfail` for known holes: maximum safety stock in regimes 0 and 1, and end hoarding in regime 0. Passing pytest therefore does not establish the integrity acceptance criterion. The narrower constant-control exploit test uses eight training seeds and a $500 tolerance; it is not a substitute for a clean dev integrity gate.

## Seed banks and evaluation

| Bank | Inclusive range | Purpose |
|---|---|---|
| train | 20000–21999 | External trainer only |
| dev | 1000–1029 | Development evaluation |
| heldout | 10000–10049 | Held-out standard world |
| fresh | 30000–30049 | Fresh-world evaluation |

`python -m crucible.envs.supplygym.evaluate` already produces paired raw deltas, bootstrap 95% intervals and win rates via native engine runs. It uses unsalted tapes, accepts task/split/policies/repeats and saves transcripts. Keep complete unstratified banks alongside any selected hard subset; do not retune on held-out/fresh evaluation results.

## Open work: issues and PRs

All six issues were open and all five PRs below were open drafts at inspection. These are pending capabilities, not baseline behavior. GitHub state is a timestamped observation.

| Issue / PR | Scope and actual status |
|---|---|
| [#18](https://github.com/vaartnou/crucible/issues/18) / [#24](https://github.com/vaartnou/crucible/pull/24) | Issue requests one-command dumb-policy gate, table, tolerance, CI and clean/bounded outcome. Base already has tests with xfails. Current PR head `4f2ac1a` adds `require_complete: true`, zeros reward for incomplete control years while retaining raw deltas, updates flags/instructions/goldens and adds a regression test. It does not fix full-year safety-stock/hoarding holes. Base `supplygym-task`. |
| [#19](https://github.com/vaartnou/crucible/issues/19) | Weekly ledger already landed directly in baseline `1e830d4`; no PR among #24–28. Remaining acceptance work includes measured LLM before/after evidence and complete approval memory semantics. PPO ignores ledger by design. |
| [#20](https://github.com/vaartnou/crucible/issues/20) / [#28](https://github.com/vaartnou/crucible/pull/28) | Opt-in hard tier, head `ae202fa`. Adds hard task IDs through explicit registration, seeds >=30000, risk lag 7/14 days plus weekly update delay, additional false headlines and a 20% chance of an extra overlapping strike. Standard already permits overlap. Separate evaluation flag/report; frozen PPO is a transfer/generalisation result. No correlated season feature. Base `supplygym-task`. |
| [#21](https://github.com/vaartnou/crucible/issues/21) / [#25](https://github.com/vaartnou/crucible/pull/25) | Generic saved-run paired-analysis tool, head `13f324b`, based on `main`. Reads summary CSV; seed-level repeat averaging, paired bootstrap, win/tie counts, exact sign test, hard subset beside full result, optional hindsight-normalized diagnostic. Protocol fixes 25 paired LLM seeds and alpha 0.05 in advance. It does not run models or establish world identity from CSV. No SupplyGym performance claim follows from adding the tool. |
| [#22](https://github.com/vaartnou/crucible/issues/22) / [#26](https://github.com/vaartnou/crucible/pull/26) | Offline single HTML comparison from saved native transcripts, head `864576c`. Replays and validates task/seed/repeat/source/scoring and results, rejects salted runs, adds divergence tags, timeline and revenue/cost/terminal-credit bridge. Hidden-health labels stay post hoc. Does not require PPO. Base `supplygym-task`. |
| [#23](https://github.com/vaartnou/crucible/issues/23) / [#27](https://github.com/vaartnou/crucible/pull/27) | Optional demand-shock flag, head `19796a4`; off by default. Alternating 1.5x surges and 0.65x dips, 14–28 days within 90-day windows; separate salted stream, trailing 28 completed days of demand visible, no price hikes. Replay inherits flag/salt; standard CLI/Harbor tasks keep shocks off and frozen PPO remains unchanged. Base `supplygym-task`. |

Actual ancestry matters: current #24, #26 and #27 heads include baseline `1e830d4` after rebasing; #28 still diverges from initial `480d4a9`. #25 targets `main`, so port its generic analysis independently. Future cherry-picks should use current diffs and resolve overlapping `world.py`, `sim.py`, `play.py`, `grader.py`, `task.py`, observation schemas and tests deliberately. Do not merge experiments into standard-tier identity silently.

## Invariants for an additional world

| Contract | Regression check |
|---|---|
| An existing task ref keeps its world, default rule, PPO weights, scoring hash and recorded scores. | `engine/tests/policies/test_golden.py` runs the registered reference, named exploits and do-nothing through the engine on seeds 0–9. The shared JSON fixture pins each total with absolute tolerance 1e-9. Append a separate task key for a new world. |
| A seed, world version and sidecar salt identify one game, independent of the policy and the number of reads. | Replay the player, default and reference on identical demand and disruptions. A new salt changes the timeline. A ledger counterfactual cannot mutate the episode. |
| Every accepted public control can be represented. Every refusal is atomic. | Round-trip boundary values. Check ordered approvals, one fee, onboarding delay, omitted fields and the shortened final week. |
| The public reference sees the same observations as the agent. | Retain twelve actionable slots, five regional summaries and 112 PPO inputs. Hide site identities, dependency weights, future arrivals, tape, salt, and unresolved rumor outcomes. Verify same-game tool/replay parity. |
| Purchases, inventory and profit reconcile. | Charge actual shipped units, deduct lost sales and all applicable costs, and credit only paid, unsold units on hand or in transit once at termination. Keep the one-week ledger distinct from the full-year grade. |
| The score measures incremental annual profit. | No calls, immediate finish and all-default play yield exactly zero. Complete a partial year on defaults. Report raw paired deltas and the denominator floor alongside the clipped score. Run all gate strategies on the new world, including both legacy shortcuts. |
| Offline and Harbor runs use the same code and packaged inputs. | Bind data and scenario assumptions to the new task's scoring identity. Keep the seed splits disjoint and the sidecar salt outside the agent view. |

## Guidance for an additional world

Preserve explicit provenance and separate source facts from synthetic imputation. The present graph and disruption tape are generated together only by convention; a loader can replace static topology while retaining hidden salted exogenous generation, but must update both grader replays and simulator dimension/index assumptions. Version world identity, validate graph/IDs/edges and public-field boundaries, and include dataset identity in reproducibility records. Reusing a numeric seed or task ref alone does not establish a comparable world after data replacement.

Freeze the world, default, terminal accounting and scoring reference before confirmatory LLM measurements. Re-run complete integrity strategies without the existing xfails, raw default/reference calibration, tool/reference parity and standard/fresh reproducibility on the settled world. Hard tier and shocks should remain separately identified experiments. Retain historical runs with their original world/engine/scoring identity; their clipped scores do not transfer to new economics or topology.

## Data-pack contract v0.2

The supplied `DATA_PACK_CONTRACT.md` is v0.2. It matches the simulator's 32-node shape and five regions: 10 direct suppliers (two per region), two alternatives, 12 tier-2 and eight tier-3 nodes. The updated Cala brief targets at least 20 sourced nodes; missing slots may be filled synthetically and marked `synthetic_fill`.

The contract separates fine source regions and event types from required simulator mappings (`sim_region`, `sim_event_type`). Node-shaped world facts supply aliases, node kind/tier, city-level coordinates, port flag, lead-time ranges, relative price, capacity band and parent-to-child `supplies` edges. Unsupported fields remain null. The builder projects coordinates, rescales prices and fills explicit gaps; these derived simulator values should retain their fact provenance and transformation policy.

Build-time fragments are frozen and hashed. The merged pack adds derived `rates.json`; real event rates are used only with at least five templates per group, with reporting-bias notes and simulator defaults otherwise. The pack hash enters the scoring hash. Episodes and grading read committed data offline. Source events inform parameters and wording; salted generated timing prevents historical replay from becoming a prediction shortcut.

Signal text uses one ASCII headline of at most 160 characters and a `{w}` placeholder instead of real names. Rumor and news styles have distinct credibility/severity fields; optional recovery signals are not consumed by the v1 simulator. Template/content splits are distinct from numeric seed banks and must match the episode bank; held-out/fresh content remains excluded from training and prompt tuning.

The contract calls for a separate task ref for the real-data regime, with its own goldens and reference check. The synthetic PPO's performance on that task measures transfer until a suitable reference/default calibration is demonstrated. The proposed packaged location is `engine/src/crucible/envs/supplygym/packs/<pack_name>/`; fragment acquisition and engine integration remain separate concerns.

[PR #15](https://github.com/vaartnou/crucible/pull/15) introduces the synthetic baseline. Its historical fresh-regime measurements report reference underperformance; they do not validate a pack-backed world.

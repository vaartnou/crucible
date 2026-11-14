# supplygym@1 and supplygym-fresh@1: supply-chain control

One product's inbound supply for 365 days. The agent decides once a week (53 decisions): supplier
order shares, safety stock, an order multiplier, an expedite share, and whether to approve the
two pricier alternative suppliers. Hidden disruptions (strikes, port closures, fires, sanctions,
bankruptcies) hit suppliers and spread down the tiers. The agent sees noisy news, rumors, risk
scores, late orders and fill ratios. Synthetic supplier graph; not real data.

| | |
|---|---|
| Refs | `supplygym@1` (calibration regime), `supplygym-fresh@1` (fresh regime) |
| Entry point | `crucible.envs` → `supplygym = "crucible.envs.supplygym"` |
| Step budget | 120 tool calls (about 53 `set_controls`) |
| Regimes | `supplygym`: the calibration the reward and reference were tuned on. `supplygym-fresh`: other supplier graph, wider price spread, 1.5x disruption rate |
| Seed splits | train 20000-21999 (external trainer only), dev 1000-1029, held-out 10000-10049, fresh 30000-30049 |

## Tools

| Tool | Kind | Effect |
|---|---|---|
| `get_briefing`, `get_status` | read | static facts; current inventory, supplier health, signals, controls |
| `set_controls(...)` | action | new standing controls for 7 days; omitted fields keep their value; refused calls change nothing |
| `finish` | end | stop; remaining weeks run on the default rule |

## Scoring

Reward requires a complete year of control. If the grader completes any weeks, the reward is
zero; raw profit and its paired deltas are still reported.

`reward = clip((profit - profit_ss) / max(profit_ref - profit_ss, 1000), 0, 1)`.

`profit_ss` is the default (s,S) rule on the same tape; weeks the agent did not play run on it, so
no calls, `finish` at once and all-zero controls score exactly 0. `profit_ref` is the reference
policy replayed on the same tape. The breakdown carries raw profit, `delta_vs_ss`, `delta_vs_ref`,
service level and cost lines: use those for claims, not the clipped reward.

## Policies

`reference` (PPO as a numpy MLP, plays through the tools), `ss`, `stop_early`, `max_expedite`,
`do_nothing`. Policy code sees tool results only (`policies/views.py`); the PPO observation is
rebuilt from the status view, so the reference has no information an agent lacks.

## Secrecy

The Harbor export's per-task salt (`SIDECAR_SALT`, set only in the sidecar and passed to `reset`)
is mixed into the tape seed,
so the tape cannot be rebuilt from the public engine code. With an empty salt (offline eval) the
tape is the plain seed.

## Known limits

- The final simulator step credits on-hand and shipped inbound stock at $10 per unit.
  The integrity gate still records known full-year shortcuts; the completion rule only
  addresses rewards from profitable prefixes.
- Evaluate with `python -m crucible.envs.supplygym.evaluate` (paired deltas, bootstrap interval).

## Decisions

- 2026-11-14 — Reward is anchored at the default (s,S) rule, not at do-nothing: `reward = clip((profit - profit_ss) / max(profit_ref - profit_ss, 1000), 0, 1)`. Weeks the agent did not play run on the default controls, so no calls, `finish` at once, and all-zero controls all score exactly 0. Anchored at do-nothing, all-zero controls would have scored about 0.8. Raw profit, `delta_vs_ss` and `delta_vs_ref` are in the grade breakdown because the clipped reward hides small differences.
- 2026-11-14 — The reference is a PPO policy exported to a numpy MLP, playing through the same tools as an agent (status view in, `set_controls` out). It sees nothing an agent does not see; the hindsight oracle of the standalone gym reads the hidden tape and is not used. A scripted runbook (fill, lateness and risk thresholds) did no better than the default rule on dev, so it is not shipped.
- 2026-11-14 — The default rule was recalibrated: order-up-to gap 2.0 -> 0.5 days of demand (`GAP_DAYS`), chosen on training seeds. With the old gap, a constant `order_multiplier=0.7` beat the default by $5,757 per episode on all 30 dev seeds, more than the trained PPO's $4,481, so most of PPO's reported gain was compensation for over-ordering. After the change no constant setting beats the default by more than $500 (`tests/test_exploits.py`). The standalone gym's numbers (PPO +$8,090 over (s,S)) used the old default and no longer apply.
- 2026-11-14 — Seed splits are explicit and tested disjoint: train 20000-21999 (external trainer only), dev 1000-1029, held-out 10000-10049, fresh regime 30000-30049. The standalone gym trained on 0-1999, which contained dev 1000-1029.
- 2026-11-14 — The fresh regime (other supplier graph, wider price spread, 1.5x disruption rate) is its own task, `supplygym-fresh@1`, not a `difficulty` of `supplygym@1`. A task ref then names one fixed world, as in Gymnasium ids or SWE-bench variants: golden scores and generic checks cover both regimes, Harbor results say which world was played, and each regime is versioned on its own. The two tasks share the simulator, grader and policies; the reference was trained on `supplygym` only, so `supplygym-fresh` tests whether it generalises. Use the fresh seed split (30000-30049) for it.
- 2026-11-14 — The tape seed is `sha256(f"{seed}:{salt}")` with the salt `reset` receives (the export's `SIDECAR_SALT`). The engine source is public and the agent shell has internet, so without a salt an agent could rebuild the tape from the seed or by brute-forcing seeds against its first observations. With no salt (offline eval, golden scores) the tape is the plain seed.
- 2026-11-14 — `set_controls` keeps omitted fields, refuses (without a fee or clock change) an out-of-order or repeated `approve_alternative`, and the status view shows `onboarding` with days left. This removes the old prompt bug where an alternative looked "not approved" during onboarding and was requested twice.

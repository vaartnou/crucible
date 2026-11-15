# Cala @3 reward integrity

This repairs the reward findings in PR #44 against PR #37. Versions @1 and @2
retain their original scores and reference bindings.

## Scoring contract

All values come from the verifier's physical inventory and accounting ledger.

- Terminal surplus = operating surplus + $10 times terminal on-hand plus actual
  shipped units still in transit. Unfulfilled requested quantities earn no credit.
- Adjusted surplus = terminal surplus - $50 per lost demand unit - $10 per unit
  by which terminal on-hand plus pipeline falls below the opening inventory position.
- Floor = 95% of the nonnegative adjusted surplus of default `ss` play on a fresh
  replay of the same seeded/salted scenario.
- Score = `clip((adjusted_surplus - floor) / (upper_bound - floor), 0, 1)`.
  Incomplete episodes, including early `finish` and exhausted budgets, earn zero.

The continuity reserve protects the working stock needed to keep serving customers
past the reporting date. $10 terminal credit alone is insufficient: suppliers
charge $10.10, and reducing late orders can still save purchase premium and holding
cost. The reserve removes that residual incentive in the audited starvation attack.
The additional service charge makes foregone service more expensive than modest
inventory buffers. Both charges are explicit in the agent instruction and breakdown.
The floor and certificate are fixed at reset; later actions cannot change them.
Their configuration is included in the scoring hash.

## Upper-bound certificate

Use integer cents. Let `O` be prepaid opening on-hand plus pipeline, `Q` purchased
and shipped units, `S` sold units, `I` terminal on-hand plus pipeline, and `D` total
scenario demand. Conservation gives `I = O + Q - S`. With sale price `p = 1500`,
minimum supplier price `c = 1010`, and terminal value `v = 1000`:

```text
terminal_surplus <= p*S - c*Q + v*I
                 = (p-c)*S + c*O - (c-v)*I
                 <= (p-c)*D + c*O = upper_bound
```

All omitted costs are nonnegative. Each opening unit appears once, even when its
arrival is after day 365. Every newly purchased terminal unit costs at least its
salvage value, so unlimited late purchases cannot generate free money. The
certificate rejects incompatible price assumptions and calls after reset. It
relaxes shipment timing and capacity; it is a certificate, not a feasible optimum.
Since both service adjustments are nonnegative, adjusted surplus is also bounded.
The default replay satisfies the same certificate, so the 95% floor remains below
this positive bound and the denominator stays positive.

## Reference and adversarial checks

`reference` on Cala @3 loads `cala_ppo` and preserves its adaptive supplier allocation,
with a one-day minimum safety-stock buffer for the stronger service objective.
Cala @2 continues to use `availability`. No checkpoint or older task behavior changes.

The public-tool regression bank covers dev seeds **1000-1009**:

- Week-50 starvation through the final decision never improves reference score.
- Blind constant multiplier 0.8 / safety stock 0 scores strictly lower on every seed.
- Reference beats `ss`, `base_stock`, `fixed_lot`, and `fill_adjusted_base_stock`
  on aggregate, as well as each of 14 constant-control combinations: multipliers
  `exp(-0.5), 0.8, 0.9, 1.0, 1.1, 1.2, exp(0.5)` with safety stock 0 or 3 days.
- `do_nothing` and `stop_early` earn exactly zero on all ten seeds.
- Every rollout checks the raw terminal surplus against the certificate before
  checking the clipped score. Separate tests cover large prepaid inventories,
  prepaid freight arriving beyond the horizon, salted deterministic floors,
  read-only grading, budget exhaustion, and incomplete years.

A separate cross-check on the audit's **62000-62019** held-out bank measured:

| Policy | Mean score | Mean service |
| --- | ---: | ---: |
| reference | 0.4443 | 99.21% |
| ss | 0.1479 | 97.84% |
| base_stock | 0.0479 | 97.14% |
| fixed_lot | 0.0208 | 95.75% |
| fill_adjusted_base_stock | 0.1080 | 97.62% |
| blind 0.8 / zero safety stock | 0.0813 | 97.51% |
| reference with week-50 starvation | 0.4237 | 99.21% |

Starvation wins on **0/20** held-out seeds. Reference is not an oracle: `ss`,
`base_stock`, `fill_adjusted_base_stock`, and the blind script each exceed it on
2/20 held-out seeds. Aggregate superiority and the specified strict dev-bank
regressions do not assert universal policy dominance on every possible scenario.
Only Cala @3 golden score values changed.

## Verification

```powershell
uv run --directory engine pytest src/crucible/envs/supplygym/tests tests/policies
uv run --directory engine ruff check src tests
uv run --directory engine pyright
```

Verified on 2026-11-15: pytest **233 passed, 3 xfailed**; Ruff passed; Pyright reported **0 errors, 0 warnings**.

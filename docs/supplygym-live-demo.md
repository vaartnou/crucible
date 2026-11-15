# SupplyGym live demo — development request

[PR 37](https://github.com/vaartnou/crucible/pull/37) implements the spectator feed requested below and introduces SupplyGym v3 upper-bound scoring. The [working viewer and launch instructions](../spectator/supplygym/README.md) use that contract.

We want judges to watch an AI buyer manage one electronics product for a simulated year. The buyer sets purchasing controls once a week. Orders and deliveries take place as the simulator advances. The demo should let a judge answer two questions: “Given what the buyer knew, would I have paid for faster freight?” and “How much money did this strategy generate over the year?”

## What the judges see

The main screen plots **cumulative operating surplus in dollars** against **completed week**. It has five labeled lines: the live agent and four deterministic purchasing strategies — the default (s,S) rule, daily base-stock, fixed-lot, and fill-adjusted base-stock. We can precompute the four baseline runs on the same scenario realization and reveal one point per strategy when the live agent finishes each week. The screen should identify which run is live. Negative values and weeks where the line falls are meaningful.

For the stage demo, the presenter pauses the **display** at one recorded decision while the simulation can keep running. Judges see the on-hand stock, pending units, next reported arrival, and public warning or fill information available when the buyer acted. They make their call. The presenter reveals the buyer’s actual control change, advances one week, and shows the freight cost, units of unmet demand, and surplus earned that week. The viewer can then catch up to the live run.

At year-end, show verified operating surplus and demand served for all five strategies. If the hindsight evaluator has finished, also show the best verified feasible weekly plan it found. An annual result is enough for a labeled endpoint. A hindsight line would require weekly values from replaying that plan. [PR 36 — scenario and SupplyGym hindsight scoring](https://github.com/vaartnou/crucible/pull/36)

## What we would like from development

**The smallest useful addition is a spectator record for every completed week, available while the run is active.** The model can continue using its existing tools. The following field names are suggestions. Existing transcript fields or stable references to them are equally useful.

| Requested data | What it enables |
| --- | --- |
| Run ID, public scenario ID, strategy ID, simulator and scorer versions | Select matching agent and baseline runs. The evaluator should confirm that the runs use the same scenario configuration and realization. A public comparison ID lets the viewer group them while keeping scenario salts private. |
| Completed week and simulated end day | Place the point at the correct checkpoint, including the short final week ending on day 365. |
| Weekly operating surplus and cumulative operating surplus in USD | Draw the line and show the individual week’s contribution when a judge selects a point. |
| A decision or transcript reference linking the pre-decision observation, applied controls and resulting weekly tool response | Explain a point using what the buyer saw, what it did, and what happened next. Reusing the existing observations and ledger keeps the event small. |
| Final run status and verified annual result when available | Mark a completed run as verified, or preserve the last completed point if a run stops early. |

Please compute cumulative surplus from the simulator’s authoritative, unrounded ledger using the annual scorer’s accounting definition. In PR 36, operating surplus is sales revenue less purchases, holding costs, lost-sale penalties, expediting, and switching costs. Weekly surplus is the change in that total across the week. If the definition changes during development, the viewer should receive the new scorer’s values and version. Calculating these values once in the gym avoids rounding drift or accidental use of the separate simulator profit metric.

## Live delivery through Harbor

We’d like to test the simplest delivery path the deployment supports. For example, the runner could append and flush one JSONL record after each successful weekly advance. If the demo host can read that stream during the job, it can also use the saved file for replay. If Harbor makes artifacts available only after completion, a runner-side relay could forward the same records to the viewer. **Live visibility in the intended Harbor deployment still needs to be tested.**

The first integration test is small: start a run and confirm that a separate viewer can display week 1 while the agent is still working on week 2. The useful contract is the weekly record and its timing. The team can choose the transport that fits the deployment.

## How we’ll know it works

- A successful weekly advance produces one chart point per strategy. Status queries and rejected actions produce none. Reconnecting the display can replay records without duplicating a point, using run ID and week as the key.
- Each baseline is revealed only through the live agent’s latest completed week. Selecting a decision shows only information available at that decision until the presenter reveals the outcome.
- A completed 365-day run has 53 weekly points under the current gym rules. Its final cumulative value matches verified annual operating surplus to the cent. An interrupted run ends at its last completed week.
- The saved records reproduce the live graph after the job. If development time is limited, the weekly surplus feed and the live visibility test are the first priorities. The detailed decision view and hindsight overlay can follow.

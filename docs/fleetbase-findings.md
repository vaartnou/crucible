# Fleetbase paired pilot findings

**Evaluation inconclusive: the user stopped the pilot after 15 valid trials.**
All twelve scripted baseline trials and all three model repeats on world 1
completed. The active world-2 model trial was cancelled, and five further model
trials were not started. Missing or cancelled trials are not zero-reward
observations. Only one independent paired world is complete; no overall agent
performance disadvantage has been established.

## Frozen protocol

| Item | Recorded setting |
|---|---|
| Experiment | `fleetbase-native-v1-pilot-b1718743d43d` |
| Candidate source SHA256 | `b1718743d43d3f0e1081ed0352aa0fd7c5988c4455f1120b163eeda8da0f9756` |
| Freeze time | 2026-11-14 22:22:14 Europe/Helsinki (20:22:14 UTC) |
| Environment | `fleetbase_dispatch@1` |
| Independent units | Three fresh paired worlds |
| Model repeats | Three independently initialized conversations per world; nine total |
| Agent | OpenCode 1.19.5, `google/gemini-3.8-flash` |
| Sampling | OpenCode/provider defaults; no sampling seed override recorded |
| Web search | Disabled |
| Public action budget | 128 calls per episode for every operator |
| Runtime | Harbor 0.26.0, sequential external-backend episodes |
| Analysis | `world-means-paired-t-interval-v1` |

The candidate was frozen before the three evaluation worlds were inspected.
Worlds are identified as fresh-world-1, fresh-world-2, and fresh-world-3. Each
world's private salt and exported task are reused across its operators and model
repeats, while each trial starts a separate clean application deployment. The
runner verifies each trial's physical world against its frozen physical-world
hash (`initial_state_sha256` in trial records). That field does not hash a native
database snapshot: native initialization is a clean database/bootstrap followed
by the same three seeded driver/vehicle resources; incidental native IDs differ. Private credentials, salts and resolved model credentials are not
included in this report.

Each world has three vehicles with matching drivers, capacities 2/4/6, twelve
requests in two waves, and a 180-minute horizon. Only released request locations,
loads, deadlines and values are public. Native order creation, assignment and
dispatch require separate operations. One vehicle can physically execute one
order at a time; reassignment is legal before pickup. Actions consume calls but
no virtual time. `advance` reconciles native commitments, moves vehicles at one
distance unit per minute, synchronizes trusted events, and returns settled state.
Stopping, finishing or exhausting the budget continues already dispatched work
to the horizon without creating or dispatching further orders.

Economic utility is on-time realized value minus actual driving distance × 0.2.
Reward is `clip(utility / total generated request value, 0, 1)`. Late deliveries
receive no delivery value. Unsuccessful driving still costs money. Workflow
mistakes carry no extra scoring penalty. Native completed status alone cannot
earn value; only private verified physical events can. The independent queue
worker, scheduler and automatic allocation are disabled. The dedicated offline
installation disables its native HTTP throttle to eliminate the wall-clock
interference discovered during infrastructure validation.

The reference uses the same public tools and observations as the agent. It plans
feasible pickup-and-delivery routes, including terminal deliveries, and chooses
disjoint vehicle routes by economic utility. It verifies each workflow response,
reads back ambiguous operations and advances to the next useful event. The greedy
baseline chooses the nearest profitable feasible request using the same native
workflow. All pilot operators, including the reference, use MCP and begin from
the same public interface; a reference's initial `observe` counts toward its
budget. Do-nothing creates no orders. Stop-early dispatches one order, then
invokes the published continuation. No operator receives an application optimizer.

## Application and dependency pins

| Component | Immutable version |
|---|---|
| Fleetbase source | `14520aa32badf4fd558f76bd90aff6389920b2af` |
| FleetOps API | `0.6.75`, `811aa711a68833dfd8390d8ddac582d4ed75f3c6` |
| Core API | `1.6.74`, `ac97b5620b260660ae4e6fd5ea0e64025629b0c9` |
| Fleetbase image | `fleetbase/fleetbase-api@sha256:75ede1bfd007ce31ac663f5438688baa802a9314399acc30254d4834e346e706` |
| MySQL image | `mysql@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b` |
| Redis image | `redis@sha256:aaf7c123077a5e45ab2328b5ef7e201b5720616efac498d55e65a7afbb96ae20` |

The engine dependency lock, deployment assets, packaged wheel and configuration
fingerprints accompany the private export. Native API behavior and offline
configuration are documented in [fleetbase-api-audit.md](fleetbase-api-audit.md).

## Reference and infrastructure gates

Before freezing, a clean local native development scenario completed all twelve
deliveries with the reference in 50 calls, reward 0.950194 and raw utility
798.162808. A separate clean Harbor oracle also completed twelve deliveries,
reward 0.960759 and utility 827.213334, in a differently salted development world.
An independently reset local reference reproduced identical world, semantic
events and economics while native IDs changed. Active business records,
including payload/tracking children, were zero after every local baseline and
replay; only the bootstrap operator remained. These development worlds are not
included in the pilot. See [fleetbase-validation.md](fleetbase-validation.md).

One earlier development attempt failed because Fleetbase's default HTTP throttle
rejected rapid workflow calls, then interrupted grading and cleanup. Its
`env_error` zero is an infrastructure failure, not an economic observation. The
attempt remains recorded and was followed by a clean rebuild after native rate
limiting was disabled. No pilot outcome may be accepted with an environment
error, cleanup failure, missing evidence or a mismatched frozen world.

## Trial completeness and validity

Final stopped snapshot: **15 / 21 planned valid episodes**. One additional
model attempt was user-cancelled and five were unstarted. Completed pilot
infrastructure failures: 0; retry attempts: 0. Cancelled and unstarted trials
are not economic zeroes. Development failures remain separate from this count.

| World | Reference | Greedy | Do-nothing | Stop-early | Agent repeats 1 / 2 / 3 |
|---|---|---|---|---|---|
| fresh-world-1 | Valid | Valid | Valid | Valid | Valid / Valid / Valid |
| fresh-world-2 | Valid | Valid | Valid | Valid | Cancelled / Unstarted / Unstarted |
| fresh-world-3 | Valid | Valid | Valid | Valid | Unstarted / Unstarted / Unstarted |

No failed pilot attempts or exclusions have been recorded at this snapshot.

## Economic outcomes and paired comparison

The table contains actual verified economic results. Agent repeats remain
separate observations within each world; scripted operators have one trial each.
No missing result is imputed.

| World | Operator | Reward | Raw utility | Generated value |
|---|---|---:|---:|---:|
| fresh-world-1 | Reference | 0.950841 | 810.116370 | 852 |
| fresh-world-1 | Greedy | 0.908526 | 774.063904 | 852 |
| fresh-world-1 | Do-nothing | 0.000000 | 0.000000 | 852 |
| fresh-world-1 | Stop-early | 0.078775 | 67.116204 | 852 |
| fresh-world-1 | Agent / repeat 1 | 0.943744 | 804.070223 | 852 |
| fresh-world-1 | Agent / repeat 2 | 0.940187 | 801.039100 | 852 |
| fresh-world-1 | Agent / repeat 3 | 0.943169 | 803.579988 | 852 |
| fresh-world-2 | Reference | 0.948192 | 814.496740 | 859 |
| fresh-world-2 | Greedy | 0.938784 | 806.415061 | 859 |
| fresh-world-2 | Do-nothing | 0.000000 | 0.000000 | 859 |
| fresh-world-2 | Stop-early | 0.069272 | 59.504994 | 859 |
| fresh-world-3 | Reference | 0.943256 | 798.937749 | 847 |
| fresh-world-3 | Greedy | 0.863144 | 731.082924 | 847 |
| fresh-world-3 | Do-nothing | 0.000000 | 0.000000 | 847 |
| fresh-world-3 | Stop-early | 0.055138 | 46.701515 | 847 |

For world `w`, the paired reward gap is
`reference_reward[w] - mean(agent_reward[w, 1:3])`. The overall paired gap is the
mean of those **three world gaps**, not a nine-sample test. The predeclared
uncertainty interval uses a two-sided Student t interval with two degrees of
freedom. With only three worlds its precision and generalizability are limited.
Reference wins, ties and losses use world-mean gaps and tolerance 1e-9.

The frozen analysis selects **Agent performs worse** only when all three complete
world gaps are positive, the 95% paired reward interval excludes zero, and
operational mistakes are documented in each world. Other complete valid results
select **No demonstrated disadvantage**. Missing/invalid comparisons or reference
quality problems select **Evaluation inconclusive**. This stopped evaluation
selects that conclusion because two paired worlds are incomplete.

| World | Reference reward | Mean agent reward | Paired reward gap | Reference utility | Mean agent utility | Paired utility gap |
|---|---:|---:|---:|---:|---:|---:|
| fresh-world-1 | 0.950841 | 0.942367 | 0.008474 | 810.116370 | 802.896437 | 7.219933 |
| fresh-world-2 | 0.948192 | Pending | Pending | 814.496740 | Pending | Pending |
| fresh-world-3 | 0.943256 | Pending | Pending | 798.937749 | Pending | Pending |

Overall paired gaps, uncertainty and world-level wins/ties/losses are not
estimable from this stopped pilot.

| World | Within-world agent reward SD | Agent reward minimum | Agent reward maximum | Within-world utility SD |
|---|---:|---:|---:|---:|
| fresh-world-1 | 0.001910 | 0.940187 | 0.943744 | 1.627070 |
| fresh-world-2 | Pending | Pending | Pending | Pending |
| fresh-world-3 | Pending | Pending | Pending | Pending |

## Service and workflow outcomes

Actual service outcomes, measured from verified physical records:

| World | Operator | On-time | Late | Unserved | Distance |
|---|---|---:|---:|---:|---:|
| fresh-world-1 | Reference | 12 | 0 | 0 | 209.418150 |
| fresh-world-1 | Greedy | 11 | 0 | 1 | 219.680480 |
| fresh-world-1 | Do-nothing | 0 | 0 | 12 | 0.000000 |
| fresh-world-1 | Stop-early | 1 | 0 | 11 | 19.418979 |
| fresh-world-1 | Agent / repeat 1 | 12 | 0 | 0 | 239.648883 |
| fresh-world-1 | Agent / repeat 2 | 12 | 0 | 0 | 254.804500 |
| fresh-world-1 | Agent / repeat 3 | 12 | 0 | 0 | 242.100062 |
| fresh-world-2 | Reference | 12 | 0 | 0 | 222.516300 |
| fresh-world-2 | Greedy | 12 | 0 | 0 | 262.924697 |
| fresh-world-2 | Do-nothing | 0 | 0 | 12 | 0.000000 |
| fresh-world-2 | Stop-early | 1 | 0 | 11 | 22.475030 |
| fresh-world-3 | Reference | 12 | 0 | 0 | 240.311254 |
| fresh-world-3 | Greedy | 10 | 0 | 2 | 219.585381 |
| fresh-world-3 | Do-nothing | 0 | 0 | 12 | 0.000000 |
| fresh-world-3 | Stop-early | 1 | 0 | 11 | 21.492423 |

| World | Operator | Uncreated | Undispatched | Rejected | Duplicates |
|---|---|---:|---:|---:|---:|
| fresh-world-1 | Reference | 0 | 0 | 0 | 0 |
| fresh-world-1 | Greedy | 1 | 0 | 0 | 0 |
| fresh-world-1 | Do-nothing | 12 | 0 | 0 | 0 |
| fresh-world-1 | Stop-early | 11 | 0 | 0 | 0 |
| fresh-world-1 | Agent / repeat 1 | 0 | 0 | 0 | 0 |
| fresh-world-1 | Agent / repeat 2 | 0 | 0 | 0 | 0 |
| fresh-world-1 | Agent / repeat 3 | 0 | 0 | 0 | 0 |
| fresh-world-2 | Reference | 0 | 0 | 0 | 0 |
| fresh-world-2 | Greedy | 0 | 0 | 0 | 0 |
| fresh-world-2 | Do-nothing | 12 | 0 | 0 | 0 |
| fresh-world-2 | Stop-early | 11 | 0 | 0 | 0 |
| fresh-world-3 | Reference | 0 | 0 | 0 | 0 |
| fresh-world-3 | Greedy | 2 | 0 | 0 | 0 |
| fresh-world-3 | Do-nothing | 12 | 0 | 0 | 0 |
| fresh-world-3 | Stop-early | 11 | 0 | 0 | 0 |

Cancelled and unstarted episodes are absent from these outcome tables, not scored as zero.

## Failure attribution and legal alternatives

### Available world-1 agent trajectories

All three world-1 agent repeats delivered all twelve requests on time, realized all
852 value, and had no uncreated orders, undispatched work, rejected operations or
duplicates. Their deficits relative to the reference are entirely actual
driving costs, not lost delivery value or a workflow penalty:

| Trial | Calls | Distance | Extra distance vs reference | Raw utility gap | Reward gap |
|---|---:|---:|---:|---:|---:|
| World 1, agent repeat 1 | 47 | 239.648883 | 30.230732 | 6.046146 | 0.007096 |
| World 1, agent repeat 2 | 46 | 254.804500 | 45.386350 | 9.077270 | 0.010654 |
| World 1, agent repeat 3 | 46 | 242.100062 | 32.681912 | 6.536382 | 0.007672 |
| World 1, reference | 51 | 209.418150 | 0 | 0 | 0 |

World-1 agent mean reward is 0.9423667101 (within-world sample SD 0.0019097072),
against reference reward 0.9508408098: the paired world-mean reward gap is
0.0084740997. Mean agent utility is 802.896437, giving a paired utility gap of
7.219933. Mean agent distance is 245.517815, or 36.099665 more than the reference.
The service/workflow outcomes remain twelve on-time deliveries and zero errors
for every repeat. This completes one independent paired world, not three.

The agent used fewer calls than the reference in these runs. Calls carry no
artificial reward penalty, so the differences above come exactly from extra
distance × 0.2. World 1 has a complete three-repeat mean, but these differences do not establish
a repeatable three-world disadvantage.

For repeat 1, after the first wave was delivered the public state at minute 60
placed vehicle-1 at [24, 4], vehicle-2 at [24, 5], and vehicle-3 at [5, 10]. The
agent correctly gave request-07, whose pickup was [5, 10], to vehicle-3. It then
assigned request-09 to vehicle-2 (transcript step 28) and request-08 to vehicle-1
(step 31), sending the two eastern vehicles toward pickups [16, 1] and [9, 9].
At step 35 it assigned request-11 to vehicle-3. These concrete vehicle/sequence
choices are associated with the higher realized route cost. Step numbers are
zero-based transcript fields.

For repeat 2, the initial choices included request-01 on vehicle-1 (step 2),
request-02 on vehicle-2 (step 5), and request-05 on vehicle-3 (step 8). Vehicle-2
served request-02 before request-03 (step 16), whereas the reference served those
two requests in the reverse order. In the second wave the agent sent vehicle-1
from [24, 4] to request-07's pickup [5, 10] (step 24). The trace documents the
particular route choices; a cross-operator comparison alone does not isolate the
causal contribution of each individual choice.

Repeat 3 again assigned request-09 to vehicle-2 and request-08 to vehicle-1 at
minute 60 (steps 27 and 30), then request-11 to vehicle-3 (step 34). Its initial
request-01 assignment was vehicle-1 rather than repeat 1's vehicle-2. These
observed route choices accompany its 32.681912 excess distance; no individual
assignment's isolated causal effect has been measured.

### Counterfactual status

A separate scripted native replay has been **prepared but not run** for world-1,
agent repeat 1. It copies the frozen task, replays the original 27 public calls,
checks each complete public observation after normalizing only incidental native
IDs, episode labels and wall-clock record timestamps, then branches to reference
routing using the remaining 128-call budget. The branch is at minute 60 after
request-07 was dispatched and before the additional second-wave commitments.
The original 24-event trusted prefix and branch public-state fingerprint have
been verified during preparation.

A valid native replay must independently verify the unchanged frozen physical
world, original trusted event prefix, semantic public branch state, native tool
legality, final economics and cleanup. Until that run succeeds, the route-choice
statements above are **trajectory associations and cost accounting**, not claimed
causal replay evidence. Any successful intervention will describe its whole
changed continuation; it will not imply that one isolated assignment explains the
entire gain. Replays are explanatory cases, not additional independent worlds.

## Conclusion and fresh-world decision

**Evaluation inconclusive.** The user intentionally stopped further evaluation
before model trials on worlds 2 and 3 completed. This is an incomplete evaluation,
not an infrastructure failure or a finding that the agent performs worse.
The complete world-1 comparison shows a reward gap of 0.008474 in favor of the
reference, entirely from driving cost; every model repeat delivered all twelve
requests without workflow errors. One world cannot establish a repeatable gap.

There were 15 valid completed pilot trials, one user-cancelled model trial, and
five unstarted model trials. No completed pilot trial required a retry or had an
infrastructure failure. The earlier development throttle failure is retained
separately in the infrastructure validation report. Raw private run artifacts
remain local; sanitized completed outcomes are in
[fleetbase-pilot-results.json](fleetbase-pilot-results.json).

The native causal replay was prepared but not run. A larger study was not
launched. No benchmark difficulty or scoring was changed in response to results.
Any later continuation must retain the frozen candidate and record cancellations;
a changed candidate requires a new experiment identity and fresh worlds.

This benchmark covers the native API order workflow, not Fleetbase's UI or an
automatic optimizer. Physical time, distance and capacity follow the published
simulator geometry and operational observations. Native map coordinates and
preliminary route estimates are a display mapping, not audited real-road
logistics or economics. The observed model deficits concern routing costs,
not demonstrated CRUD or general Fleetbase workflow incompetence.

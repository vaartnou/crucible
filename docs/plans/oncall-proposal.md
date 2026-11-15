
Plan: oncall@1, a hard on-call gym in Crucible
Status: proposal, 2026-11-15. Nothing is implemented. Replaces the earlier draft of this file. The dispatch v3 work is parked in runs/fleetbase-v3-wip.patch (gitignored).

1. Goal
One environment, oncall@1: a simulated production system of 20–30 services has an incident, and the agent must restore service and name the cause. It runs through Harbor.

"Hard" means a measured gap, not a feeling. On 10 salted seeds:

Gate	Target
do_nothing	0
loudest_alert bot (acts like a typical LLM: fixes the service that shouts loudest)	≤ 0.40
graph_walk bot (simple rule: follow the call graph to the deepest unhealthy service, revert its newest change, verify)	≥ 0.75
reference (graph walk plus fault-specific checks)	≥ 0.85
Gemini 3.8 Flash and 3.1 Pro through Harbor	≥ 0.20 below graph_walk on average and on 7 of 10 seeds
Exploits (shotgun, restart_all, rollback_all)	below loudest_alert, never crash the grader
The graph_walk gate stands in for "a person who knows SRE". Every world must be solvable from the public signals by a written rule (section 6.7). If the LLM loses, it loses on reasoning, not on missing information.

2. Decisions
Decision	Choice	Reason
Real cluster or simulator	Simulator, and it stays one	Deterministic scores, episodes in seconds, full control of cascades. The SRE benchmarks that use real clusters (SWEOps-Bench, AIOpsLab, SREGym) pay for it: SWEOps ran one trial per case on a 64 GiB VM, 4 tasks at a time
Where	Crucible, used the light way	crucible export harbor already gives the sidecar, the grade token, test.sh and the oracle. The in-process loop runs 50 seeds in seconds. Skip golden scores and validation tables until the gates pass
Task ID	oncall@1, new	A clean start. incident@1 is deleted with the other environments (section 4)
Simulation granularity	A fluid model per simulated minute, not single requests	Fast (milliseconds per minute for 30 services), deterministic, and enough for latency, errors, retries and saturation
Telemetry	Derived on demand from one state history and one clock	Logs, metrics, traces and alerts cannot disagree with each other. Incoherent synthetic telemetry is a known failure ("The Temporal Coherence Problem": RCA accuracy 100 % → 40 %)
3. One PR, four parts
Branch from main. Each part is its own commit with the test suite green.

Part	Content	Section
A	Remove all other environments and the application-backend machinery	4
B	Harbor improvements in the exporter and the loop	5
C	oncall@1 thin slice: 8 services, 2 faults, through Harbor end to end	6, 9 (M2)
D	oncall@1 hard: generated topology, full fault catalogue, bots, tuning to the gates	6, 9 (M3–M5)
If D takes longer than planned, the PR can merge after C, with D as a second PR.

4. Part A: remove the other environments
Delete:

engine/src/crucible/envs/fleetbase_dispatch/, backend_probe/, incident/ and their entry points in engine/pyproject.toml.
Application-backend machinery that only those environments use: engine/src/crucible/backends/, engine/src/crucible/export/deployment.py, the deployment_bundle branch in export/harbor.py, Task.deployment_bundle and Task.external_backend, the matching code in core/executor.py and cli.py, and their tests (tests/backends/, tests/export/test_deployment.py).
Docs: docs/fleetbase-*.md, docs/fleetbase-native-proof.json, docs/backend-runtime.md, docs/deployment-bundles.md, docs/plans/fleetbase-*.md, docs/archive/fleetbase-*, the fleetbase and incident@1 parts of docs/harbor-runs.md, spikes/hello-mcp-ns/.
Entries for removed tasks in engine/tests/policies/golden_scores.json.
Keep guess@1, but move it out of the shipped package to engine/tests/fixtures/guess/. About 54 references in the core tests (registry, session, executor, jobs, server, export) use it as a small known task. Tests register it with register_task. Then the wheel ships only oncall@1.

Update README.md (featured environment becomes oncall@1), docs/concepts.md, docs/using-environments.md, docs/adding-an-environment.md and docs/decisions.md. Git history keeps everything removed; the incident@1 and fleetbase numbers stay there (last commit 323d4fc).

Exit check: pytest, pyright and the pre-commit hooks pass; crucible tasks lists nothing until part C adds oncall@1.

5. Part B: Harbor improvements
From the exporter (engine/src/crucible/export/harbor.py and its templates) and our runs.

#	Change	Why	Effort
B1	test.sh writes reward.json with named numbers (reward, impact, verified, diagnosis, penalty), not only reward.txt	harbor view and harbor analyze then show why a run scored what it did. First check which key Harbor 0.26 uses as the main reward	small
B2	Build the sidecar image once per export (crucible-sidecar:<wheel sha>); compose uses image: instead of build:	Today every seed folder builds its own image. One build for all seeds	small
B3	Fast oracle: a /autoplay endpoint on the sidecar, gated by a solution token that exists only in [solution.env]; it plays the reference in process. solve.sh becomes one curl	Today solve.sh downloads uv from the internet and installs the wheel on every oracle run. The MCP path stays tested by crucible play --mcp in the test suite	small
B4	crucible regrade <job folder>: replay the action list from each trial's transcript.json in process, against the same world (seed and salt from the export folder)	A change to the grader or the weights rescores old agent runs in seconds, with no new agent runs. After a simulator change it shows where a replay diverges	medium
B5	crucible agent: a small tool-calling loop for Gemini (and other OpenAI-compatible APIs) straight against the task in process, 10 seeds in parallel. Start from agents/remote.py	A filter before Harbor: minutes per batch, no Docker. Harbor with OpenCode stays the number we report	medium
B6	Task schema 1.4 in task.toml; run harbor lint in the export test	Keep up with Harbor 0.26	small
B7	Try the agent network allowlist again: remove expose: from the sidecar (not needed inside the compose network), allowlist only the model API host and mcp-server	Closes the open integrity gap (docs/reward-integrity.md). If it still fails, keep the current mitigations and note it	small, may fail
B8	A failure rubric for harbor analyze in the repo (chased a downstream alert, no verification, wrong rollback, repeated restart, read too long)	Turns 30 transcripts into a table of failure modes in minutes	small
B9	Run with -n 8 after raising Docker Desktop memory to about 32 GB (now 7.75 GB of 48 GB)	Twice the current concurrency	none
Grading stays in the sidecar (/grade), because the episode state lives there. A separate verifier environment gives nothing here. Harbor multi-step tasks ([[steps]]) are a later option, not part of this PR.

6. Parts C and D: the oncall@1 design
6.1 Layers
world.py     seeded, salted: topology, workload, change history, faults, herrings
model.py     fluid traffic model: one tick = one simulated minute
faults.py    the fault catalogue: modifiers on the model, their fixes and side effects
telemetry.py metrics, logs, traces, alerts, events, built from the state history
commands.py  the command parser and real-looking output (kubectl-style tables)
task.py      MCP tools, the clock, the episode budget, the instruction
grader.py    impact, verified recovery, diagnosis, penalties
policies/    do_nothing, shotgun, loudest_alert, graph_walk, reference, exploits
telemetry.py and commands.py never read the fault list, only the state that the model produced. That is how "no tool names the cause" is enforced in code, and the generic no-hidden-state check tests it.

6.2 Service model
Service   name, kind (http | worker | cache | db | queue | external), owner team,
          deployment (replicas, cpu and memory limits, version), config map (versioned),
          dependencies (edges)
Pod       id (checkout-7f9c4-x2k1p), node, version, state (Running | CrashLoopBackOff |
          OOMKilled | Evicted | Pending), memory, restarts
Edge      caller -> callee, sync or async, timeout, retries, connection-pool size
Change    id, minute, kind (deploy | config | flag | scale), service, author, diff summary
Node      name, disk, memory (only for disk-pressure and eviction faults)
Topology comes from templates (storefront, payments, search, auth, notifications) joined by a gateway, with 20–30 services in total. Names, versions, sizes and edges vary per seed.

6.3 Traffic model (fluid)
Each simulated minute, in reverse topological order (callees first):

Demand at the entry points follows a daily curve plus noise.
Each service gets the request rate from its callers, multiplied by retries: load = base × (1 + retries × error_rate_of_this_call). This produces retry storms.
Per pod: utilisation = load / capacity; latency grows like base / (1 − utilisation), capped. Over capacity, requests queue, then time out.
Error rate per pod = its own fault effects + calls that fail or time out (callee latency above the edge timeout counts as an error).
Connection pools: if concurrency = rate × latency is above the pool size, the excess fails at the caller, while the callee looks healthy.
Pod states change: memory grows with leaks, OOM kills, restarts, evictions.
Retries make load depend on errors and errors depend on load, so step 2–4 iterate a few times per tick until the numbers settle. Each tick stores per-pod numbers: request rate, error rate, p50 and p99 latency, CPU, memory, pool in use, queue depth. Thirty services × 3 pods × 150 minutes is small enough to keep in memory.

6.4 Faults
A fault is a small object: when it starts, which model values it changes, which action fixes it, and what side effects that action and the wrong actions have.

Fault	Changes in the model	Right fix	Trap
Partial rollout	1 of 3 pods on a bad version with 60 % errors	roll back the deployment	aggregated error rate is about 20 %; logs from a random pod look fine
Config push	an edge timeout cut from 2000 to 200 ms	revert the config version	no deploy event; only config history shows it
Memory leak	memory grows on the new version, OOM kill loop	roll back	a restart clears it for 20–40 minutes, then it returns
Pool exhaustion	pool size too small after a scale-up of callers	raise the pool or undo the scale	the database looks healthy; callers time out
Retry storm	a slow callee plus aggressive retries	fix the slow callee	the loudest alerts are downstream
Cache stampede	a cache restart or flush sends all reads to the database	wait or warm, rate-limit	caused by the agent's own restart of the cache
Certificate expiry	TLS to one dependency fails	rotate the certificate	appears as connection errors in one caller only
Disk pressure	a node's disk fills and pods are evicted	clean the node, cordon	pods move and fail on other nodes
Queue backlog	a consumer crash	restart or roll back the consumer	no errors at first, only latency of async work
Bad feature flag	a flag turns on a slow code path for 30 % of requests	turn off the flag	flags are in a separate store
External outage	a third-party dependency is down	enable the fallback flag, do not restart	restarts do nothing and cost capacity
HPA at maximum	the autoscaler is capped	raise the cap	scaling looks right but the cap holds
Herrings (1–3 per episode): an unrelated deploy just before the fault, a service that logs errors every day, a slow query that is normal at this hour, a pod that restarted once.

Episodes have one fault (part C) or one to two faults (part D). With two, fixing the first can reveal the second.

6.5 Telemetry
All of it is a function of the stored history, the clock and a seeded RNG keyed by (world salt, pod, minute). Nothing is stored twice, and the same question gives the same answer.

Metrics: the per-pod history. Queries aggregate by service or by pod, over a window.
Logs: generated lazily when read. Per pod and minute, a number of lines from templates by service kind and condition: normal INFO lines, routine WARN noise, and evidence lines whose rate follows the model (for example upstream timeout after 200ms calling payments-api on callers, pool exhausted: 20/20 active on the pool owner, a stack trace on the bad version). Evidence appears only on the pods that have the problem. Logs are searchable by text and level, and come 20 lines per page, newest first.
Traces: built from the call graph for a sample of requests; the failing or slow span is the deepest one that has the problem.
Alerts: rules over the metrics (error ratio above x for 5 minutes, p99 above the SLO, restarts, disk). User-facing services have more severe alerts, so the loudest alerts are often downstream of the cause. Herring alerts are always present.
Events: Kubernetes-style events (OOMKilled, BackOff, Evicted, ScalingReplicaSet) from pod state changes.
Change log: deploys, config versions and flag changes with ID, time and author.
6.6 Tools
Tool	Examples	Cost (simulated minutes)
run(command)	kubectl get pods -n shop, kubectl describe pod checkout-7f9c4-x2k1p, kubectl logs <pod> --since=10m, kubectl rollout history deploy/checkout, kubectl rollout undo deploy/checkout, kubectl scale deploy/x --replicas=5, kubectl get events, config history checkout, config revert checkout <version>, flags list, flags set <flag> off	reads 1, actions 1–5
query_metrics(metric, service, by, window)	error_ratio, p99_latency, request_rate, memory, pool_in_use, queue_depth; by = service | pod	1
search_logs(service, query, level, since, page)	text search across one service's pods	1 per page
get_trace(request_id) / sample_traces(service, status)	one trace, or a few IDs	2
list_alerts(), get_alert(id)	the pager	1
wait(minutes)	let time pass	1–30
resolve(report)	closes the incident (see 6.8)	ends the episode
Output looks like the real tools:

$ kubectl get pods -n shop -l app=checkout
NAME                       READY   STATUS    RESTARTS   AGE
checkout-7f9c4-x2k1p       1/1     Running   0          3h12m
checkout-7f9c4-q8d2w       1/1     Running   0          3h12m
checkout-8a1e7-m4n7z       1/1     Running   0          6m
Start with a fixed list of commands and a strict parser that answers like kubectl (error: unknown command). query_metrics is a structured tool first; a small PromQL subset is optional later.

Time moves only on tool calls, so model speed does not change the score. The episode has a call budget (about 80) and a horizon (about 150 simulated minutes).

6.7 Why the LLM loses and a rule wins
Mechanism	What a typical LLM does	What the rule does
Loudest alerts are downstream	restarts or rolls back the frontend or the gateway	walks call edges to the deepest unhealthy service whose callees are healthy
Evidence on 1 of 3 pods	reads a random pod's logs, sees nothing, moves on	always splits error ratio by = pod
Unrelated deploy just before the fault	rolls it back (penalty)	picks the change on the culprit service closest to the onset minute
Restart fixes the leak for 30 minutes	calls it fixed and resolves	waits and checks the memory slope before resolve
Agent-caused stampede	restarts the cache as a "safe" first step	never restarts a stateful service without evidence
Every read costs a minute	reads 20–40 things before acting	reads a fixed small set, then acts
Two faults	stops after the first fix	verifies all SLOs green for 15 minutes
The left column is a hypothesis. M4 checks it against real Flash and Pro transcripts.

Solvability filter. The world generator rejects a world unless graph_walk (using only public tools) finds the root cause service and the triggering change. This keeps every world fair, and it is the evidence for the claim "an expert or a simple rule solves it". About 5–20 % of generated worlds should be rejected; if far more are, the faults are too subtle.

6.8 Scoring
Deterministic, no LLM judge. Weights in scoring.yaml.

Impact: failed and slow requests during the episode, scaled between do-nothing (0) and the oracle (1), as in the old incident@1.
Verified recovery: after resolve, the grader runs the model for 30 more simulated minutes. A fault that returns (a restarted leak) or a residual anomaly (one bad pod left, a second fault) adds its failed requests to impact and sets verified = 0.
Diagnosis: resolve(report) takes structured fields: service, fault_kind (from a fixed list), change_id, remediation. Graded per field against the world.
Penalties: wrong actions (a rollback of a healthy service, a restart of a healthy database), actions that cause a new fault (the stampede), extra capacity minutes.
reward = clamp(impact × (0.6 + 0.2 × verified + 0.2 × diagnosis) − penalty, 0, 1). A first guess, tuned in M3.
6.9 Policies
Policy	Behaviour	Purpose
do_nothing	resolve at once with an empty report	0
shotgun	restarts and rolls back everything	exploit
restart_all, rollback_all, guess_report	single-trick exploits	exploits
loudest_alert	the most severe alert's service: read its logs, restart, then roll back its newest deploy	proxy for typical LLM behaviour; check against real Flash transcripts
graph_walk	the rule in 6.7, about 50 lines	the "expert" gate
reference	graph_walk plus per-fault checks and safe repairs	gates and the Harbor oracle
oracle bound	the right fix at fault onset, from the hidden world; scripts only	the score of 1
7. Feedback loop
Loop	Tool	Time	Used for
1	crucible eval on 50 seeds, all bots	seconds	world and fault tuning to the bot gates
2	crucible regrade on old Harbor jobs (B4)	seconds	grader and weight changes
3	crucible agent with Flash on 10 seeds (B5)	minutes	quick check that the LLM still loses
4	Harbor, OpenCode, Flash and Pro, -n 8, -k 3, then harbor analyze (B8)	10–30 min	the reported numbers
Rule: loop 4 only after loop 3 shows the gap. Episodes are about 40–80 calls, so a Harbor batch takes minutes per trial, not the 15 minutes of dispatch.

8. Demo
Replay viewer (a single HTML page): reads a world and a transcript. It shows the service map with health colours, alerts as they fire, the agent's reads and actions on a timeline, and a counter of failed requests.
Split screen: the Flash run and the graph_walk run on the same world, side by side by simulated minute. The intended story: Flash chases the loud frontend alert and restarts the cache; the rule walks to payments, reverts config v14, done in 9 minutes.
Scoreboard: 10 seeds × (do-nothing, loudest alert, Flash, Pro, graph walk, reference).
Fallback with no extra work: harbor view on the job folder.
9. Milestones
Milestone	Work	Exit check	Rough time
M1	Part A (removal) and part B (B1, B2, B3, B6, B7, B9)	suite green; exporter tests pass with the guess fixture	half a day
M2	Part C thin slice: fixed 8-service topology, partial rollout and config push, 1 herring, run / query_metrics / search_logs / list_alerts / wait / resolve, grader, do_nothing, loudest_alert, reference. Export, Harbor oracle and nop, one Flash run	Harbor oracle ≈ reference, nop = 0, Flash run completes	1 day
M3	B4 and B5; generated topology of 20–30 services; leak, pool exhaustion, retry storm, stampede; graph_walk, exploits, solvability filter; tune	bot gates on 50 seeds	1–2 days
M4	10 Flash runs with crucible agent, then Harbor with Flash and Pro; harbor analyze	agent gate on 10 salted seeds	half a day
M5	Rest of the fault catalogue, two-fault episodes; repeat M3–M4	gates still pass	1 day
M6	Replay viewer, split screen, scoreboard	demo runs end to end	1 day
Stop point: if after M4 Flash is not clearly below graph_walk, stop and read the transcripts before M5. The likely fix is in the world (more noise, more scale), not in the agent.

Cut order if time runs out: M5 first (keep 6 faults), then B5, then the split screen (keep the scoreboard and harbor view).

10. Risks
Risk	Mitigation
Flash also does a graph walk and closes the gap	Scale (30 services make every check cost minutes), evidence on single pods, delayed recurrence; measure in M4 before more work
The bots are weak or strong proxies for the LLM	Check loudest_alert against real Flash transcripts in M4; tune it to match them, not to make the gap look good
The sim is regular and the instruction teaches it	No runbook in the instruction; names, sizes and topology vary per seed; herrings that look like faults
Command output is large and costs tokens	Pagination, output caps, a fixed column set
The agent writes its own graph walk in the shell	Allowed: same tools. It is the skill we want to measure
The agent has internet in Harbor	B7; otherwise salt and the private token, as today
Removing incident@1 loses the featured result	Its numbers and code stay in git history; oncall@1 becomes the featured environment
11. Open questions
Is deleting incident@1 in this PR OK, or should it stay until oncall@1 passes its gates?
Demo audience and length (hackathon judges, 3 minutes?).
Which models count for the agent gate: Flash and Pro only, or also a Claude or GPT model through OpenCode?

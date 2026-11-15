# Incident response runbook

This workflow uses only the tools available to the evaluated agent. It does not use a seed,
hidden cause, verifier endpoint, or simulator source. The executable `incident_runbook` policy
implements a narrow version of this workflow: a memory-trend preflight followed by the existing
causal graph walk and recovery checks. It is a deterministic rules baseline, not a model with
this document in its context.

1. Establish user impact and scope. Inspect ingress errors and latency, the dependency graph,
   and recent changes. Keep the sample minute with each observation.
2. Check developing failures as well as current errors. Compare recent memory trends, including
   pod-level values when the service average obscures a canary. A positive slope after a release
   can explain a failure that has not yet reached its memory limit.
3. Trace failing calls through the graph. Distinguish an unhealthy callee from a caller whose
   deadline or connection pool rejects otherwise healthy responses. Use offered versus admitted
   attempt rates to identify retry amplification and pool rejection.
4. Match each hypothesis to evidence. Inspect the candidate service's actual configuration and
   rollout history. Record the revision, runtime symptom, and expected effect of reversing it.
   A later unrelated change does not establish causality.
5. Make a targeted repair, then inspect the resulting system. Track which symptoms disappear,
   remain, or newly emerge. Revisit the investigation when residual impact has a different cause.
6. Verify global errors and latency, memory growth, and queue drainage. A falling queue can need
   time to drain; another restart does not erase its work. A restart of a leaking build can give
   temporary relief without fixing the regression.
7. Close with one diagnosis for each supported independent cause. In `oncall@2`, submit
   `resolve(reports=[{service, fault_kind, change_id, remediation}, ...])`. Reports do not perform
   repairs, and closure triggers an additional thirty-minute recovery probe.

To compare an agent supplied with this runbook against the standalone agent, use Harbor's
`--extra-instruction-path docs/oncall-runbook.md` on the same original export. Keep the model,
OpenCode version, budgets, and simulator fixed, and label this as a separate experimental arm.
No model-plus-runbook result is implied by the deterministic policy's score.

# On-call transcript review rubric

Use this rubric with `harbor analyze` on the job folder. Label only behavior visible in the transcript. A low reward alone does not establish a failure mode. Include trial ID, seed, model, first supporting call index, quoted evidence, and confidence. Multiple labels may apply.

| Label | Evidence required |
| --- | --- |
| chased_downstream_alert | Repaired a caller while public metrics or traces identified an unhealthy dependency; identify both services. |
| no_verification | Resolved after repair without a later health check or observation window. Distinguish a missing check from a check that falsely suggested recovery. |
| wrong_rollback | Reverted a change on a healthy service or an unrelated change; cite history and symptom timing. |
| repeated_restart | Restarted the same service repeatedly despite unchanged or recurrent symptoms. |
| read_too_long | Spent enough simulated minutes reading that impact was materially worse; report time to first correct action and compare the same-world graph_walk replay. |
| partial_pod_missed | Used aggregate or single-pod evidence to dismiss a service despite public evidence of an unhealthy sibling pod. |
| recurrence_missed | Resolved during temporary recovery and the private verifier observed recurrence; distinguish verifier evidence from what the agent saw. |
| second_fault_missed | Resolved with an independently unhealthy branch still visible. |
| stateful_restart_damage | Restarted a cache/database and subsequent public history shows new damage. |
| environment_or_transport_error | Tool/API failure prevented evaluation; report separately from reasoning failures. |
| inconclusive | Transcript lacks enough evidence for the other labels. |

Compare the agent and graph_walk on exactly the same seed **and salt**. Report reward, impact, verified, diagnosis, penalty, action count, and simulated time to first correct repair. Never infer a root cause from a service name alone. Keep model/API errors out of the reasoning-gap denominator and report their count separately.

The report must include counterexamples: runs where the model outperformed graph_walk, or followed the graph successfully. Check whether loudest_alert actually resembles observed model behavior before using it as a proxy. Do not change the world to penalize a correct solution found by a model.

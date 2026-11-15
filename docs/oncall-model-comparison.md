# Matched oncall model comparison

Measured 15 November 2026 on the same private ten-world `oncall@2` export. All models use OpenCode 1.19.5 in Harbor/Docker, identical public tools, eighty calls, and the same simulated clock and scoring. The two new model batches ran in parallel, with one active trial per model. Native model reasoning defaults remain in effect.

| Model / workflow | Valid worlds | Overall reward | Recovery impact | Verified recovery | Fully solved |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Gemini 3.1 Pro Preview](oncall-overlap-pro31-measurements.json) | 10/10 | 0.784 | 0.879 | 10/10 | 9/10 |
| [Gemini 3.8 Flash](oncall-overlap-measurements.json) | 10/10 | 0.672 | 0.686 | 10/10 | 9/10 |
| [Gemini 3.5 Flash-Lite](oncall-overlap-flashlite35-measurements.json) | 10/10 | 0.430 | 0.527 | 9/10 | 5/10 |
| Incident runbook (deterministic rules) | 10/10 | 0.872 | 0.872 | 10/10 | 10/10 |

Recovery impact is avoided failed requests relative to each world's no-action trajectory. Fully solved requires both verified recovery and complete correct diagnosis. Overall reward also includes diagnosis credit and action penalties. Valid zero scores are included; no provider or environment failures occurred in these thirty valid model trials.

## Interpretation

Pro restores service in every world and has slightly higher mean recovery impact than the runbook in this sample (0.879 versus 0.872). Its lower overall reward comes from action penalties and one incorrect diagnosis. The mean action penalty is 0.084; one trial reports six causes for two actual faults. The runbook has higher overall reward, but this does not establish that it restores service better than Pro.

The runbook-minus-model mean gaps are 0.089 for Pro, 0.200 for Flash, and 0.443 for Flash-Lite. The historical difficulty gate requires a mean gap of at least 0.20 and at least seven of ten individual gaps of at least 0.20. Pro reaches that per-world gap on three worlds, Flash on four, and Flash-Lite on nine. Only Flash-Lite passes that gate. The current scenario therefore does not demonstrate the required large gap against Pro.

The two new batches took 41m 22s (Pro) and 20m 53s (Flash-Lite), including setup, while running concurrently. Wall-clock latency is separate from the deterministic simulated clock.

## Measurement boundaries

The comparison verifies source directly from the exported wheel, world salt digests, model/agent identities, and replay with an explicit 1e-12 numeric tolerance for Linux/macOS roundoff. All thirty valid model transcripts reproduce their rewards exactly and observations within that tolerance. No simulator or scoring changes were made for these additional model runs.

This is one attempt per world in a narrow family that always includes a memory leak. The ten primary triggers cover five types: one config push, three memory leaks, one partial rollout, two pool exhaustions, and three retry storms. No queue-backlog primary trigger occurs in this sample. These are screening results, not a broad model ranking or evidence of a general production incident-response advantage.

The runbook is a deterministic policy using public tools, not a model supplied with a runbook prompt. Only the configured Google credential was used; no cross-provider comparison was run.

See [scenario design and reproduction commands](oncall-overlap.md) and the linked machine-readable reports for per-world scores and replay evidence.

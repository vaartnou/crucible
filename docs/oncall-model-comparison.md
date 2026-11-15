# Matched oncall model comparison

Measured 15 November 2026 on the same private ten-world `oncall@2` export. All models use OpenCode 1.19.5 in Harbor/Docker, identical public tools, eighty calls, and the same simulated clock and scoring. Native model reasoning defaults remain in effect. Pro and Flash-Lite ran in parallel with one active trial per model; Hydrogen used four concurrent workers after an initial two-worker start.

| Model / workflow | Valid worlds | Overall reward | Recovery impact | Verified recovery | Fully solved |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Gemini 4 Hydrogen](oncall-overlap-hydrogen4-measurements.json) | 10/10 | 0.733 | 0.744 | 10/10 | 9/10 |
| [Gemini 3.1 Pro Preview](oncall-overlap-pro31-measurements.json) | 10/10 | 0.784 | 0.879 | 10/10 | 9/10 |
| [Gemini 3.8 Flash](oncall-overlap-measurements.json) | 10/10 | 0.672 | 0.686 | 10/10 | 9/10 |
| [Gemini 3.5 Flash-Lite](oncall-overlap-flashlite35-measurements.json) | 10/10 | 0.430 | 0.527 | 9/10 | 5/10 |
| Incident runbook (deterministic rules) | 10/10 | 0.872 | 0.872 | 10/10 | 10/10 |

Recovery impact is avoided failed requests relative to each world's no-action trajectory. Fully solved requires both verified recovery and complete correct diagnosis. Overall reward also includes diagnosis credit and action penalties. Valid zero scores are included. No provider or environment failures occurred in these forty scored model trials; two administratively interrupted Hydrogen attempts are explicitly excluded.

## Interpretation

Hydrogen averages 0.733 overall versus the runbook's 0.872, a mean gap of 0.140. The runbook scores higher on all ten worlds. Hydrogen restores service in every world and completes the correct diagnosis in nine. Its mean recovery impact is 0.744, mean action penalty 0.007, and mean tool-call count 52.9. Most of its gap comes from failed requests before repair, rather than action penalties or incorrect diagnoses.

Pro restores service in every world and has slightly higher mean recovery impact than the runbook in this sample (0.879 versus 0.872). Its lower overall reward comes from action penalties and one incorrect diagnosis. The mean action penalty is 0.084; one trial reports six causes for two actual faults. The runbook has higher overall reward, but this does not establish that it restores service better than Pro.

The runbook-minus-model mean gaps are 0.140 for Hydrogen, 0.089 for Pro, 0.200 for Flash, and 0.443 for Flash-Lite. The historical difficulty gate requires a mean gap of at least 0.20 and at least seven of ten individual gaps of at least 0.20. Hydrogen reaches that per-world gap on one world, Pro on three, Flash on four, and Flash-Lite on nine. Only Flash-Lite passes that gate. The current scenario therefore does not demonstrate the required large gap against Hydrogen or Pro.

## Concurrency and interrupted attempts

Hydrogen began with two concurrent workers. When the user requested higher concurrency, one completed world (seed 0) was preserved, two in-flight attempts were administratively cancelled, and the remaining nine worlds ran with four workers. The machine-readable Hydrogen report retains both CancelledError exclusions. No cancelled attempt is counted as an agent failure, and every scored seed appears exactly once. Docker had about 7.75 GiB allocated; measured per-trial memory made all ten trials at once unsuitable.

The nine-world, four-worker Hydrogen batch took 7m 7s, in addition to the initial completed world and restart. Pro and Flash-Lite took 41m 22s and 20m 53s including setup while running concurrently. Wall-clock latency is separate from the deterministic simulated clock, so these durations are not a controlled latency ranking.

## Measurement boundaries

The comparison verifies source directly from the exported wheel, world salt digests, model/agent identities, and replay with an explicit 1e-12 numeric tolerance for Linux/macOS roundoff. All forty scored model transcripts reproduce their rewards exactly and observations within that tolerance. No simulator or scoring changes were made for these additional model runs.

This is one scored attempt per world in a narrow family that always includes a memory leak. The ten primary triggers cover five types: one config push, three memory leaks, one partial rollout, two pool exhaustions, and three retry storms. No queue-backlog primary trigger occurs in this sample. These are screening results, not a broad model ranking or evidence of a general production incident-response advantage.

The runbook is a deterministic policy using public tools, not a model supplied with a runbook prompt. Only the configured Google credential was used; no cross-provider comparison was run.

See [scenario design and reproduction commands](oncall-overlap.md) and the linked machine-readable reports for per-world scores and replay evidence.

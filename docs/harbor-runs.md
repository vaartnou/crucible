# Harbor runs

Runs of the generated `incident@1` tasks (`crucible export harbor --task incident@1 --seeds 0-9`) on a laptop with local Docker. Harbor 0.26.0, OpenCode 1.19.5. The job folders are in `jobs/` (gitignored); this file keeps the scores.

Date: 2026-11-14. Engine commit: `44f0dca` plus the step 7 export.

## Summary

| Agent | Model | Job | Tasks | Mean reward | Root cause correct |
|---|---|---|---|---|---|
| oracle (`solve.sh`, reference policy) | none | `incident-1-oracle` | 10 | 0.971 | 10/10 |
| opencode | `google/gemini-3.8-flash` | `incident-1-opencode-gemini` | 3 | 0.955 | 3/3 |
| nop | none | `incident-1-nop` | 3 | 0.000 | 0/3 |

The oracle reward is equal to the in-process reference score (`crucible eval --task incident@1 --policy reference --seeds 0-9`) on each seed, to all printed digits. This held before exports were salted; since the per-task `SIDECAR_SALT`, a Harbor world differs from the in-process world of the same seed.

## Per trial

| Job | Seed | Reward | Steps | Stop reason | Root cause correct |
|---|---|---|---|---|---|
| incident-1-oracle | 0 | 0.935 | 8 | task_done | yes |
| incident-1-oracle | 1 | 0.983 | 7 | task_done | yes |
| incident-1-oracle | 2 | 0.977 | 6 | task_done | yes |
| incident-1-oracle | 3 | 0.976 | 6 | task_done | yes |
| incident-1-oracle | 4 | 0.977 | 6 | task_done | yes |
| incident-1-oracle | 5 | 0.958 | 7 | task_done | yes |
| incident-1-oracle | 6 | 0.980 | 6 | task_done | yes |
| incident-1-oracle | 7 | 0.962 | 7 | task_done | yes |
| incident-1-oracle | 8 | 0.981 | 7 | task_done | yes |
| incident-1-oracle | 9 | 0.981 | 8 | task_done | yes |
| incident-1-opencode-gemini | 0 | 0.918 | 13 | task_done | yes |
| incident-1-opencode-gemini | 1 | 0.983 | 13 | task_done | yes |
| incident-1-opencode-gemini | 2 | 0.965 | 10 | task_done | yes |
| incident-1-nop | 0 | 0.000 | 0 | policy_stop | no |
| incident-1-nop | 1 | 0.000 | 0 | policy_stop | no |
| incident-1-nop | 2 | 0.000 | 0 | policy_stop | no |

## Observations

- OpenCode used only the MCP tools (`mcp-server_*`). There were no shell or web calls. The grade token and `/grade` do not appear in its logs.
- Gemini 3.8 Flash scores close to the reference. It does not score above it: the reward is mostly set by how soon the repair starts (see `docs/decisions.md`, calibration finding), and Gemini made 3–6 more calls than the reference. With only a small gap between the reference and the model, this task cannot separate strong models well. Faults 4–6 should be harder.
- `[agent] network_mode = "allowlist"` with `allowed_hosts` fails at startup in Harbor 0.26.0. The error is `conflicting options: port exposing and the container type network mode` on the sidecar. Job `incident-1-opencode-allowed-hosts`.

## Re-run after the `crucible` rename

Date: 2026-11-14. Engine: commit `0667420` plus the uncommitted rename and `envs/` move (the
exported `task.toml` records `0667420`). Export: `crucible export harbor --task incident@1 --seeds 0-9`.

| Job | Agent | Tasks | Mean reward | Root cause correct | Note |
|---|---|---|---|---|---|
| `crucible-incident-1-oracle` | oracle | 10 | 0.971 | 10/10 | equal to the golden reference score on every seed, all digits |
| `crucible-incident-1-opencode-gemini` | opencode, `google/gemini-3.8-flash` | 1 (seed 0) | 0.918 | 1/1 | 12 steps, `task_done`, MCP tools only |

## Commands

```bash
export PATH=$PATH:/Applications/Docker.app/Contents/Resources/bin
uv run --directory engine crucible export harbor --task incident@1 --seeds 0-9 --out ../harbor/incident-1
harbor run -p harbor/incident-1 -a oracle -o jobs --job-name incident-1-oracle
harbor run -p harbor/incident-1 -a nop -o jobs --job-name incident-1-nop -l 3
set -a; . ./.env; set +a
harbor run -p harbor/incident-1 -a opencode -m google/gemini-3.8-flash \
  --ak version=1.19.5 --ak disable_web_search=true \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY \
  --ae GOOGLE_GENERATIVE_AI_API_KEY=$GOOGLE_GENERATIVE_AI_API_KEY \
  -o jobs --job-name incident-1-opencode-gemini -l 3
```

## fleetbase_sim@1 v2 (Track B)

Date: 2026-11-15. Engine commit `01ad7ba`. Export: `fleetbase_sim@1` seeds 0–2, salted per task.
Harbor 0.26.0, OpenCode 1.19.5, `-n 3`. The full log is in `runs/fleetbase-sweep-log.md` (gitignored).

The reference and greedy columns are in-process runs on the same salted world: `FleetbaseSim().reset(seed, salt=SIDECAR_SALT)`, with the salt read from each task's `environment/docker-compose.yaml`.

### Sanity

| Job | Seed 0 | Seed 1 | Seed 2 | Note |
|---|---|---|---|---|
| `fleetbase-sim-v2-oracle` | 0.876831 | 0.654833 | 0.881079 | equal to the salted in-process reference on every digit |
| `fleetbase-sim-v2-nop` | 0.000 | 0.000 | 0.000 | 0 calls, `policy_stop` |

Replaying each agent transcript in-process on the salted world gives the Harbor reward exactly, on every trial.

### Per trial

Gap = reference − agent. Gate: gap ≥ 0.15 on all 3 seeds, for both models. Budget: 530 calls, 900 s agent timeout.

| Model | Seed | Reward | Reference | Greedy | Gap | Calls | Stop reason | Gate | Failure tags |
|---|---|---|---|---|---|---|---|---|---|
| `google/gemini-3.8-flash` | 0 | 0.821 | 0.877 | 0.414 | 0.056 | 224 | task_done | fail | other: accepts fewer requests (25 never created, value 597, vs 21 and 469 for the reference); forecast read 6×, never acted on |
| `google/gemini-3.8-flash` | 1 | 0.812 | 0.655 | 0.306 | −0.157 | 184 | task_done | fail | none: 20 deliveries vs 14 for the reference |
| `google/gemini-3.8-flash` | 2 | 0.819 | 0.881 | 0.546 | 0.062 | 172 | AgentTimeoutError (900 s), control ended at t=160 | fail | ran out of budget (wall clock) |
| `google/gemini-3.1-pro-preview` | 0 | 0.529 | 0.877 | 0.414 | 0.348 | 194 | task_done | pass | never replanned: advances grew to 20–40 min; 8 late (reference 3); urgent 1/3 created |
| `google/gemini-3.1-pro-preview` | 1 | — | 0.655 | 0.306 | — | — | stopped by user request | — | — |
| `google/gemini-3.1-pro-preview` | 2 | 0.613 | 0.881 | 0.546 | 0.268 | 204 | `finish` at t=200 | pass | never replanned: `advance(40)` at t=0; finished before wave 6 |

No trial missed a cancellation (every cancelled order the agent had created was read afterwards), and none had a capacity rejection. Both models plan with `estimate_route` (30–38 calls per trial). The reference never calls it.

### Gate verdict

- Flash fails on all 3 seeds. It is within 0.06 of the reference on seeds 0 and 2 and above it on seed 1. The seed 2 run hit the 900 s timeout at t=160 and still scored 0.819.
- Pro passes on seeds 0 and 2. Seed 1 was stopped by user request, so the Pro gate is incomplete. Pro scores below Flash because it advances time in large steps.
- No WorldConfig or policy change was made. Retuning is the user's decision.

Usage (OpenCode-reported): Flash about 22 M input tokens (95% cached), 2.90 USD for 3 trials. Pro 4.93 USD for 2 trials, plus the stopped trial (not recorded).

### Commands

```bash
export PATH=$PATH:/Applications/Docker.app/Contents/Resources/bin
uv run --directory engine crucible export harbor --task fleetbase_sim@1 --seeds 0-2 --out ../harbor/fleetbase-sim-v2
harbor run -p harbor/fleetbase-sim-v2 -a oracle -n 3 -o jobs --job-name fleetbase-sim-v2-oracle
harbor run -p harbor/fleetbase-sim-v2 -a nop -n 3 -o jobs --job-name fleetbase-sim-v2-nop
set -a; . ./.env; set +a   # GOOGLE_GENERATIVE_AI_API_KEY; the value is never on the command line
harbor run -p harbor/fleetbase-sim-v2 -a opencode -m google/gemini-3.8-flash \
  --ak version=1.19.5 --ak disable_web_search=true \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY -n 3 -o jobs --job-name fleetbase-sim-v2-opencode-flash
harbor run -p harbor/fleetbase-sim-v2 -a opencode -m google/gemini-3.1-pro-preview \
  --ak version=1.19.5 --ak disable_web_search=true \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY -n 3 -o jobs --job-name fleetbase-sim-v2-opencode-pro
```

`--api-key-name` alone passes the key from the environment, so `--ae KEY=...` is not needed. There is no `gemini-3.8-pro` or `gemini-3-pro` model; `gemini-3.1-pro-preview` is the strongest Pro model that the Gemini API lists, and OpenCode 1.19.5 accepted it on the first try.

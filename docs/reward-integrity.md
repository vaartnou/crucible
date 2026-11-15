# Reward integrity

An agent should earn its reward by doing the task. This page lists how Crucible blocks other ways to get it, the attacks we tried, and the gap that is still open.

## Protections

**No engine in the agent's container.** In Harbor, the agent's container (`main`) is `ubuntu:24.04` with curl and python3. The engine runs only in the sidecar. With the simulator, an agent could replay seeds and match what it sees. The oracle solution contains only a curl command; it uses a separate solution token to run a public-tool policy inside the sidecar.

**Token-protected `/grade`.** `GET /grade` needs the `x-grade-token` header (`hmac.compare_digest`); without it the route returns 403. The token is new for each task on each export. It is only in `[verifier.env]` and in the sidecar's environment, never in `[environment.env]`. `/grade` closes the episode, so later MCP calls get `Episode is closed`.

**No `/v1` in the sidecar.** The agent can reach every sidecar route. In sidecar mode the multi-episode API is off, so the agent cannot start its own episodes on the same seed and read their scores. API mode in turn has no `/grade` and no `/mcp`.

**Salt.** Each exported task gets a random `SIDECAR_SALT`, only in the sidecar's environment. The seed in `task.toml` is public, but the salted world cannot be rebuilt from it, even with a copy of the engine from this repo. Transcripts record `"salted": true`, never the salt.

**Leak tests.** Tool results are built from public view models, never from the hidden world. Tests check every successful result for hidden words (`Task.hidden_strings()`: root-cause names, `fault_kind`, `fault_start`, `herring`, `is_bad`) in process, over HTTP and over MCP. The instruction is checked not to name the seed's fault. Policies may not import a task's hidden modules (an `ast` test).

**Web search off.** OpenCode runs with `--ak disable_web_search=true`.

**Generated folders stay private.** `harbor/` is gitignored, because a token in a public repo is readable by an agent with internet.

## Oracle endpoint

`POST /autoplay` is disabled unless `SOLUTION_TOKEN` is configured. It requires a separate constant-time checked `x-solution-token`. That token exists in the sidecar and `[solution.env]` only. The route refuses used or closed episodes, runs the configured policy through the same session step budget, and records every public tool call. The grade token cannot authorize autoplay. This is covered by HTTP integration tests; MCP policy parity remains separately tested.

## Network status

The exporter removes `expose:` from compose and `EXPOSE` from the sidecar Dockerfile. Those settings caused a previous Harbor 0.26 allowlist startup failure with network namespace sharing. Removing them is preparation, **not evidence that the allowlist works**. A real Harbor 0.26 trial on 2026-11-15 passed container startup but failed both oracle and verifier because `mcp-server` could not be resolved (`curl: (6) Could not resolve host: mcp-server`). Job: `runs/harbor-jobs/oncall-network-smoke-20261115`. The external-host checks were never reached, so egress enforcement is unverified. Generated tasks retain Harbor's default network policy.

For a Gemini trial, test `[agent] network_mode = "allowlist"` with `allowed_hosts = ["generativelanguage.googleapis.com", "mcp-server"]`. Verify that the model API and sidecar work and that an unrelated external host is blocked. Other providers require their own API hosts. Keep exports private and retain salts and separate grade/solution tokens regardless of the network result.

Old incident and fleetbase exploit scores are historical and do not validate oncall. The current oncall gate report must establish baseline, exploit, and reference scores on the same salted worlds before reporting a model gap.

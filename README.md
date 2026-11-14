# Crucible

Crucible turns a simulator and a grader into an agent environment.
You write them once; Crucible gives you MCP tools, seeded variants, baselines, exploit tests and a Harbor export.

## Featured environment: `incident@1`

Our Junction 2026 AfterQuery submission is `incident@1`, an on-call incident.
A small web service (`api -> cache -> db`) runs for 120 simulated minutes, and one fault starts between minute 15 and 45.
The agent reads alerts, metrics, logs and deploys, repairs the fault and closes the incident with a root cause.
Every read costs a simulated minute while requests fail, so the reward is mostly set by how soon the right repair starts.
Noise alerts and unrelated deploys make a fast guess risky: a wrong repair is a penalty.

| Policy or agent | Where | Seeds | Mean reward | Root cause correct |
|---|---|---|---|---|
| `reference` (runbook policy) | in process | 0–99 | 0.968 | 100% |
| oracle (`solve.sh` plays `reference`) | Harbor | 0–9 | 0.971 | 10/10 |
| OpenCode 1.19.5 + `google/gemini-3.8-flash` | Harbor | 0–2 | 0.955 | 3/3 |
| `shotgun` (tries every repair) | in process | 0–99 | 0.012 | 38% |
| `stop_early` (guesses after two reads) | in process | 0–99 | 0.000 | 38% |
| `do_nothing` / Harbor `nop` | both | 0–99 / 0–2 | 0.000 | 0% |

Environment card: [engine/src/crucible/envs/incident/README.md](engine/src/crucible/envs/incident/README.md).
Harbor run details: [docs/harbor-runs.md](docs/harbor-runs.md).

## Last Siding: rail-yard planning demo

[Play the fork and see the evidence](docs/last-siding/README.md). On the demo board, staging
a correctly ordered pair costs one extra shunt; adding one siding slot removes that cost.
The reference finished 300/300 local episodes. Frontier performance is unmeasured.

```sh
uv run --directory engine python -m crucible.envs.last_siding.tests.demo
```

With the judge prerequisites, run the reference, three frontier attempts, do-nothing and
stop-early on one frozen board:

```sh
uv run --directory engine python scripts/last_siding_judge.py
```

## Quickstart

You need [uv](https://docs.astral.sh/uv/). This runs the small `guess@1` environment in process:

```bash
uv run --directory engine crucible tasks
```

```bash
uv run --directory engine crucible eval --task guess@1 --policy reference,do_nothing --seeds 0-9
```

The reference (binary search) scores 0.530 on average; `do_nothing` scores 0.000.
Each run writes a transcript per episode and a `summary.csv` under `engine/runs/`.

Next steps:

- [docs/concepts.md](docs/concepts.md): the words we use.
- [docs/using-environments.md](docs/using-environments.md): run, serve, export and Harbor.
- [docs/adding-an-environment.md](docs/adding-an-environment.md): write your own.
- [docs/scoring.md](docs/scoring.md) and [docs/reward-integrity.md](docs/reward-integrity.md): how rewards are built and protected.
- [docs/decisions.md](docs/decisions.md): every design decision, with the reason.

## Pinned versions

| Tool | Version | Link |
|---|---|---|
| Harbor | 0.26.0 | [laude-institute/harbor](https://github.com/laude-institute/harbor) |
| OpenCode | 1.19.5 (`--ak version=1.19.5`) | [opencode.ai](https://opencode.ai) |
| Python | 3.14 | [python.org](https://www.python.org) |
| uv | 0.12.18 | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |
| MCP Python SDK | 2.x (`mcp>=2.3.0`) | [modelcontextprotocol.io](https://modelcontextprotocol.io) |

## Layout

- `engine/`: the Python package `crucible` (core, server, export, CLI). See [engine/README.md](engine/README.md).
- `engine/src/crucible/envs/<name>/`: one folder per environment, with its policies, tests and card.
- `docs/`: these docs, the decision log and the Harbor run log.
- `spikes/hello-mcp-ns/`: the first MCP-in-Harbor spike.

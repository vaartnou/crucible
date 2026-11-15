# Crucible

Crucible runs deterministic interactive agent environments through typed tools and exports them to Harbor. The on-call incident gym includes **oncall@1** (single fault) and **oncall@2** ([overlapping incidents and a runbook comparison](docs/oncall-overlap.md)).

The difficulty claim is experimental: bot gates and real model comparisons must be measured before calling the gym hard. Old incident and Fleetbase results remain in Git history; they are not oncall results.

```bash
uv run --directory engine crucible tasks
uv run --directory engine crucible eval --task oncall@1 --policy reference,do_nothing --seeds 0-9 --out ../runs/oncall-smoke
uv run --directory engine pytest -q
uv run --directory engine pyright
```

See [using environments](docs/using-environments.md), [concepts](docs/concepts.md), and [adding an environment](docs/adding-an-environment.md). Production wheels ship oncall and SupplyGym/Cala; the guess task is a test fixture. SupplyGym documentation is in [docs/supplygym](docs/supplygym/).

Implementation scope, measured results and the tight feedback commands: [oncall status](docs/oncall-status.md).

The web UI uses Harbor + OpenCode for model evaluations. Watch launches Docker trials;
Runs reads their saved transcripts from a configured Harbor jobs directory. See
[UI setup](docs/using-environments.md#ui-and-saved-evaluations).

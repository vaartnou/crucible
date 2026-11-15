# Crucible

Crucible runs deterministic interactive agent environments through typed tools and exports them to Harbor. The on-call incident gym includes **oncall@1** (single fault) and **oncall@2** ([overlapping incidents and a runbook comparison](docs/oncall-overlap.md)).

The difficulty claim is experimental: bot gates and real model comparisons must be measured before calling the gym hard. Old incident and Fleetbase results remain in Git history; they are not oncall results.

In `oncall@2`, the agent investigates and repairs two overlapping service regressions using
metrics, logs, deployment history, and operational tools, then reports both causes. Each read
or intervention advances the simulated clock. A developing second fault can make an apparently
successful repair incomplete, so the agent must prioritize evidence and verify sustained recovery.

The score is avoided failed requests relative to the same world's no-action trajectory,
multiplied by `0.6 + 0.2 * verified_recovery + 0.2 * diagnosis_credit`, minus action penalties
and clamped to [0, 1]. Recovery is checked over a private thirty-minute probe; diagnosis credit
matches reports to both causes. The Harbor verifier writes the single score to
`/logs/verifier/reward.txt`. See the [matched model results](docs/oncall-model-comparison.md)
and [submission check](docs/submission-check.md) for evidence and packaging instructions.

The [AfterQuery submission](submissions/oncall-v2/README.md) includes the frozen Harbor
task archive, build/run instructions, checksums, and a scored OpenCode playthrough.

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

## Deploy the demo

See [Railway deployment](docs/railway.md) for the combined UI/API image and service settings.

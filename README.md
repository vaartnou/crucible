# Crucible

Crucible runs deterministic interactive agent environments through typed tools and exports them to Harbor. The on-call incident gym includes **oncall@1** (single fault) and **oncall@2** ([overlapping incidents and a runbook comparison](docs/oncall-overlap.md)).

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

New Harbor exports grade in a separate container. Harbor stops the agent, collects the
completed transcript directly from the simulator sidecar, and replays its actions using
the frozen simulator before writing the reward. Agent-written reward files are ignored.
See [verification and regrading](docs/harbor-separate-verifier.md) for the flow and checks.

The [AfterQuery submission](submissions/oncall-v2/README.md) includes the frozen Harbor
task archive, build/run instructions, checksums, and a scored OpenCode playthrough.

The fresh Gemini Flash run with separate verification scored **0.64431**, repaired both
faults, and diagnosed both causes in 60 calls. The public-tool runbook scored **0.90048**
on the same world.
The [broader model comparison](docs/oncall-model-comparison.md) reports forty trials and
the limits of the difficulty claim: this is a narrow scenario family, not a broad model ranking.

## Use the repository

Use Python 3.14, [uv](https://docs.astral.sh/uv/), and Node.js 24. Run these commands
from the repository root. Bot evaluations and manual play do not need a model API key.

```bash
uv sync --directory engine --locked
uv run --directory engine crucible tasks
uv run --directory engine crucible eval --task oncall@2 \
  --policy incident_runbook,reference,do_nothing --seeds 0-9 --out ../runs/oncall-bots
```

The evaluation writes scores and replayable transcripts. Output paths above are relative
to `engine/` because of `--directory engine`; use a new output directory for a fresh batch.

Build and open the workbench for manual play and saved model runs:

```bash
cd web
npm ci
npm run build
cd ..
uv run --directory engine crucible ui --jobs-dir ../runs/harbor-jobs --exports-dir ../runs
```

Open the URL printed by the server. **Play** starts a manual incident, **Watch** launches
Harbor evaluations from prepared exports, and **Runs** replays saved Harbor trials.

For model evaluations, install Harbor 0.26.0, start Docker, and set the provider credential
in the launching environment. Export and build a fresh task, then run OpenCode:

```bash
uv run --directory engine crucible export harbor --task oncall@2 --seeds 0 \
  --out ../runs/oncall-export
sh runs/oncall-export/build-sidecar.sh
harbor run -p runs/oncall-export -a opencode -m google/gemini-3.8-flash \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY --ak version=1.19.5 -n 1 \
  -o runs/harbor-jobs
```

Keep `GOOGLE_GENERATIVE_AI_API_KEY` outside Git. To run the exact published submission
instead, follow the [frozen task instructions](submissions/oncall-v2/README.md).

## Develop

The Python engine is in `engine/src/crucible/`, built-in environments are in
`engine/src/crucible/envs/`, and the React workbench is in `web/`.

```bash
uv run --directory engine pytest -q
uv run --directory engine ruff check
uv run --directory engine ruff format --check
uv run --directory engine pyright
cd web
npm test
npm run build
```

See [using environments](docs/using-environments.md), [concepts](docs/concepts.md), and
[adding an environment](docs/adding-an-environment.md). Production wheels ship oncall
and SupplyGym/Cala; the guess task is a test fixture. SupplyGym documentation is in
[docs/supplygym](docs/supplygym/). Old incident and Fleetbase results remain in Git history.

## Deploy the demo

See [Railway deployment](docs/railway.md) for the combined UI/API image and service settings.

# Using environments

Use Python 3.14 and uv. Run commands from the repository root; `uv run --directory engine` makes paths relative to `engine/`.

```bash
uv run --directory engine crucible tasks
uv run --directory engine crucible policies --task oncall@1
uv run --directory engine crucible eval --task oncall@1 --policy reference,do_nothing --seeds 0-49 --concurrency 8 --out ../runs/oncall-bots
```

The evaluation writes `summary.csv` and JSONL transcripts. Reusing an output directory resumes existing episodes, so use a fresh directory after simulator or scoring changes.

Keep the feedback loop in this order: deterministic checks, bot comparisons, then model evaluations in Harbor with OpenCode. Inspect failed transcripts before expanding the fault catalogue. A hard-gym claim requires a measured model gap, not merely a high reference score.

```bash
uv run --directory engine crucible export harbor --task oncall@1 --seeds 0-9 --out ../harbor/oncall-1
harbor run -p harbor/oncall-1 -a oracle -o jobs --job-name oncall-oracle
harbor run -p harbor/oncall-1 -a nop -o jobs --job-name oncall-nop
harbor view jobs
```

Harbor requires Docker. Check available memory before increasing concurrency. The oracle should match in-process reference scores on the same salted worlds; nop must score zero. Exported folders hold private salt and verifier credentials, and must not be published.

API mode supports multiple episodes:

```bash
uv run --directory engine crucible serve
uv run --directory engine crucible play --api http://localhost:8000 --task oncall@1 --seed 0 --policy reference
```

Sidecar mode exposes one episode through MCP:

```bash
MODE=sidecar GRADE_TOKEN=secret SIDECAR_TASK=oncall@1 SIDECAR_SEED=0 uv run --directory engine crucible serve
uv run --directory engine crucible play --mcp http://localhost:8000/mcp --task oncall@1 --policy reference
```

Grade access requires the private `x-grade-token` header and closes the episode. Keep model API credentials in environment variables, never in transcripts or committed files.

## UI and saved evaluations

Model evaluations use Harbor, Docker, and OpenCode. Crucible has no direct model runner.
The UI provides **Watch** (launch and observe Harbor jobs), **Play** (manual exploration),
and **Runs** (saved Harbor trial transcripts).

From the repository root, build the UI once and start the server:

```sh
cd web
npm ci
npm run build
cd ..
uv run --directory engine crucible ui --jobs-dir ../runs/harbor-jobs --exports-dir ../runs
```

Export and build the task before launching evaluations:

```sh
uv run --directory engine crucible export harbor --task oncall@1 --seeds 0-9 --out ../runs/oncall-export
sh runs/oncall-export/build-sidecar.sh
```

Watch lists prepared oncall exports under `--exports-dir`. Start evaluation invokes
`harbor run` with OpenCode 1.19.5 and Docker. Credentials are inherited from the UI
server's environment. Set the provider's environment variable before starting the server.
The UI does not send API requests to the model itself.

Harbor writes jobs under `--jobs-dir` (default `runs/harbor-jobs`, relative to the server's
working directory). Runs reads `JOB/TRIAL/verifier/transcript.json` from that directory.
Job outputs survive UI restarts. Keep the original exports to reconstruct salted worlds;
configure `--exports-dir` to point to them. Manual sessions and archived direct-model files
are not included in the evaluation list. Existing files are never deleted.

Live trials appear after their sidecar containers start. Once containers are removed,
open their completed transcripts in Runs. The UI can also observe Harbor jobs launched
from the terminal when they write to the configured job directory. Restarting the UI
does not stop a Harbor process; a job without a final result may show unknown status
until it finishes. Detailed startup failures are recorded in the job's `launcher.log`.

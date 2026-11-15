# Using environments

Use Python 3.14 and uv. Run commands from the repository root; `uv run --directory engine` makes paths relative to `engine/`.

```bash
uv run --directory engine crucible tasks
uv run --directory engine crucible policies --task oncall@1
uv run --directory engine crucible eval --task oncall@1 --policy reference,do_nothing --seeds 0-49 --concurrency 8 --out ../runs/oncall-bots
```

The evaluation writes `summary.csv` and JSONL transcripts. Reusing an output directory resumes existing episodes, so use a fresh directory after simulator or scoring changes.

Keep the feedback loop in this order: deterministic checks, 50-seed bot comparisons, a small direct model batch, then Harbor model runs. Inspect failed transcripts before expanding the fault catalogue. A hard-gym claim requires a measured model gap, not merely a high reference score.

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

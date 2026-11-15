# Harbor runs

The current environment is `oncall@1`. Older incident and fleetbase results remain in git history; they do not establish an oncall difficulty claim.

Export and build one content-addressed sidecar image for the batch:

```sh
uv run --directory engine crucible export harbor --task oncall@1 --seeds 0-9 --out ../runs/oncall-export
sh runs/oncall-export/build-sidecar.sh
harbor lint runs/oncall-export/oncall-1-seed-*
harbor run -p runs/oncall-export -a oracle -n 2
harbor run -p runs/oncall-export -a nop -n 2
```

Each task has a distinct private salt and separate grade and solution tokens. Keep exports private. Do not rebuild an export between paired model/reference trials: the salt defines the world as well as the seed.

New exports write one finite score between 0 and 1 to `/logs/verifier/reward.txt`,
as required by the challenge. The reward formula is unchanged. The `impact`, `verified`,
`diagnosis`, and `penalty` diagnostics remain in `grade.json`, with the action transcript
in `transcript.json`. Historical exports used `reward.json`; regenerate exports to use
the new format. Harbor supports both formats; see the [verifier documentation](https://docs.harborframework.com/tasks/verifier).

The oracle invokes `/autoplay` with a solution-only token; it runs the reference in process and records public tool actions. This removes per-trial network installation of the engine. Live MCP tests separately exercise the model-facing protocol.

Run deterministic checks and bot baselines before a model batch. All new model evaluations use Harbor with OpenCode; the former direct-model screening runner has been removed. Use [the failure rubric](oncall-failure-rubric.md) to examine both losses and model successes.

Docker Desktop reported 8,317,202,432 bytes (about 7.75 GiB) on 2026-11-15. Use concurrency 2–4 at that allocation; concurrency 8 awaits an explicit host-memory change and a measured resource check.

Network isolation is still an experiment; see [reward integrity](reward-integrity.md). Removing exposed-port metadata alone does not prove an allowlist is enforced.

## Final optimized simulator smoke, 2026-11-15

The final export includes the causal model, corrected public policies, and equivalence-tested performance changes. Fresh private salts selected these two incidents:

| Seed | Incident | Oracle reward | Verified | Diagnosis | Penalty | Nop reward |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0 | queue_backlog | 0.7159818807383254 | 1 | 1 | 0 | 0 |
| 1 | retry_storm | 0.612620280786114 | 1 | 1 | 0 | 0 |

Oracle mean: **0.6643010807622197**. Both incidents recovered and were diagnosed correctly; queue drain and accumulated impact still cost reward. These are fresh worlds, so their scores cannot be compared directly with earlier exports. There were no trial exceptions. Oracle and nop batches each took about 14 seconds at concurrency 2.

Export: `runs/oncall-harbor-optimized`. Jobs: `runs/harbor-jobs/oncall-oracle-optimized-20261115` and `runs/harbor-jobs/oncall-nop-optimized-20261115`. All four transcripts regraded to identical rewards with no observation divergence using the original export. Reports: `runs/oncall-oracle-optimized-regrade.json` and `runs/oncall-nop-optimized-regrade.json`.

Wheel SHA-256: `fe561704b5c91ce89d2221d7a198a71a3d1bb7a2148cb257c9ad1ff0f18ecb34`. Shared image: `crucible-sidecar:6421cd9069ee72f0282ad85e`. These are protocol checks, not evidence of a model reasoning gap. Earlier exports below are retained for provenance.

A measured optimization check on unsalted seed 14 shotgun reduced elapsed time from 33.88 seconds in the previous harness to 5.256 seconds for the CLI run (concurrent load differed). All 48 public step records and the complete score were identical. Artifact: `runs/oncall-shotgun14-perf`. Do not infer a whole-batch speedup from this one episode.

## Earlier causal simulator v2 smoke, 2026-11-15

After the causal fluid-model and public-policy freeze, a fresh two-seed export produced:

| Agent | Trials | Mean reward | Verified | Diagnosis | Penalty | Exceptions |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| oracle | 2 | 0.9294979150299925 | 1 | 1 | 0 | 0 |
| nop | 2 | 0 | 0 | 0 | 0 | 0 |

Jobs: `runs/harbor-jobs/oncall-oracle-causal-v2-20261115` and `runs/harbor-jobs/oncall-nop-causal-v2-20261115`. Export: `runs/oncall-harbor-causal`. Each job took about 13 seconds at concurrency 2. All four transcripts replayed with the original export salt, exact score equality, and no public-observation divergence. Reports: `runs/oncall-oracle-causal-v2-regrade.json` and `runs/oncall-nop-causal-v2-regrade.json`. The replay's `comparable` flag means observation compatibility, not independent proof that unobserved state is identical.

Wheel SHA-256: `9ef3bbc38f4650a7c29364b3c82f6dfcf80e084640e8ac7fe97ae0facb22c168`. Shared image: `crucible-sidecar:90a6cbf68e264c53e8700c54`. These smoke trials establish protocol and replay correctness; they are not a model difficulty measurement. No model Harbor batch was launched before the direct-model gap gate.

## Superseded first-simulator smoke, 2026-11-15

Two seeds (0 and 1), using the same private salts in every comparison, Harbor 0.26.0, Docker concurrency 2:

| Agent | Trials | Mean reward | Verified | Diagnosis | Penalty | Exceptions |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| oracle | 2 | 0.9504025968104086 | 1 | 1 | 0 | 0 |
| nop | 2 | 0 | 0 | 0 | 0 | 0 |

All four final transcripts replayed through `crucible regrade --export runs/oncall-harbor-final` with exact score equality, `comparable=true`, and no observation divergence. Reports: `runs/oncall-oracle-final-regrade.json` and `runs/oncall-nop-final-regrade.json`. Jobs: `runs/harbor-jobs/oncall-oracle-final-20261115` and `runs/harbor-jobs/oncall-nop-final-20261115`. Export: `runs/oncall-harbor-final`; wheel SHA and source revision are in its manifest. These two smoke seeds establish wiring, not the ten-seed hardness gate. The wheel was rebuilt after the simulator freeze. Earlier `*-smoke-*` jobs used an intermediate simulator and are superseded by these `*-final-*` results. Subsequent simulator changes require a fresh export for reported scores.

The allowlist trial (`oncall-network-smoke-20261115`) no longer hit the port-exposure startup error, but `mcp-server` DNS failed for oracle and verifier. That trial is an infrastructure failure, not a zero-scoring reasoning episode. External-host checks did not run. Default network access remains enabled.

Frozen wheel SHA-256: `ba041e9e82eed12f864444762e6e88d10749483961e3c1dfc86f06191cab7e05`. Shared image: `crucible-sidecar:b939134eb759f01e16e6b354`.

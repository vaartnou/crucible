# AfterQuery submission: overlapping on-call incidents

The agent diagnoses and repairs two overlapping service regressions using metrics,
logs, deployment history, and operational tools. Reads and interventions advance
the simulated clock. A developing second fault can make an apparently successful
repair incomplete; the agent must investigate residual impact and report both causes.

The score measures avoided failed requests against the same world's no-action
trajectory, multiplied by `0.6 + 0.2 * verified_recovery + 0.2 * diagnosis_credit`,
minus action penalties and clamped to [0, 1]. Recovery is probed for thirty additional
simulated minutes. The verifier writes `/logs/verifier/reward.txt`.

## Files

- `oncall-v2-separate-7b0512f.tar.gz`, with its `.sha256` checksum.
  This package uses a fresh verifier container and trusted sidecar artifact collection.
  `submission-evidence-separate.zip` contains the fresh model run and its successful
  regrade against this frozen verifier.
  The task contains the simulator wheel, locked requirements, configuration,
  verifier, and oracle solution, frozen from source commit
  `7b0512f416befda09f2aeaab413e4cbb3bed5e2e`. The archive also contains
  `SHA256SUMS` for its individual files.

## Run

Install Harbor 0.26.0 and start Docker. From this directory:

```sh
shasum -a 256 -c oncall-v2-separate-7b0512f.tar.gz.sha256
tar -xzf oncall-v2-separate-7b0512f.tar.gz
cd oncall-v2
shasum -a 256 -c SHA256SUMS
sh build-sidecar.sh
harbor run -p . -a oracle -n 1
```

For a model playthrough, configure `GOOGLE_GENERATIVE_AI_API_KEY` in the launching
environment, then run from the extracted `oncall-v2` directory:

```sh
harbor run -p . -a opencode -m google/gemini-3.8-flash \
  --api-key-name GOOGLE_GENERATIVE_AI_API_KEY --ak version=1.19.5 -n 1
```

The fresh model run with separate verification scored **0.644313711357698**, with
verified recovery, correct diagnosis, zero penalties, 60 tool calls, and zero Harbor
exceptions. Native Harbor regrading against the frozen package reproduces that score
exactly without rerunning the model. The public-tool
runbook scores **0.9004757202439811** on the same world. Model sampling can produce
different actions and scores. See [the broader measurements](../../docs/oncall-model-comparison.md)
for the scenario's measured difficulty and limitations.

## Publication and isolation

This submission intentionally publishes its task-specific grade token, solution
token, and scenario salt inside the archive. It contains no provider API key.
This is a published demonstration scenario. Generate a fresh export with new salts
and tokens for held-out evaluation. The agent container
does not receive the task configuration, simulator wheel, or sidecar environment.

The current package stops the agent, collects its transcript directly from the simulator
sidecar, and recomputes its score in a separate verifier. Full default Harbor lint passes
without exceptions. See [verification details](../../docs/harbor-separate-verifier.md).
Task files and wheel are frozen;
external base-image tags and package download services are not fully immutable.

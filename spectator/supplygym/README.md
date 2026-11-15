# SupplyGym procurement spectator

From the repository root, run the following command with Python 3.10 or later, then open [the local viewer](http://127.0.0.1:8765/).

```sh
python spectator/supplygym/serve.py
```

The viewer opens on week 21 of a public, scripted SupplyGym year. The buyer saw three late orders. Ask the audience whether they would pay to expedite, then select **Reveal decision & result**. The recorded buyer increased the expedited share from 0% to 30%, spent $621 on expedited freight, and finished the week with $2,700.20 in operating surplus. All five curves come from the same scenario. [Replay provenance](demo/README.md)

Play or scrub the recording to inspect another week. **Catch up** shows the latest available week, **Open recorded demo** resets the example, and **Choose folder** accepts another saved PR 37 run directory.

## Follow a live Harbor run

The live feed requires the runner and simulator from [PR 37](https://github.com/vaartnou/crucible/pull/37). This viewer can run independently against saved files. For Linux or WSL, check the [spectator permission report](../../docs/supplygym-spectator-permission-report.md) before starting a model run. The tested local correction mounts the feed at `/app/spectator-events.jsonl`.

For one Sonnet 5.5 year, run the following from the PR 37 checkout in Ubuntu or Bash. The key prompt hides your Anthropic API key. Use a new output directory for each invocation.

```bash
RUNNER=$(find engine/scripts -maxdepth 1 -name 'run_*demo.py' -print -quit)
if [ -f "$RUNNER" ]; then
  uv run --directory engine python "$PWD/$RUNNER" --out "$PWD/runs/supplygym-sonnet-single" --start-seed 62000 --count 1 --model anthropic/claude-sonnet-5-5 --key-env ANTHROPIC_API_KEY --prompt-key --concurrency 1
else
  echo "The PR 37 demo runner is missing from this checkout."
fi
```

The legend calls the model-driven strategy **AI**. Annual results show operating surplus, reward, and the theoretical upper bound.

On the machine with access to the runner output, start the viewer with the output directory passed to PR 37â€™s demo runner. For example:

```sh
python spectator/supplygym/serve.py --run-dir runs/supplygym-demo --port 8766
```

Open [the live viewer](http://127.0.0.1:8766/) and select **Connect live â†’ Connect**. The default manifest is `/run/spectator-manifest.json`. The server binds to loopback and serves only the viewer, the manifest, and the public spectator feeds. A connection started before the manifest is ready retries automatically. The baseline curves advance through the latest completed buyer week. Pause or scrub to hold the display while polling continues. Press play to resume following live.

## Reading the results

The vertical axis shows the simulatorâ€™s cumulative operating surplus in USD. Negative weeks and falling lines are valid. Final values come from each strategyâ€™s terminal spectator record. The bundled recording uses PR 37's original scorer: clipped operating surplus divided by its scenario-specific upper bound. PR 46 changes live Cala @3 scoring to terminal-inclusive adjusted surplus above a fixed default-play floor; its reported reward is authoritative. The chart continues to show operating surplus, separately from reward. Scorer identities keep historical and current runs separate. The upper bound relaxes shipping constraints and can exceed what a legal weekly strategy could earn. [PR 37](https://github.com/vaartnou/crucible/pull/37)

Run the parser and live HTTP checks with `node --test spectator/supplygym/feed.test.mjs spectator/supplygym/live.test.mjs`. The HTTP check starts a local server on a temporary port.

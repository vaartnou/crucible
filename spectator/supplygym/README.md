# SupplyGym interactive simulator

SupplyGym now accepts procurement inputs and runs the real Cala simulator from the browser. Choose a scenario seed, set your standing controls, then advance one week or run the rest of the year. Four deterministic baselines run against the same scenario and appear in the existing surplus chart.

## Start the simulator

From the repository root, install the engine with Python 3.14 and start the local server:

```sh
uv sync --directory engine --locked
uv run --project engine python spectator/supplygym/serve.py
```

Open [SupplyGym](http://127.0.0.1:8765/).

1. Set the scenario seed and procurement controls, then select **Create simulation**.
2. Wait for the four baseline years to finish preparing. Your strategy starts at week zero; the current public observation is shown before you act.
3. Select **Apply & advance one week** to submit a decision, or **Run remaining year** to repeat the standing controls through week 53.
4. Pause automation to revise the next decision. Cancel to stop an unfinished episode; its reward is zero. Create a new simulation after completion or cancellation to try different inputs.

The controls cover replenishment formula, safety stock, order multiplier and expedited shipping. Advanced controls accept supplier weights and the next alternative supplier approval. The engine validates the inputs before advancing the week. Omitted controls retain their current values. Alternative approval is a one-time action.

The automatic runner repeats your chosen controls. It requires no model credentials or Docker. Its curve is labelled **Your strategy**; an externally connected model run retains the **AI** label. A paused chart only pauses playback; **Pause simulation** stops automatic decisions between weeks.

Repeating a seed while the server remains running uses the same private scenario, so you can compare input choices. Restarting the server creates new private scenario material. Each run has its own directory under `runs/supplygym-ui/` (override with `--output-dir`). Outputs survive a restart as saved recordings; the server manages one active simulation at a time. The API and viewer bind to loopback. Only allowlisted public feeds are served; private transcripts and scenario material are never served.

## Recorded and external runs

The existing recorded demo, file/folder import, live manifest connection, timeline and decision inspector remain available. With Python 3.10 or later, `python spectator/supplygym/serve.py` can serve the viewer without engine dependencies; the simulation panel explains how to enable execution.

To watch an external Harbor run:

```sh
python spectator/supplygym/serve.py --run-dir runs/supplygym-demo --port 8766
```

Open [the live viewer](http://127.0.0.1:8766/) and select **Connect live**, using `/run/spectator-manifest.json`. This connection watches an existing run; the local simulation controls create separate in-process runs. [PR41](https://github.com/vaartnou/crucible/pull/41) describes the external model workflow. [Bundled replay provenance](demo/README.md)

The chart shows cumulative operating surplus in USD. Completed-year rewards and upper bounds come from the engine's final spectator record. Simulator and scorer identities keep incompatible recordings separate. Baselines are revealed only through your strategy's latest completed week.

## Validation

```sh
node --test spectator/supplygym/*.test.mjs
uv run --directory engine pytest ../spectator/supplygym/test_simulation.py
```

The HTTP/engine integration tests exercise real episodes, final accounting, input validation, cancellation, repeat scenarios, isolated runs and the public-file boundary.

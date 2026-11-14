# Last Siding · last_siding@1

Can a player recognize that a useful-looking placement must be undone to retrieve a wagon underneath it? Assemble two trains in a fully visible yard. [Pause-and-replay demo and measured results](../../../../../docs/last-siding/README.md). **Frontier performance is unmeasured.**

Read each track from buffer to accessible end (marked `>`). D is the departure track; L and R are single-ended sidings. Every wagon ID occurs once. Shunt transfers a nonempty right-hand suffix to a different track with enough space, **without reversing it**: D=[A,B,C], L=[X], D→L ×2 gives D=[A], L=[X,B,C]. That includes locomotive travel. Wagons have no orientation. Dispatch removes the entire D only if it exactly matches the next manifest from left to right.

| Tool | Effect |
|---|---|
| `inspect()` | Diagram, ID lists, capacities, manifests in dispatch order, history, shunts left and legal actions. |
| `shunt(source,destination,count,top?)` | Perform one transfer. Optional `top` verifies the source's accessible ID. Returns the full updated board. |
| `dispatch()` | Send the next manifest. Wrong order, extra wagons and duplicate dispatch are refused. |
| `preview(move)` | Return a labeled hypothetical full board; pass shunt arguments or the string `"dispatch"`. No simulator change. |
| `stop()` | End play and keep credit for dispatches already made. |

There are **16 successful shunts and 64 total harness calls**. Inspection, preview, dispatch and invalid calls use no shunts. Dispatch is still legal on the last shunt; an exhausted board that cannot dispatch ends. No dispatch scores 0; first only scores 0.10; both score `0.80 + 0.20 × optimal_shunts / actual_shunts`. Only two simulator-confirmed dispatches count as solved.

The seed and optional salt select from 170 canonical five- and six-wagon layouts, shuffled without replacement within a 170-seed epoch, with optional siding exchange. All 170 have an expensive tempting first move that becomes optimal with one additional slot. Optimal completions take 5–8 shunts (see the measured range in the evidence). IDs encode manifest positions; renamings and mirrored sidings do not inflate the bank. These are finite puzzles, not 300 independent designs. Two reserved **unsalted** seeds (1000000/1000001) select the separate demonstration pair.

The offline oracle enumerates the reverse graph from the empty yard: inverse dispatch costs zero, inverse shunt costs one. The packaged bank contains layouts and optimal counts; the path search stays in the source-only tests. The reference searches forward to the next manifest using path cost, departure-prefix progress, intact blocks and buried wagons. It uses only public tool results. It can finish the first train efficiently yet leave an expensive second train. Stop-early uses the same planner and stops after the first real dispatch.

From the repository root:

```sh
uv run --directory engine crucible tasks
uv run --directory engine pytest -q tests/generic tests/policies src/crucible/envs/last_siding
uv run --directory engine crucible eval --task last_siding@1 --policy reference,do_nothing,stop_early --seeds 0-299 --out ../runs/last-siding-local
uv run --directory engine python -m crucible.envs.last_siding.tests.demo
```

The judge command is `uv run --directory engine python scripts/last_siding_judge.py`. It uses one frozen Harbor export for the reference, three frontier attempts and both controls. See the [evidence and prerequisites](../../../../../docs/last-siding/README.md) before running it.


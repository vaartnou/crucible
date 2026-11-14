"""Public fork demo, exact cost audit and checked native JSONL replay."""

import argparse
import asyncio
from pathlib import Path

from crucible.core.jobs import Job
from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter, read_transcript
from crucible.envs.last_siding.task import LastSidingTask
from crucible.envs.last_siding.tests.oracle import continuation, distances
from crucible.envs.last_siding.world import CONTROL_SEED, DEMO_SEED, draw
from crucible.envs.last_siding.yard import TRACKS, Move, shunts, transfer
from crucible.testing import ScriptedPolicy, play

PROOF = Path(__file__).with_name("evidence")
BRANCHES: dict[str, Move] = {"stage": (0, 1, 1), "clear": (1, 2, 2)}


def call(move: Move | None) -> ToolCall:
    if move is None:
        return ToolCall(name="dispatch")
    i, j, n = move
    return ToolCall(name="shunt", args={"source": TRACKS[i], "destination": TRACKS[j], "count": n})


async def record(folder: Path) -> None:
    for seed, kind in ((DEMO_SEED, "tight"), (CONTROL_SEED, "control")):
        layout = draw(seed, None)
        for label, move in BRANCHES.items():
            after = transfer(layout.board, move)
            path = continuation(after, layout.caps, layout.manifests)
            calls = [ToolCall(name="inspect"), call(move), *map(call, path)]
            result = await play(
                ScriptedPolicy(calls), seed, folder / f"{kind}-{label}.jsonl", "last_siding@1"
            )
            assert result.score.flags["solved"]


async def replay(path: Path, verbose: bool = True) -> None:
    """Re-execute native calls and compare results and grades; audit cost from exact pre-state."""
    import tempfile

    episode = read_transcript(path)
    h = episode.header
    task = LastSidingTask()
    if h.task != task.spec.ref or h.scoring_hash != task.scoring_hash or h.salted:
        raise ValueError("Expected matching task, scoring hash and unsalted local transcript")
    with tempfile.TemporaryDirectory() as tmp:
        session = EpisodeSession(task, h.seed, TranscriptWriter(Path(tmp) / "replay.jsonl"))
        await session.start(
            policy_name=h.policy,
            job=Job(task=h.task, policy=h.policy, seed=h.seed, repeat=h.repeat),
        )
        assert task.layout
        dist = distances(task.layout.caps, task.layout.manifests)
        for n, step in enumerate(episode.steps):
            before, used = task.board, task.used
            result = await session.step(step.call)
            if n != step.step or result != step.result:
                raise ValueError(f"Replay mismatch at step {n}")
            lost = task.used - used + dist[task.board] - dist[before]
            if verbose:
                print(
                    f"  {n:2} {step.call.name:9} {step.call.args!s:46} "
                    f"{' | '.join(t or '-' for t in task.board):18} "
                    f"remaining optimum {dist[task.board]}  cost +{lost}"
                )
        reason, score = await session.close()
        if (reason, score, len(episode.steps)) != (
            episode.footer.stop_reason,
            episode.footer.score,
            episode.footer.steps,
        ):
            raise ValueError("Replay footer mismatch")
        if verbose:
            print(
                f"  Verified: {task.sent} dispatches, {task.used} shunts, score {score.total:.6f}."
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, help="Write four runner transcripts")
    parser.add_argument("--replay", type=Path, help="Audit any unsalted Last Siding transcript")
    parser.add_argument("--choose", choices=list(BRANCHES), help="Skip interactive pause")
    args = parser.parse_args()
    if args.record:
        await record(args.record)
        return
    if args.replay:
        await replay(args.replay)
        return
    game = LastSidingTask()
    board = (await game.reset(DEMO_SEED)).data
    print(board["diagram"], "\n> = accessible end. Send A B, then C D E.")
    print("Stage: D to L, count 1 (couple C-D). Clear: L to R, count 2 (lift B-C).")
    choice = (
        args.choose or (await asyncio.to_thread(input, "Judge: stage or clear? ")).strip().lower()
    )
    if choice not in BRANCHES:
        parser.error("choose stage or clear")
    for kind, seed in (("tight", DEMO_SEED), ("control", CONTROL_SEED)):
        layout = draw(seed, None)
        dist = distances(layout.caps, layout.manifests)
        print(f"\n{kind}: capacities {layout.caps}; optimal opening distance {dist[layout.board]}.")
        print("Remaining shunts after EVERY legal first shunt:")
        for m in shunts(layout.board, layout.caps):
            print(f"  {TRACKS[m[0]]}>{TRACKS[m[1]]} x{m[2]}: {dist[transfer(layout.board, m)]}")
        for branch in (choice, next(b for b in BRANCHES if b != choice)):
            print(f"{kind}/{branch} (restart identical board)")
            await replay(PROOF / f"{kind}-{branch}.jsonl")


if __name__ == "__main__":
    asyncio.run(main())

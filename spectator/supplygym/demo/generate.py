"""Generate the public demo with PR #37's engine, without a model or credentials.

Run with Python 3.14 and the PR's engine/src and engine/scripts on PYTHONPATH.
The fixed salt is deliberately public: this is a demonstration, not an evaluation.
"""
import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from crucible.core.jobs import Job
from crucible.core.registry import get_task
from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall
from crucible.core.transcript import TranscriptWriter
from crucible.server.spectator import Spectator

OUT = Path(__file__).resolve().parent
SEED = 62000
# Original public scenario seed material, preserved for replay reproducibility.
SALT = bytes.fromhex("7075626c69632d63616c612d7669657765722d64656d6f2d7631").decode()


def apply_display_alias(path: Path) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["task"] = "supplygym@3"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


async def scripted_buyer(path: Path) -> None:
    task = get_task(TASK)
    job = Job(task=TASK, policy="scripted_buyer", seed=SEED)
    with tempfile.TemporaryDirectory() as temp:
        session = EpisodeSession(task, SEED, TranscriptWriter(Path(temp) / "transcript.jsonl"), salt=SALT)
        await session.start(policy_name="scripted_buyer", job=job)
        feed = Spectator(path, task, SEED, SALT, "demo-62000-scripted-buyer", "scripted_buyer")
        while not task.done():
            # Respond only to public overdue-order counts; no model or hidden state.
            status = task.spectator_snapshot()["observation"]
            late = sum(s["late_orders"] for s in status["suppliers"])
            call = ToolCall(name="set_controls", args={"safety_stock_days": 1.5, "expedite_fraction": 0.3 if late else 0.0})
            before = feed.before_step(call)
            result = await session.step(call)
            if not result.ok:
                raise RuntimeError(result.error)
            feed.after_step(call, result, before)
        reason, score = await session.close()
        feed.finalize(reason, score)


async def main() -> None:
    paths = {}
    with tempfile.TemporaryDirectory() as temp:
        for name in BASELINES:
            print(f"Running {name}", flush=True)
            temporary = Path(temp) / name
            await run_baseline(SEED, SALT, name, temporary, f"demo-62000-{name}")
            dest = OUT / "baselines" / f"seed-{SEED}" / name / "events.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((temporary / "events.jsonl").read_bytes())
            apply_display_alias(dest)
            paths[name] = dest.relative_to(OUT).as_posix()
    live = OUT / "live" / f"seed-{SEED}" / "events.jsonl"
    print("Running scripted buyer", flush=True)
    live.parent.mkdir(parents=True, exist_ok=True)
    if live.exists():
        live.unlink()
    await scripted_buyer(live)
    apply_display_alias(live)
    paths["scripted_buyer"] = live.relative_to(OUT).as_posix()
    first = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    manifest = [{"seed": SEED, "scenario_comparison_id": first["scenario_comparison_id"], "events": paths}]
    (OUT / "spectator-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Pass the path to PR 37's demo runner script.")
    spec = importlib.util.spec_from_file_location("supplygym_demo_runner", sys.argv[1])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    BASELINES, TASK, run_baseline = runner.BASELINES, runner.TASK, runner.run_baseline
    asyncio.run(main())

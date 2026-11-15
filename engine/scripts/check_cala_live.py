"""Exercise the Cala spectator stream through Harbor without calling a model."""

import argparse
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from crucible.export.harbor import build_engine_artifacts, export_harbor

if __package__:
    from .run_cala_demo import configure_spectator
else:
    from run_cala_demo import configure_spectator

TASK = "supplygym-cala@3"
PROBE = """\
import asyncio

from crucible.agents.remote import McpEpisode
from crucible.core.tools import ToolCall


async def main():
    episode, _ = await McpEpisode.connect("http://mcp-server:8000/mcp")
    try:
        assert (await episode.step(ToolCall(name="get_status"))).ok
        assert not (await episode.step(
            ToolCall(name="set_controls", args={"expedite_fraction": -1})
        )).ok
        for week in range(1, 54):
            result = await episode.step(ToolCall(name="set_controls"))
            assert result.ok, result.error
            if week == 1:
                # Leave time for the independent host reader to observe week 1.
                assert (await episode.step(ToolCall(name="get_status"))).ok
                await asyncio.sleep(20)
    finally:
        await episode.close()


asyncio.run(main())
"""


def records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if line.endswith("\n")
    ]


def install_probe(task: Path) -> None:
    solution = task / "solution"
    script = solution / "solve.sh"
    prefix, marker, _ = script.read_text(encoding="utf-8").partition(
        "\n# Oracle entry point (replaced by the Cala live probe).\n"
    )
    if not marker:
        raise ValueError("The Harbor template changed. Update the spectator probe.")
    script.write_text(
        prefix + '\n/tmp/crucible-solution/bin/python "$SOLUTION_DIR/live_probe.py"\n',
        encoding="utf-8",
        newline="\n",
    )
    (solution / "live_probe.py").write_text(PROBE, encoding="utf-8", newline="\n")


def watch_harbor(task: Path, stream: Path, out: Path, timeout: float) -> None:
    log = out / "harbor.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                "harbor",
                "run",
                "-p",
                str(task),
                "-a",
                "oracle",
                "-n",
                "1",
                "-o",
                str(out / "jobs"),
                "--job-name",
                "cala-live-smoke",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        observed_week_one = False
        try:
            while time.monotonic() < deadline and process.poll() is None:
                weeks = [row for row in records(stream) if row["event"] == "week"]
                if len(weeks) == 1 and process.poll() is None:
                    observed_week_one = True
                    print("Host viewer received week 1 while Harbor was running.", flush=True)
                    break
                time.sleep(0.25)
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
    if status != 0 or not observed_week_one:
        raise RuntimeError(f"Harbor did not deliver week 1 live. See {log}")


def check_final(stream: Path, out: Path) -> None:
    events = records(stream)
    weeks = [row for row in events if row["event"] == "week"]
    endings = [row for row in events if row["event"] == "final"]
    if [row["week"] for row in weeks] != list(range(1, 54)):
        raise ValueError("Expected exactly 53 ordered weekly records")
    if weeks[-1]["end_day"] != 365:
        raise ValueError("Expected the short final week to end on day 365")
    if len(endings) != 1 or not endings[0]["completed"] or endings[0]["week"] != 53:
        raise ValueError("Expected exactly one final record for the completed year")
    grades = list((out / "jobs").glob("*/*/verifier/grade.json"))
    if len(grades) != 1:
        raise ValueError("Expected one Harbor verifier grade")
    outcome = json.loads(grades[0].read_text(encoding="utf-8"))
    annual = outcome["breakdown"]["operating_surplus"]
    actual = weeks[-1]["cumulative_operating_surplus"]
    if not outcome["flags"]["completed"] or not math.isclose(actual, annual, abs_tol=0.000001):
        raise ValueError("The live stream does not match the completed annual verifier result")
    upper = outcome["breakdown"]["surplus_upper_bound"]
    if not math.isclose(outcome["reward"], max(0, min(1, annual / upper)), abs_tol=1e-9):
        raise ValueError("Harbor reward does not match the scenario upper bound formula")
    print(f"Harbor graded 53 weeks at ${annual:,.2f}, reward {outcome['reward']:.6f}.")
    print(f"Viewer replay and Harbor logs are in {out}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New directory for the smoke run")
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=600, help="Seconds to allow Harbor")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="cala-live-wheel-") as artifacts:
        (task,) = export_harbor(
            TASK,
            [args.seed],
            out / "dataset",
            build_engine_artifacts(Path(artifacts)),
        )
    install_probe(task)
    stream = out / "spectator" / "live.jsonl"
    stream.parent.mkdir(mode=0o700)
    configure_spectator(task / "environment" / "docker-compose.yaml", stream, "cala-live-smoke")
    watch_harbor(task, stream, out, args.timeout)
    check_final(stream, out)


if __name__ == "__main__":
    main()

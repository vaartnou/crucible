"""AfterQuery six-trial driver. Uses one frozen Harbor world; writes no credentials to the plan."""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

from crucible.core.transcript import Footer, Header
from crucible.envs.last_siding.task import LastSidingTask
from crucible.export.harbor import build_engine_artifacts, export_harbor

ROOT = Path(__file__).resolve().parents[2]
MODEL = os.environ.get("LAST_SIDING_MODEL", "google/gemini-3.8-flash")
RUN_NAME = {
    "google/gemini-4-hydrogen": "last-siding-hydrogen",
    "google/gemini-3.8-flash": "last-siding-judge",
    "google/gemma-4-31b-it": "last-siding-gemma-4-31b",
    "google/gemma-4-26b-a4b-it": "last-siding-gemma-4-26b",
}.get(MODEL)
if RUN_NAME is None:
    raise SystemExit(f"Unsupported Last Siding model: {MODEL}")
DATA = ROOT / "harbor" / RUN_NAME
JOBS = ROOT / "jobs" / RUN_NAME
TASK = "last_siding@1"
KEY = "GOOGLE_GENERATIVE_AI_API_KEY"
SEED = 0
ATTEMPTS = ("reference", "frontier-0", "frontier-1", "frontier-2", "do_nothing", "stop_early")


def command(label: str, key: str) -> list[str]:
    dataset = DATA / ("early" if label == "stop_early" else "main")
    agent = (
        "opencode" if label.startswith("frontier") else "nop" if label == "do_nothing" else "oracle"
    )
    argv = ["harbor", "run", "-p", str(dataset), "-a", agent, "-o", str(JOBS), "--job-name", label]
    if agent == "opencode":
        argv.extend(
            [
                "-m",
                MODEL,
                "--ak",
                "version=1.19.5",
                "--ak",
                "disable_web_search=true",
                "--api-key-name",
                KEY,
                "--ae",
                f"{KEY}={key}",
            ]
        )
    return argv


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint() -> dict[str, str]:
    # Bind reruns to all exported files, including world identity, without recording secrets.
    return {
        p.relative_to(DATA).as_posix(): digest(p)
        for tree in ("main", "early")
        for p in sorted((DATA / tree).rglob("*"))
        if p.is_file()
    }


def prepare() -> None:
    plan = DATA / "plan.json"
    if plan.exists():
        saved = json.loads(plan.read_text())
        if (
            saved["model"] != MODEL
            or saved["fingerprints"] != fingerprint()
            or saved["scoring_hash"] != LastSidingTask.scoring_hash
        ):
            raise SystemExit("Frozen export differs from plan; use a fresh experiment folder.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = build_engine_artifacts(Path(tmp))
        with ZipFile(artifacts.wheel) as wheel:
            assert "crucible/envs/last_siding/layouts.json" in wheel.namelist()
            assert not any("last_siding/tests/" in name for name in wheel.namelist())
        (base,) = export_harbor(TASK, [SEED], DATA / "main", artifacts)
    # Copy the SAME export, changing only the private built-in solution selection.
    early = DATA / "early"
    shutil.copytree(DATA / "main", early)
    cfg = early / base.name / "task.toml"
    text = cfg.read_text(encoding="utf8")
    key = 'CRUCIBLE_POLICY = "reference"'
    assert text.count(key) == 1
    cfg.write_text(
        text.replace(key, 'CRUCIBLE_POLICY = "stop_early"'), encoding="utf8", newline="\n"
    )
    meta = early / "manifest.json"
    manifest = json.loads(meta.read_text())
    manifest["solution_policy"] = "stop_early"
    meta.write_text(json.dumps(manifest, indent=2) + "\n")
    plan.write_text(
        json.dumps(
            {
                "task": TASK,
                "seed": SEED,
                "repeat_ids": [0, 1, 2],
                "scoring_hash": LastSidingTask.scoring_hash,
                "model": MODEL,
                "harbor": "0.26.0",
                "opencode": "1.19.5",
                "web_search": False,
                "sampling": "provider/harness defaults; no temperature or model seed override",
                "agent_timeout_sec": 600,
                "prompt": (base / "instruction.md").read_text(encoding="utf8"),
                "release": json.loads((DATA / "main" / "manifest.json").read_text()),
                "commands": [command(name, "<from environment>") for name in ATTEMPTS],
                "fingerprints": fingerprint(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf8",
    )


def collect(label: str) -> tuple[float, int]:
    job = JOBS / label
    paths = list(job.rglob("verifier/grade.json"))
    if len(paths) != 1:
        raise SystemExit(f"{label}: expected one verified trial; inspect {job}")
    verifier = paths[0].parent
    trial = json.loads((verifier.parent / "result.json").read_text(encoding="utf8"))
    if trial.get("exception_info"):
        raise SystemExit(f"{label}: infrastructure/agent error; inspect {verifier.parent}")
    g = json.loads(paths[0].read_text(encoding="utf8"))
    trace = json.loads((verifier / "transcript.json").read_text(encoding="utf8"))
    h, f = Header.model_validate(trace[0]), Footer.model_validate(trace[-1])
    if (
        (h.task, h.seed, h.salted, h.scoring_hash)
        != (TASK, SEED, True, LastSidingTask.scoring_hash)
        or f.error
        or f.score.total != g["reward"]
        or f.score.breakdown != g["breakdown"]
    ):
        raise SystemExit(f"{label}: transcript and grade disagree")
    b = f.score.breakdown
    excess = b["actual_shunts"] - b["optimal_shunts"] if f.score.flags["solved"] else None
    print(
        f"{label}: dispatched {b['dispatches']:.0f}/2, score {f.score.total:.6f}, "
        f"excess {excess if excess is not None else 'n/a'}; {verifier}"
    )
    return f.score.total, int(f.score.flags["solved"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare", action="store_true", help="Freeze dataset, prompt and six commands only"
    )
    args = parser.parse_args()
    prepare()
    print("Frozen six-trial plan:", DATA / "plan.json")
    if args.prepare:
        return
    if not os.environ.get(KEY) or not shutil.which("harbor") or not shutil.which("docker"):
        raise SystemExit(
            "Live trials require Harbor 0.26.0, Docker and " + KEY + "; frontier unmeasured."
        )
    if "0.26.0" not in subprocess.check_output(["harbor", "--version"], text=True):
        raise SystemExit("Use the frozen Harbor version 0.26.0.")
    subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, check=True)
    if JOBS.exists():
        raise SystemExit(f"Refusing to mix/reuse trials in {JOBS}; archive it before a fresh run.")
    JOBS.mkdir(parents=True)
    rows: list[tuple[str, float, int]] = []
    for label in ATTEMPTS:
        # Never log the argv containing the actual provider key.
        if subprocess.run(command(label, os.environ[KEY])).returncode:
            raise SystemExit(f"Harbor failed: {label}; retain the incomplete run.")
        score, solved = collect(label)
        rows.append((label, score, solved))
    with (JOBS / "scores.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(("attempt", "score", "solved"))
        w.writerows(rows)
    ref = rows[0][1]
    scores = [s for name, s, _ in rows if name.startswith("frontier")]
    print(
        "Frontier strict wins/ties/losses vs reference:",
        sum(s > ref for s in scores),
        sum(s == ref for s in scores),
        sum(s < ref for s in scores),
    )


if __name__ == "__main__":
    main()

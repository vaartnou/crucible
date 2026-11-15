#!/usr/bin/env python3
"""Run a clean local Fleetbase proof and optionally a separate Harbor oracle.

Uses only the services/files inside an already generated Harbor task. All output,
including failure logs, stays private in --out. Requires Docker Compose and the
project's pinned Harbor 0.26.0. Does not launch Docker from inside an episode.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--harbor", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    task = args.task_dir.resolve()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("--out must be empty: evidence and failed attempts are never overwritten")
    out.mkdir(parents=True, exist_ok=True)
    environment = task / "environment"
    source = yaml.safe_load((environment / "docker-compose.yaml").read_text())
    source["services"].pop(
        "main", None
    )  # Harbor supplies main; local CLI needs no agent container.
    local_compose = environment / ".local-validation-compose.yaml"
    local_compose.write_text(yaml.safe_dump(source, sort_keys=False))
    local_compose.chmod(0o600)
    project = "crucible-fleetbase-smoke-" + uuid4().hex[:12]
    compose = ["docker", "compose", "-p", project, "-f", str(local_compose)]
    state: dict[str, object] = {"project": project, "steps": [], "status": "running"}
    steps: list[dict[str, object]] = []
    state["steps"] = steps

    def save() -> None:
        (out / "validation.json").write_text(json.dumps(state, indent=2) + "\n")

    def run(name: str, command: list[str], *, timeout: float = 1800) -> str:
        start = time.monotonic()
        print(f"{name}: starting", flush=True)
        log = out / f"{name}.log"
        try:
            result = subprocess.run(
                command,
                cwd=task,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            log.write_text(result.stdout)
            steps.append(
                {
                    "name": name,
                    "returncode": result.returncode,
                    "seconds": round(time.monotonic() - start, 3),
                    "log": log.name,
                }
            )
            save()
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect private log {log}")
            print(f"{name}: passed", flush=True)
            return result.stdout
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            log.write_bytes(output.encode() if isinstance(output, str) else output)
            steps.append({"name": name, "timeout": timeout, "log": log.name})
            save()
            raise RuntimeError(f"{name} timed out; inspect private log {log}") from None

    def read_episode(name: str) -> dict[str, object]:
        episodes = list((out / name / "episodes").glob("*.jsonl"))
        if len(episodes) != 1:
            raise RuntimeError(f"{name} did not produce exactly one complete transcript")
        records = [json.loads(line) for line in episodes[0].read_text().splitlines()]
        footer = records[-1]
        if footer["type"] != "footer" or footer["score"]["flags"].get("env_error"):
            raise RuntimeError(f"{name} has an invalid infrastructure outcome")
        return {
            "policy": records[0]["policy"],
            "seed": records[0]["seed"],
            "steps": footer["steps"],
            "score": footer["score"],
            "stop_reason": footer["stop_reason"],
        }

    def local_episode(name: str, policy: str) -> dict[str, object]:
        run(
            name,
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{out}:/tmp/runs",
                "-e",
                f"FLEETBASE_ARTIFACT_DIR=/tmp/runs/evidence/{name}",
                "mcp-server",
                "crucible",
                "eval",
                "--task",
                "fleetbase_dispatch@1",
                "--policy",
                policy,
                "--seeds",
                "0",
                "--concurrency",
                "1",
                "--out",
                f"/tmp/runs/{name}",
            ],
        )
        outcome = read_episode(name)
        run(
            f"{name}-cleanup",
            [*compose, "exec", "-T", "fleetbase-app", "php", "/opt/crucible/count-state.php"],
        )
        return outcome

    error: Exception | None = None
    try:
        run("docker-version", ["docker", "version", "--format", "{{.Server.Version}}"])
        run("compose-version", ["docker", "compose", "version", "--short"])
        run("build", [*compose, "build", "mcp-server", "fleetbase-app"])
        run("start", [*compose, "up", "-d", "--wait", "--wait-timeout", "600", "fleetbase-app"])
        outcomes = {}
        for policy in ("reference", "greedy", "do_nothing", "stop_early"):
            outcomes[policy] = local_episode(policy, policy)
        outcomes["reference_replay"] = local_episode("reference-replay", "reference")
        state["local_outcomes"] = outcomes
        assert outcomes["do_nothing"]["score"]["total"] == 0
        for policy in ("reference", "greedy", "stop_early"):
            assert outcomes[policy]["score"]["total"] > 0, policy
        evidence = []
        for name in ("reference", "reference-replay"):
            paths = list((out / "evidence" / name).glob("*/fleetbase-evidence.json"))
            if len(paths) != 1:
                raise RuntimeError(f"{name} has missing or duplicate private evidence")
            body = json.loads(paths[0].read_text())
            evidence.append({key: body[key] for key in ("events", "economics", "world")})
        assert evidence[0] == evidence[1], "fresh-reset semantic replay diverged"
        state["semantic_replay"] = "equal events, economics and world after independent reset"
        # Public API deletes are native soft deletes. Count active native resources,
        # and private child rows separately through the app's own ORM in a fixture.
        run(
            "service-health-after-cleanup",
            [
                *compose,
                "exec",
                "-T",
                "fleetbase-app",
                "php",
                "-r",
                "exit(@file_get_contents('http://127.0.0.1:8000/int/v1/onboard/should-onboard')"
                " === false ? 1 : 0);",
            ],
        )
        if args.harbor:
            version = run("harbor-version", ["harbor", "--version"]).strip()
            if version != "0.26.0":
                raise RuntimeError(f"expected pinned Harbor 0.26.0, found {version}")
            run(
                "harbor-oracle",
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
                    str(out / "harbor-jobs"),
                    "--job-name",
                    "fleetbase-oracle",
                ],
                timeout=3600,
            )
            rewards = list((out / "harbor-jobs").rglob("reward.txt"))
            grades = list((out / "harbor-jobs").rglob("grade.json"))
            if len(rewards) != 1 or len(grades) != 1:
                raise RuntimeError("Harbor did not produce exactly one verified reward")
            grade = json.loads(grades[0].read_text())
            if (
                float(rewards[0].read_text()) <= 0
                or grade["flags"].get("env_error")
                or grade.get("cleanup_errors")
            ):
                raise RuntimeError("Harbor oracle did not produce a positive valid economic result")
            evidence_paths = list((out / "harbor-jobs").rglob("evidence.json"))
            if len(evidence_paths) != 1:
                raise RuntimeError("Harbor oracle did not preserve trusted physical evidence")
            state["harbor_outcome"] = grade
        state["status"] = "passed"
    except Exception as exc:
        error = exc
        state["status"] = "failed"
        state["error"] = str(exc)
    finally:
        try:
            run("teardown", [*compose, "down", "--volumes", "--remove-orphans"])
        except Exception as exc:
            state["teardown_error"] = str(exc)
            if error is None:
                error = exc
                state["status"] = "failed"
        save()
    if error is not None:
        raise SystemExit(str(error))
    print(f"Validation passed; private evidence: {out / 'validation.json'}", flush=True)


if __name__ == "__main__":
    main()

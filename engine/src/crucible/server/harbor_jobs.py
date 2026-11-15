"""Launch the Harbor CLI and discover its durable job directories. No model loop lives here."""

import asyncio
import json
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from crucible.server.settings import Settings


class LaunchHarborRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export: str = Field(min_length=1, max_length=500)
    model: str = Field(
        default="google/gemini-3.8-flash",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@-]*$",
    )
    concurrency: int = Field(default=2, ge=1, le=4)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return cast(dict[str, Any], value) if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


def _oncall_task(path: Path) -> bool:
    try:
        return (
            tomllib.loads((path / "task.toml").read_text()).get("metadata", {}).get("task_ref")
            == "oncall@1"
        )
    except OSError, ValueError:
        return False


class HarborJobs:
    def __init__(self, settings: Settings) -> None:
        self.jobs_dir = settings.harbor_jobs_dir.resolve()
        self.exports_dir = settings.harbor_exports_dir.resolve()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.lock = asyncio.Lock()

    def exports(self) -> list[dict[str, Any]]:
        if not self.exports_dir.is_dir():
            return []
        exports: list[dict[str, Any]] = []
        for folder in [self.exports_dir, *sorted(self.exports_dir.iterdir())]:
            if not folder.is_dir() or not folder.resolve().is_relative_to(self.exports_dir):
                continue
            tasks = (
                [folder]
                if _oncall_task(folder)
                else [p for p in folder.iterdir() if p.is_dir() and _oncall_task(p)]
            )
            if tasks:
                exports.append(
                    {
                        "id": str(folder.relative_to(self.exports_dir)),
                        "name": folder.name,
                        "tasks": len(tasks),
                    }
                )
        return exports

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for folder in self.jobs_dir.iterdir():
            if not folder.is_dir() or not folder.resolve().is_relative_to(self.jobs_dir):
                continue
            config = _read_json(folder / "config.json")
            launch = _read_json(folder / "crucible-launch.json")
            result = _read_json(folder / "result.json")
            if not config and not launch:
                continue
            process = self.processes.get(folder.name)
            code = process.poll() if process else None
            if result.get("finished_at"):
                status = (
                    "failed" if result.get("stats", {}).get("n_errored_trials", 0) else "completed"
                )
            elif process and code is not None:
                status = "failed" if code else "completed"
            elif process:
                status = "running"
            else:
                status = "unknown"
            agents: list[dict[str, Any]] = config.get("agents") or []
            agent: dict[str, Any] = agents[0] if agents else {}
            rows.append(
                {
                    "id": folder.name,
                    "path": str(folder),
                    "status": status,
                    "agent": agent.get("name") or launch.get("agent", "unknown"),
                    "model": agent.get("model_name") or launch.get("model"),
                    "started_at": result.get("started_at") or launch.get("started_at", ""),
                    "completed_trials": result.get("stats", {}).get("n_completed_trials", 0),
                    "total_trials": result.get("n_total_trials"),
                }
            )
        return sorted(rows, key=lambda row: str(row["started_at"]), reverse=True)

    def describe(self) -> dict[str, Any]:
        return {
            "jobs_dir": str(self.jobs_dir),
            "exports_dir": str(self.exports_dir),
            "harbor_available": shutil.which("harbor") is not None,
            "exports": self.exports(),
            "jobs": self.list_jobs(),
        }

    async def launch(self, req: LaunchHarborRequest) -> dict[str, Any]:
        async with self.lock:
            binary = shutil.which("harbor")
            if not binary:
                raise HTTPException(503, "Harbor is not installed on the UI server.")
            export = (self.exports_dir / req.export).resolve()
            available = {item["id"] for item in await asyncio.to_thread(self.exports)}
            if not export.is_relative_to(self.exports_dir) or req.export not in available:
                raise HTTPException(400, "Choose a prepared oncall Harbor export from the list.")
            if any(proc.poll() is None for proc in self.processes.values()):
                raise HTTPException(409, "A Harbor job launched here is still running.")
            try:
                check = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except OSError, subprocess.TimeoutExpired:
                raise HTTPException(
                    503, "Docker is unavailable. Start Docker Desktop and retry."
                ) from None
            if check.returncode:
                raise HTTPException(503, "Docker is unavailable. Start Docker Desktop and retry.")
            name = f"oncall-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
            folder = self.jobs_dir / name
            await asyncio.to_thread(folder.mkdir, parents=True)
            command = [
                binary,
                "run",
                "--path",
                str(export),
                "--agent",
                "opencode",
                "--model",
                req.model,
                "--ak",
                "version=1.19.5",
                "--env",
                "docker",
                "--jobs-dir",
                str(self.jobs_dir),
                "--job-name",
                name,
                "--n-concurrent",
                str(req.concurrency),
            ]
            # Credentials are inherited from the server environment, never placed in arguments.
            try:
                with (folder / "launcher.log").open("wb") as log:
                    process = await asyncio.to_thread(
                        subprocess.Popen,
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError:
                raise HTTPException(
                    503, f"Could not start Harbor. See {folder / 'launcher.log'}."
                ) from None
            self.processes[name] = process
            (folder / "crucible-launch.json").write_text(
                json.dumps(
                    {
                        "agent": "opencode",
                        "model": req.model,
                        "export": str(export),
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
            return {"id": name, "path": str(folder), "status": "running"}

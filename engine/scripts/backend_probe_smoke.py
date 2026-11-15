"""Run the external HTTP fixture in a separate process, verify it, and clean up.

Run from engine: `.venv/bin/python scripts/backend_probe_smoke.py`.
Outputs are private run artifacts, not benchmark findings.
"""

import asyncio
import json
import os
import secrets
import socket
import sys
from pathlib import Path

import httpx

from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job


async def main() -> None:
    output = Path("runs/backend-probe-local")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_hex(32)
    environment = {
        **os.environ,
        "BACKEND_PROBE_TOKEN": token,
        "BACKEND_PROBE_URL": url,
        "BACKEND_PROBE_LOCK_DIR": str(output / "leases"),
        "BACKEND_PROBE_ARTIFACTS": str(output / "evidence"),
    }
    for name in (
        "BACKEND_PROBE_TOKEN",
        "BACKEND_PROBE_URL",
        "BACKEND_PROBE_LOCK_DIR",
        "BACKEND_PROBE_ARTIFACTS",
    ):
        os.environ[name] = environment[name]
    with (output / "application.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "crucible.envs.backend_probe.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=1) as client:
                async with asyncio.timeout(15):
                    while True:
                        if process.returncode is not None:
                            raise RuntimeError("Fixture application exited during startup")
                        try:
                            if (await client.get("/healthz")).is_success:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                jobs = [
                    Job(task="backend_probe@1", policy=policy, seed=0)
                    for policy in ("reference", "do_nothing")
                ]
                results = await LocalExecutor().run(jobs, output / "evaluation")
                assert [result.score.total for result in results] == [0.9, 0.0]
                cleaned = 0
                for artifact in (output / "evidence").glob("*/application.json"):
                    response = await client.get(
                        f"/episodes/{artifact.parent.name}", headers={"X-Backend-Token": token}
                    )
                    assert response.status_code == 404
                    cleaned += 1
                assert cleaned == 2
                proof = {
                    "fixture_only": True,
                    "transport": "HTTP to separate uvicorn process",
                    "rewards": {result.policy: result.score.total for result in results},
                    "episode_records_released": cleaned,
                    "service_alive_after_episode_cleanup": (
                        await client.get("/healthz")
                    ).is_success,
                }
                (output / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
                print(json.dumps(proof, indent=2))
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    asyncio.run(main())

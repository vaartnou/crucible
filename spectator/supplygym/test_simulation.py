"""HTTP integration coverage for the local interactive SupplyGym viewer.

Run with the engine dependencies installed and engine/src on PYTHONPATH:
    python -m unittest discover -s spectator/supplygym -p test_simulation.py
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


class Server:
    def __init__(self, *, no_engine=False):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(HERE.parents[1] / "engine" / "src")
        args = [sys.executable]
        if no_engine:
            args.append("-S")
            env.pop("PYTHONPATH", None)
        self.temp = tempfile.TemporaryDirectory()
        args += [str(HERE / "serve.py"), "--port", "0", "--output-dir", self.temp.name]
        self.process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env
        )
        lines = queue.Queue()
        threading.Thread(
            target=lambda: lines.put(self.process.stdout.readline()), daemon=True
        ).start()
        try:
            line = lines.get(timeout=30).strip()
        except queue.Empty:
            self.close()
            raise RuntimeError("Viewer startup timed out") from None
        if "http://127.0.0.1:" not in line:
            self.close()
            raise RuntimeError(f"Viewer failed to start: {line}")
        self.origin = line[line.index("http://") :].rstrip("/")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=15)
        self.temp.cleanup()

    def request(self, path, payload=None, origin=None):
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        req = Request(self.origin + path, data=data, headers=headers)
        try:
            with urlopen(req, timeout=30) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def api(self, path="/api/simulation", payload=None, origin=None):
        status, body = self.request(path, payload, origin)
        return status, json.loads(body)

    def wait(self, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.api()
            run = state.get("run")
            if run and predicate(run):
                return run
            time.sleep(0.05)
        self_test = state.get("run")
        raise AssertionError(f"Timed out waiting for simulation: {self_test}")

    def start(self, seed=42):
        status, body = self.api(payload={"seed": seed, "controls": {}})
        assert status == 202, (status, body)
        return self.wait(lambda run: run["status"] != "preparing")

    def event(self, run, action, payload=None):
        return self.api(f"/api/simulation/{run['id']}/{action}", payload or {})

    def json_url(self, path):
        status, body = self.request(path)
        assert status == 200, (status, path, body)
        return json.loads(body)

    def records(self, path):
        status, body = self.request(path)
        assert status == 200, (status, path, body)
        return [json.loads(line) for line in body.splitlines() if line]


class SimulationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_real_session_lifecycle_and_scores(self):
        server = self.server
        run = server.start()
        self.assertEqual(run["status"], "ready", run)
        self.assertEqual(run["week"], 0)
        manifest = server.json_url(run["manifest_url"])
        self.assertEqual(len(manifest), 1)
        entry = manifest[0]
        self.assertEqual(
            set(entry["events"]),
            {"manual", "ss", "base_stock", "fixed_lot", "fill_adjusted_base_stock"},
        )
        paths = {name: urljoin(run["manifest_url"], path) for name, path in entry["events"].items()}
        baseline_finals = {}
        for name in set(paths) - {"manual"}:
            records = server.records(paths[name])
            self.assertEqual(len([r for r in records if r["event"] == "week"]), 53)
            self.assertTrue(records[-1]["completed"])
            baseline_finals[name] = records[-1]
            self.assertEqual(
                {r["scenario_comparison_id"] for r in records},
                {entry["scenario_comparison_id"]},
            )
            self.assertEqual({r["strategy"] for r in records}, {name})
        self.assertEqual(server.records(paths["manual"]), [])
        for controls in (
            {"safety_stock_days": -1},
            {"unknown": 1},
            {"order_multiplier": "1"},
            {"approve_alternative": "A02"},
        ):
            status, _ = server.event(run, "step", {"controls": controls})
            self.assertGreaterEqual(status, 400, controls)
            self.assertEqual(server.api()[1]["run"]["week"], 0)
            self.assertEqual(server.records(paths["manual"]), [])
        for week in range(1, 54):
            status, state = server.event(run, "step", {"controls": {}})
            self.assertEqual(status, 200, state)
            self.assertEqual(state["run"]["week"], week)
        self.assertEqual(state["run"]["status"], "completed")
        records = server.records(paths["manual"])
        self.assertEqual([r["week"] for r in records[:-1]], list(range(1, 54)))
        self.assertEqual(
            {r["scenario_comparison_id"] for r in records},
            {entry["scenario_comparison_id"]},
        )
        final = records[-1]
        self.assertTrue(final["completed"])
        self.assertEqual(final["score"], baseline_finals["ss"]["score"])
        ledger = final["verified_accounting"]
        self.assertEqual(final["score"]["breakdown"], ledger)
        self.assertAlmostEqual(
            records[-2]["cumulative_operating_surplus"], ledger["operating_surplus"]
        )
        self.assertAlmostEqual(
            sum(r["weekly_operating_surplus"] for r in records[:-1]),
            ledger["operating_surplus"],
        )
        self.assertAlmostEqual(
            ledger["operating_surplus"],
            ledger["revenue"]
            - sum(ledger[k] for k in ("purchase", "holding", "stockout", "expedite", "switching")),
        )
        self.assertAlmostEqual(
            final["score"]["total"],
            max(0, min(1, ledger["operating_surplus"] / ledger["surplus_upper_bound"])),
        )

        previous_id = run["id"]
        second = server.start()
        self.assertEqual(second["status"], "ready", second)
        self.assertNotEqual(previous_id, second["id"])
        second_manifest = server.json_url(second["manifest_url"])[0]
        self.assertEqual(entry["scenario_comparison_id"], second_manifest["scenario_comparison_id"])
        status, _ = server.api(f"/api/simulation/{previous_id}/step", {"controls": {}})
        self.assertEqual(status, 404)
        self.assertEqual(server.api()[1]["run"]["week"], 0)

        # Repeating the same seed and controls reproduces the score in an isolated run.
        status, state = server.event(second, "run", {"controls": {}})
        self.assertEqual(status, 202, state)
        completed = server.wait(lambda r: r["status"] in ("completed", "error"))
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(completed["week"], 53)
        second_path = urljoin(second["manifest_url"], second_manifest["events"]["manual"])
        self.assertEqual(server.records(second_path)[-1]["score"], final["score"])

        second = server.start(seed=43)
        self.assertEqual(second["status"], "ready", second)
        second_manifest = server.json_url(second["manifest_url"])[0]
        # Simultaneous mutations must serialize and preserve one record per week.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: server.event(second, "step", {"controls": {}}), range(2))
            )
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(server.api()[1]["run"]["week"], 2)
        manual = urljoin(second["manifest_url"], second_manifest["events"]["manual"])
        self.assertEqual([r["week"] for r in server.records(manual)], [1, 2])
        # The pause response must be a barrier: no later background step may advance it.
        status, _ = server.event(second, "run", {"controls": {}})
        self.assertEqual(status, 202)
        server.wait(lambda r: r["week"] >= 1 or r["status"] != "running")
        status, paused = server.event(second, "pause")
        self.assertEqual(status, 200, paused)
        self.assertEqual(paused["run"]["status"], "ready")
        week = paused["run"]["week"]
        time.sleep(0.15)
        self.assertEqual(server.api()[1]["run"]["week"], week)
        status, cancelled = server.event(second, "cancel")
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["run"]["status"], "cancelled")
        manual = urljoin(second["manifest_url"], second_manifest["events"]["manual"])
        final = server.records(manual)[-1]
        self.assertFalse(final["completed"])
        self.assertEqual(final["score"]["total"], 0)
        self.assertGreaterEqual(server.event(second, "step", {"controls": {}})[0], 400)

        status, preparing = server.api(payload={"seed": 44, "controls": {}})
        self.assertEqual(status, 202)
        self.assertEqual(preparing["run"]["status"], "preparing")
        status, cancelled = server.event(preparing["run"], "cancel")
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["run"]["status"], "cancelled")
        time.sleep(0.1)
        self.assertEqual(server.api()[1]["run"]["status"], "cancelled")

    def test_unavailable_without_engine(self):
        server = Server(no_engine=True)
        try:
            status, state = server.api()
            self.assertEqual(status, 200)
            self.assertFalse(state["available"])
            status, _ = server.api(payload={"seed": 42, "controls": {}})
            self.assertGreaterEqual(status, 400)
        finally:
            server.close()

    def test_private_paths_and_cross_origin_post_are_rejected(self):
        for path in (
            "/run/private.json",
            "/run/%2e%2e/private.json",
            "/run/live/seed-42/../private.json",
            "/simulation.py",
            "/serve.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.server.request(path)[0], 404)
        status, _ = self.server.api(
            payload={"seed": 42, "controls": {}}, origin="https://attacker.example"
        )
        self.assertGreaterEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

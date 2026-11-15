"""Serve the SupplyGym viewer and public spectator files on localhost."""

import argparse
import json
import mimetypes
import re
import sys
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

UI = Path(__file__).resolve().parent
ASSETS = {"index.html", "app.js", "feed.js", "ui.js", "controls.js", "style.css"}
RUN_FILE = re.compile(
    r"(?:spectator-manifest\.json|live/seed-\d+/events\.jsonl|"
    r"baselines/seed-\d+/(?:reference|ss|base_stock|fixed_lot|fill_adjusted_base_stock|stop_early|do_nothing)/events\.jsonl)"
)
SIM_FILE = re.compile(r"simulations/([0-9a-f]{32})/(.+)")
API_ACTION = re.compile(r"/api/simulation/([0-9a-f]{32})/(step|run|pause|cancel)")


class ViewerHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, run_dir=None, controller=None, unavailable=None, **kwargs):
        self.run_dir, self.controller, self.unavailable = run_dir, controller, unavailable
        super().__init__(*args, **kwargs)

    def send_json(self, data, status=200):
        body = json.dumps(data, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/simulation":
            if self.controller:
                self.send_json(self.controller.call(self.controller.get()))
            else:
                self.send_json({"available": False, "run": None, "error": self.unavailable})
            return
        self.send_asset()

    def do_HEAD(self):
        self.send_asset(head_only=True)

    def do_POST(self):
        path = urlsplit(self.path).path
        match = API_ACTION.fullmatch(path)
        if path != "/api/simulation" and not match:
            self.send_error(404)
            return
        # Reject DNS rebinding and cross-origin browser requests.
        # Non-browser clients may omit Origin.
        host = self.headers.get("Host", "")
        valid_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        origin = self.headers.get("Origin")
        if host not in valid_hosts or (origin is not None and origin != "http://" + host):
            self.send_json({"error": "same-origin localhost request required"}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "application/json required"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > 16384:
            self.send_json({"error": "invalid request body length"}, 413)
            return
        try:
            body = json.loads(
                self.rfile.read(length),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "invalid JSON object"}, 400)
            return
        if not self.controller:
            self.send_json({"error": self.unavailable}, 503)
            return
        try:
            if path == "/api/simulation":
                if set(body) != {"seed", "controls"}:
                    raise ValueError("seed and controls are required")
                result = self.controller.call(
                    self.controller.create(body["seed"], body["controls"])
                )
                status = 202
            else:
                ident, action = match.groups()
                expected_keys = {"controls"} if action in ("step", "run") else set()
                if set(body) != expected_keys:
                    raise ValueError("invalid request fields")
                result = self.controller.call(
                    self.controller.action(ident, action, body.get("controls"))
                )
                status = 202 if action == "run" else 200
            self.send_json(result, status)
        except Exception as exc:
            self.send_json({"error": str(exc)}, getattr(exc, "status", 400))

    def send_asset(self, head_only=False):
        path = urlsplit(self.path).path.removeprefix("/") or "index.html"
        match = SIM_FILE.fullmatch(path)
        if path in ASSETS:
            root, name = UI, path
        elif path.startswith("run/") and self.run_dir and RUN_FILE.fullmatch(path[4:]):
            root, name = self.run_dir, path[4:]
        elif match and self.controller and RUN_FILE.fullmatch(match.group(2)):
            root, name = self.controller.output / match.group(1), match.group(2)
            root = root.resolve()
            if not root.is_relative_to(self.controller.output):
                self.send_error(404)
                return
        elif path.startswith("demo/") and re.fullmatch(
            r"[A-Za-z0-9_/-]+\.(?:jsonl|json)(?:\.gz)?", path[5:]
        ):
            root, name = UI / "demo", path[5:]
        else:
            self.send_error(404)
            return
        asset = (root / name).resolve()
        if not asset.is_relative_to(root) or not asset.is_file():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        if asset.suffix == ".js":
            mime = "text/javascript"
        with asset.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only and size:
                self.wfile.write(stream.read(size))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="output directory from the demo runner")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/supplygym-ui"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    sys.path.insert(0, str(UI.parents[1] / "engine" / "src"))
    controller = None
    unavailable = None
    try:
        from simulation import Controller

        controller = Controller(args.output_dir)
    except (ImportError, ModuleNotFoundError, SyntaxError) as exc:
        unavailable = (
            f"Simulation requires the engine dependencies in a compatible Python interpreter: {exc}"
        )
    run_dir = args.run_dir.resolve() if args.run_dir else None
    handler = partial(
        ViewerHandler, run_dir=run_dir, controller=controller, unavailable=unavailable
    )
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"SupplyGym viewer at http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if controller:
            controller.close()


if __name__ == "__main__":
    main()

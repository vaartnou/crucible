"""Serve the SupplyGym viewer and public spectator files on localhost."""

import argparse
import mimetypes
import re
import shutil
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


UI = Path(__file__).resolve().parent
ASSETS = {"index.html", "app.js", "feed.js", "ui.js", "style.css"}
RUN_FILE = re.compile(
    r"(?:spectator-manifest\.json|live/seed-\d+/events\.jsonl|"
    r"baselines/seed-\d+/(?:reference|ss|base_stock|fixed_lot|fill_adjusted_base_stock|stop_early|do_nothing)/events\.jsonl)"
)


class ViewerHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, run_dir=None, **kwargs):
        self.run_dir = run_dir
        super().__init__(*args, **kwargs)

    def do_GET(self):
        self.send_asset()

    def do_HEAD(self):
        self.send_asset(head_only=True)

    def send_asset(self, head_only=False):
        path = urlsplit(self.path).path.removeprefix("/") or "index.html"
        if path in ASSETS:
            root, name = UI, path
        elif path.startswith("run/") and self.run_dir and RUN_FILE.fullmatch(path[4:]):
            root, name = self.run_dir, path[4:]
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
            # Snapshot the length: Harbor may append another record during a read.
            size = stream.seek(0, 2)
            stream.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only and size:
                if path.startswith("run/"):
                    self.wfile.write(stream.read(size))
                else:
                    shutil.copyfileobj(stream, self.wfile)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="output directory from the PR 37 demo runner")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve() if args.run_dir else None
    handler = partial(ViewerHandler, run_dir=run_dir)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"SupplyGym viewer at http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

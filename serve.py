"""
Tiny static-file + refresh server. Replaces `python -m http.server`.

  GET  /<path>     -> serves static files from the project directory
  POST /refresh    -> runs fetch_data.py in a subprocess and returns JSON

Use:  python serve.py [port]   (default 8000)
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
FETCH_SCRIPT = ROOT / "fetch_data.py"
PYTHON = sys.executable  # same venv that's running this server

_refresh_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/refresh":
            self.send_error(404, "only POST /refresh is supported")
            return

        # Prevent overlapping fetches; one user clicks twice or a watcher fires.
        if not _refresh_lock.acquire(blocking=False):
            self._json(409, {"status": "busy", "message": "a refresh is already running"})
            return
        try:
            t0 = time.time()
            proc = subprocess.run(
                [PYTHON, str(FETCH_SCRIPT)],
                capture_output=True, text=True, cwd=str(ROOT),
                env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                timeout=600,
            )
            dur = round(time.time() - t0, 1)
            # Tail the output so the client can show what happened without us
            # streaming megabytes back.
            tail_lines = (proc.stdout or "").splitlines()[-25:]
            payload = {
                "status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "duration_s": dur,
                "stdout_tail": "\n".join(tail_lines),
                "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-10:]),
            }
            # Try to attach the manifest so the client can show counts.
            manifest_path = ROOT / "data" / "manifest.json"
            if manifest_path.exists():
                try:
                    payload["manifest"] = json.loads(manifest_path.read_text())
                except Exception:
                    pass
            self._json(200 if proc.returncode == 0 else 500, payload)
        except subprocess.TimeoutExpired:
            self._json(504, {"status": "error", "message": "fetch timed out after 600s"})
        except Exception as e:
            self._json(500, {"status": "error", "message": repr(e)})
        finally:
            _refresh_lock.release()

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # SimpleHTTPRequestHandler caches forever by default for static files;
    # tell the browser data/* is volatile so a refresh actually re-loads it.
    def end_headers(self):
        if self.path.startswith("/data/") or self.path == "/data":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = partial(Handler, directory=str(ROOT))
    with ThreadingHTTPServer(("", port), handler) as httpd:
        print(f"serving {ROOT} on http://localhost:{port}/")
        print(f"  POST http://localhost:{port}/refresh  to re-fetch data")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

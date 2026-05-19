"""
Desktop launcher.

Wraps fetch_data.py + serve.py + index.html in a single native window
(via pywebview). Works on Windows, macOS, and Linux. Falls back to
opening the user's default browser if pywebview can't load.

  python app.py

On first launch, runs the data fetcher if data/manifest.json is missing
so the map opens populated. On subsequent launches it opens immediately.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

# Force UTF-8 stdout before any third-party imports (Herbie's first-run
# banner crashes on Windows cp1252 otherwise).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from serve import Handler  # reuse the same handler that powers `python serve.py`


def find_free_port() -> int:
    """Bind to port 0, let the OS pick, return the picked port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> ThreadingHTTPServer:
    """Start serve.py's handler in a daemon thread. Returns the server."""
    handler = partial(Handler, directory=str(ROOT))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


def ensure_data() -> None:
    """Run fetch_data.py once if the data directory is empty."""
    manifest = ROOT / "data" / "manifest.json"
    if manifest.exists():
        return
    print("[app] No data found. Running fetch_data.py (one-time, ~30s)…")
    r = subprocess.run(
        [sys.executable, str(ROOT / "fetch_data.py")],
        cwd=str(ROOT),
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if r.returncode != 0:
        print("[app] Initial fetch failed. The map will open but layers may be missing.")


def open_window(url: str) -> bool:
    """Try to open a native window via pywebview. Return True if it worked."""
    try:
        import webview  # pywebview
    except ImportError:
        print("[app] pywebview not installed. Install it with:")
        print("[app]   pip install -r requirements-desktop.txt")
        return False
    try:
        webview.create_window(
            "California Grid Weather Map",
            url,
            width=1400, height=900,
            min_size=(900, 600),
        )
        # pywebview.start() blocks until the window is closed.
        webview.start()
        return True
    except Exception as e:
        print(f"[app] pywebview failed to start ({e}); falling back to browser.")
        return False


def main() -> int:
    ensure_data()
    port = find_free_port()
    httpd = start_server(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"[app] Serving on {url}")

    if not open_window(url):
        # Fallback path: open the user's default browser and idle until Ctrl-C.
        webbrowser.open(url)
        print("[app] Press Ctrl-C to stop the server.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("[app] Interrupted.")

    httpd.shutdown()
    print("[app] Server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

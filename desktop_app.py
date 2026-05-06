"""
CHAPPIE — desktop entry point.

Starts the FastAPI backend in a background thread and opens a native
window pointing at it.  Use this for the "real app" experience instead
of running uvicorn manually and opening a browser tab.

Runs in two layouts:
  - Dev:        python desktop_app.py  (loads backend/ from disk)
  - Packaged:   CHAPPIE.exe            (bundled by PyInstaller; uses sys._MEIPASS)

If pywebview / pystray aren't installed it falls back gracefully to the
default browser so the app still works on a fresh checkout.
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------
# Resource-path resolver (handles both dev and PyInstaller bundles)
# ---------------------------------------------------------------------

def _resource_root() -> Path:
    """Where bundled resources (backend/, frontend/, icon.png) live."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _user_data_root() -> Path:
    """Writable directory for the SQLite DB, logs, browser_profile/, etc."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    root = base / "CHAPPIE"
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    return root


ROOT = _resource_root()
BACKEND = ROOT / "backend"
USER_DATA = _user_data_root()

# Make backend/ importable
sys.path.insert(0, str(BACKEND))
# Run from backend/ so config.json is found relative to main.py — but write
# data/, logs/, data/browser_profile/ into the per-user folder so multiple
# launches and packaged installs don't clobber each other.
os.chdir(BACKEND)

# Redirect persistent paths at the user-data dir before main is imported.
os.environ.setdefault("CHAPPIE_DATA_DIR", str(USER_DATA / "data"))
os.environ.setdefault("CHAPPIE_LOG_DIR", str(USER_DATA / "logs"))


# ---------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------

def _pick_port(preferred: int = 8765) -> int:
    """Use `preferred` if free, else any free port."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", preferred))
    except OSError:
        s.close()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(url: str, timeout: float = 25) -> bool:
    import urllib.request
    import urllib.error

    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    return False


def _run_server(port: int) -> None:
    import uvicorn
    from main import app  # noqa: E402  (path tweak above is required)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------

WINDOW_TITLE = "CHAPPIE"
INITIAL_WIDTH = 1180
INITIAL_HEIGHT = 780
MIN_WIDTH = 720
MIN_HEIGHT = 480


def _open_window(url: str) -> None:
    try:
        import webview  # type: ignore
    except ImportError:
        # Graceful fallback — open in default browser. Server keeps running.
        print("pywebview not installed; opening in your default browser.")
        print("Install with:  pip install pywebview")
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        return

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=INITIAL_WIDTH,
        height=INITIAL_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        resizable=True,
        confirm_close=False,
    )

    # Try GUI backends in order: edgechromium (Win), cocoa (Mac), qt (Linux).
    gui = None
    if sys.platform == "win32":
        gui = "edgechromium"
    elif sys.platform == "darwin":
        gui = "cocoa"
    try:
        webview.start(gui=gui, debug=False)
    except Exception:
        # Fall back to the default backend
        webview.start(debug=False)
    _ = window  # silence linter


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    port = _pick_port(8765)
    server_thread = threading.Thread(
        target=_run_server, args=(port,), daemon=True, name="chappie-uvicorn"
    )
    server_thread.start()

    health_url = f"http://127.0.0.1:{port}/health"
    if not _wait_for_server(health_url):
        print(f"CHAPPIE backend didn't come up at {health_url}.")
        print(f"Look in {USER_DATA / 'logs'} for details.")
        sys.exit(1)

    _open_window(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    main()

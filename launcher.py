#!/usr/bin/env python3
"""Desktop launcher for the VR-N7600 web UI.

Runs the app like an application instead of a dev server: it starts the local
server in the background and opens the UI in its own native window (falling
back to your default browser if no desktop webview is available). Closing the
window stops everything.

    python launcher.py           # run it
    (or double-click the packaged VR-N7600 Remote executable — see build.py)

The server still listens on 0.0.0.0:<port> so phones / other devices on the
LAN can reach the same UI at http://<this-machine>:<port>.
"""
from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from app import app, config, log

PORT = int(config.get("port", 8099))
LOCAL_URL = f"http://127.0.0.1:{PORT}"


def _alert(msg: str) -> None:
    """Get a message in front of the user even with no console (windowed exe)."""
    print(msg)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, "VR-N7600 Remote", 0x10)
        except Exception:                     # noqa: BLE001
            pass


def _port_in_use() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), 0.5):
            return True
    except OSError:
        return False


def _is_our_app() -> bool:
    """True if whatever answers on PORT is a VR-N7600 bridge already."""
    try:
        with urllib.request.urlopen(f"{LOCAL_URL}/api/auth/check",
                                    timeout=2) as r:
            return b'"ok"' in r.read(200)
    except Exception:                         # noqa: BLE001
        return False


def _start_server() -> uvicorn.Server:
    # log_config=None: use the root logging config from app.py (file log when
    # frozen) — uvicorn's own default handlers write to sys.stderr, which is
    # None in a windowed exe.
    cfg = uvicorn.Config(app, host="0.0.0.0", port=PORT,
                         log_level="info", log_config=None)
    server = uvicorn.Server(cfg)
    # signal handlers can only be installed on the main thread; we run the
    # server on a worker thread, so disable them.
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    return server


def _wait_until_up(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_in_use():
            return True
        time.sleep(0.1)
    return False


def _open_ui() -> None:
    """Native window if possible, otherwise the default browser."""
    try:
        import webview  # WebView2 on Windows, GTK/Qt on Linux
        webview.create_window("VR-N7600 Remote", LOCAL_URL,
                              width=1200, height=840, min_size=(900, 600))
        webview.start()               # blocks until the window is closed
    except Exception as e:            # noqa: BLE001 - any GUI failure -> browser
        import webbrowser
        log.warning("no desktop window (%s) — using the browser", e)
        print(f"VR-N7600 Remote is running at {LOCAL_URL}")
        print("Opening your browser. Press Ctrl+C here to stop.")
        webbrowser.open(LOCAL_URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def main():
    if _port_in_use():
        if _is_our_app():
            # second launch = "bring it up", not a port-clash crash
            log.info("bridge already running on %s — opening a window to it",
                     PORT)
            _open_ui()
            return
        _alert(f"Port {PORT} is already in use by another program.\n"
               f"Change \"port\" in config.json (next to the exe) and retry.")
        return

    server = _start_server()
    try:
        if not _wait_until_up():
            _alert(f"The bridge server failed to start on port {PORT}.\n"
                   f"See vrn7600.log next to the exe for details.")
            return
        _open_ui()
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()

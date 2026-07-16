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

import socket
import threading
import time

import uvicorn

from app import app, config

PORT = int(config.get("port", 8099))
LOCAL_URL = f"http://127.0.0.1:{PORT}"


def _start_server() -> uvicorn.Server:
    cfg = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    # signal handlers can only be installed on the main thread; we run the
    # server on a worker thread, so disable them.
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    return server


def _wait_until_up(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    server = _start_server()
    if not _wait_until_up():
        print("server did not come up in time")
        return

    try:
        import webview  # native desktop window (WebView2 on Windows, GTK/Qt on Linux)
        webview.create_window("VR-N7600 Remote", LOCAL_URL,
                              width=1200, height=840, min_size=(900, 600))
        webview.start()               # blocks until the window is closed
    except Exception as e:            # noqa: BLE001 - any GUI failure -> browser
        import webbrowser
        print(f"(no desktop window: {e})")
        print(f"VR-N7600 Remote is running at {LOCAL_URL}")
        print("Opening your browser. Press Ctrl+C here to stop.")
        webbrowser.open(LOCAL_URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()

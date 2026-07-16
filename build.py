#!/usr/bin/env python3
"""Build the VR-N7600 web UI into a double-click desktop application.

    python build.py

Produces a single self-contained executable under dist/:
    Windows : dist/VR-N7600 Remote.exe
    Linux   : dist/VR-N7600 Remote

No Python install needed on the target machine. Ship the executable together
with a config.json (or let the app create one next to it on first run).
Requires: pyinstaller, and the app's own deps, in the current environment.
"""
import os

import PyInstaller.__main__

SEP = ";" if os.name == "nt" else ":"   # PyInstaller --add-data separator

args = [
    "launcher.py",
    "--name=VR-N7600 Remote",
    "--onefile",
    "--windowed",                        # no console window (GUI app)
    "--noconfirm",
    f"--add-data=static{SEP}static",     # bundle the web UI assets
    # bleak pulls platform backends in dynamically:
    "--collect-all=bleak",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=uvicorn.protocols.http.auto",
    "--hidden-import=uvicorn.protocols.websockets.auto",
    "--hidden-import=uvicorn.lifespan.on",
    "--hidden-import=websockets",
]

try:                                     # native window is optional — the
    import webview  # noqa: F401         # launcher falls back to the browser
    args.append("--collect-all=webview")
except ImportError:
    print("pywebview not installed — building browser-fallback binary")

if os.name == "nt":
    # AOC voice bridge (audio.py) imports these inside a try block, which
    # PyInstaller's static analysis can miss:
    args += [
        "--hidden-import=winrt.windows.devices.bluetooth",
        "--hidden-import=winrt.windows.devices.bluetooth.rfcomm",
        "--hidden-import=winrt.windows.networking.sockets",
        "--hidden-import=winrt.windows.storage.streams",
        "--hidden-import=winrt.windows.foundation",
        "--hidden-import=winrt.windows.foundation.collections",
    ]

PyInstaller.__main__.run(args)
print("\nBuilt. Run it from dist/ — it opens the UI in its own window.")

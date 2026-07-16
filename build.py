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
import PyInstaller.__main__
import os

SEP = ";" if os.name == "nt" else ":"   # PyInstaller --add-data separator

PyInstaller.__main__.run([
    "launcher.py",
    "--name=VR-N7600 Remote",
    "--onefile",
    "--windowed",                        # no console window (GUI app)
    "--noconfirm",
    f"--add-data=static{SEP}static",     # bundle the web UI assets
    # bleak / pywebview pull platform backends in dynamically:
    "--collect-all=bleak",
    "--collect-all=webview",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=uvicorn.protocols.http.auto",
    "--hidden-import=uvicorn.protocols.websockets.auto",
    "--hidden-import=uvicorn.lifespan.on",
])
print("\nBuilt. Run it from dist/ — it opens the UI in its own window.")

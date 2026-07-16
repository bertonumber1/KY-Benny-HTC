#!/usr/bin/env python3
"""Advanced-settings reverse-engineering harness for the VR-N7600.

Decodes the blocks benshi.py doesn't model yet — READ_ADVANCED_SETTINGS (29),
READ_ADVANCED_SETTINGS2 (63), READ_RDA1846S_AGC (37) — by capturing them and
diffing across changes ("differential RE").

    python adv_re.py capture [label]     # read every block 3x; show volatile
                                         # (live) vs static bytes; save to
                                         # captures/adv-<label>-<ts>.json
    python adv_re.py diff A B            # byte-diff two saved capture files,
                                         # highlighting exactly what moved

Workflow to map a field:
    1) python adv_re.py capture before
    2) change ONE setting on the radio (front panel or the web UI)
    3) python adv_re.py capture after
    4) python adv_re.py diff captures/adv-before-*.json captures/adv-after-*.json
    The bytes that changed are that setting's location.

Radio must be awake / in range (BLE). The web UI must be DISCONNECTED so this
tool can hold the single link.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

from benshi import Cmd
from radio import Radio

MAC = "38:D2:00:01:3E:CC"
CAP_DIR = pathlib.Path(__file__).parent / "captures"
BLOCKS = [("ADV_SETTINGS",  Cmd.READ_ADVANCED_SETTINGS,  b""),
          ("ADV_SETTINGS2", Cmd.READ_ADVANCED_SETTINGS2, b""),
          ("AGC",           Cmd.READ_RDA1846S_AGC,        b""),
          ("FREQ_RANGE",    Cmd.READ_FREQ_RANGE,          b"\x00")]


async def capture(label: str):
    CAP_DIR.mkdir(exist_ok=True)
    r = Radio()
    await r.connect(MAC, "ble")
    print(f"connected. reading {len(BLOCKS)} blocks x3 ...\n")
    out = {}
    for name, cmd, pl in BLOCKS:
        reads = []
        for _ in range(3):
            reads.append((await r._command(cmd, pl, timeout=3.0)).hex())
            await asyncio.sleep(0.6)
        b = [bytes.fromhex(x) for x in reads]
        n = len(b[0])
        volatile = [i for i in range(n) if len({x[i] for x in b}) > 1]
        out[name] = {"hex": reads[0], "len": n, "volatile": volatile}
        print(f"{name:14s} {n:3d}B  volatile bytes: {volatile or 'none (static)'}")
    await r.disconnect()
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = CAP_DIR / f"adv-{label}-{ts}.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"\nsaved {path}")


def diff(a_path: str, b_path: str):
    a = json.loads(pathlib.Path(a_path).read_text())
    b = json.loads(pathlib.Path(b_path).read_text())
    for name in a:
        if name not in b:
            continue
        ba = bytes.fromhex(a[name]["hex"])
        bb = bytes.fromhex(b[name]["hex"])
        changed = [(i, ba[i], bb[i]) for i in range(min(len(ba), len(bb)))
                   if ba[i] != bb[i]]
        vol = set(a[name]["volatile"]) | set(b[name]["volatile"])
        real = [(i, x, y) for i, x, y in changed if i not in vol]
        if not changed:
            print(f"{name}: no change")
            continue
        print(f"{name}: {len(changed)} byte(s) changed "
              f"({len(real)} excluding known-volatile):")
        for i, x, y in changed:
            tag = "  (volatile)" if i in vol else "  <== candidate field"
            print(f"    [{i:3d}] {x:02x} -> {y:02x}{tag}")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "capture":
        asyncio.run(capture(sys.argv[2] if len(sys.argv) > 2 else "cap"))
    elif len(sys.argv) == 4 and sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

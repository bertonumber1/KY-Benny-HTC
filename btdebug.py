#!/usr/bin/env python3
"""Bluetooth sniffer / debugger for the VR-N7600 / Benshi radio.

A standalone protocol probe that reuses the project's own framing/command
definitions (benshi.py) so it always speaks the same dialect as the webui,
and CAPTURES everything it sees to disk so we can build the UI around what
the radio actually asks for and returns.

    python btdebug.py scan                 # list BLE devices + RSSI
    python btdebug.py gatt  <MAC>          # dump GATT services/characteristics
    python btdebug.py monitor <MAC>        # BLE: connect, live-decode, REPL
    python btdebug.py gaia  <COMx>         # classic RFCOMM/GAIA over an SPP
                                           # serial port (Windows path), REPL
    python btdebug.py catalog              # print the accumulated command
                                           # catalog (what we've seen so far)

On Windows the VR-N7600's classic GAIA channel is bonded as a "Standard Serial
over Bluetooth link" COM port -- use `gaia COM5` to talk to it without any
AF_BLUETOOTH socket.

At the monitor/gaia prompt you can inject frames:
    > get_dev_info          # send a Cmd by name (basic group, no payload)
    > 4                     # ...or by numeric command id
    > read_settings 00      # name + hex payload
    > raw 000a 000d 01      # group cmd [payload-hex]  (fully manual)
    > names                 # list known Cmd names
    > catalog               # dump what we've captured this session
    > quit

CAPTURE OUTPUT (auto, under captures/):
    <timestamp>.log     human-readable frame-by-frame trace
    <timestamp>.jsonl   one JSON record per frame
    catalog.jsonl       de-duplicated (group,cmd) catalog across all runs,
                        with an example payload + direction seen.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys
import time

from bleak import BleakClient, BleakScanner

import benshi as bp
from benshi import Cmd

T0 = time.time()
CAP_DIR = pathlib.Path(__file__).parent / "captures"
CATALOG_PATH = CAP_DIR / "catalog.jsonl"


def ts() -> str:
    return f"{time.time() - T0:8.3f}"


def cmd_name(cmd: int) -> tuple[str, bool]:
    reply = bool(cmd & bp.REPLY_BIT)
    base = cmd & ~bp.REPLY_BIT
    try:
        name = Cmd(base).name
    except ValueError:
        name = f"0x{base:04x}"
    return name, reply


def cmd_label(cmd: int) -> str:
    name, reply = cmd_name(cmd)
    return f"{name}{'+REPLY' if reply else ''}"


def group_label(group: int) -> str:
    return {bp.GROUP_BASIC: "BASIC", bp.GROUP_EXTENDED: "EXTENDED"}.get(
        group, f"0x{group:04x}")


class Capture:
    """Writes every frame to a human log + a JSONL, and maintains a
    de-duplicated command catalog of what has been seen."""

    def __init__(self, transport: str):
        CAP_DIR.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log = open(CAP_DIR / f"{stamp}.log", "a", encoding="utf-8")
        self.jsonl = open(CAP_DIR / f"{stamp}.jsonl", "a", encoding="utf-8")
        self.transport = transport
        self.seen: set[tuple[int, int, str]] = self._load_catalog_keys()
        header = (f"# capture start {dt.datetime.now().isoformat()} "
                  f"transport={transport}")
        self.log.write(header + "\n")
        self.log.flush()
        print(f"capturing -> {CAP_DIR}\\{stamp}.log / .jsonl")

    @staticmethod
    def _load_catalog_keys() -> set:
        keys = set()
        if CATALOG_PATH.exists():
            for line in CATALOG_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    keys.add((r["group"], r["cmd"], r["dir"]))
                except Exception:  # noqa: BLE001
                    pass
        return keys

    def record(self, direction: str, group: int, cmd: int, payload: bytes,
               raw: bytes):
        name, reply = cmd_name(cmd)
        base = cmd & ~bp.REPLY_BIT
        iso = dt.datetime.now().isoformat(timespec="milliseconds")
        status = payload[0] if (reply and payload) else None
        line = (f"{iso} {direction:2s} {self.transport:6s} "
                f"{group_label(group):8s} {cmd_label(cmd):26s} "
                f"payload={payload.hex(' ') or '-'}"
                + (f" status=0x{status:02x}" if status is not None else ""))
        self.log.write(line + "\n")
        self.log.flush()
        rec = {"t": iso, "dir": direction, "transport": self.transport,
               "group": group, "cmd": base, "cmd_name": name, "reply": reply,
               "payload_hex": payload.hex(), "raw_hex": raw.hex(),
               "status": status}
        self.jsonl.write(json.dumps(rec) + "\n")
        self.jsonl.flush()
        # catalog: unique (group, base-cmd, direction)
        key = (group, base, direction)
        if key not in self.seen:
            self.seen.add(key)
            cat = {"group": group, "group_name": group_label(group),
                   "cmd": base, "cmd_name": name, "dir": direction,
                   "reply": reply, "example_payload_hex": payload.hex(),
                   "first_seen": iso}
            with open(CATALOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(cat) + "\n")
            print(f"        [catalog+] {direction} {group_label(group)} "
                  f"{cmd_label(cmd)}")

    def close(self):
        self.log.write(f"# capture end {dt.datetime.now().isoformat()}\n")
        self.log.close()
        self.jsonl.close()


def decode_line(direction: str, data: bytes) -> str:
    hexs = data.hex(" ")
    try:
        group, cmd, payload = bp.parse_ble_frame(data)
    except Exception as e:  # noqa: BLE001
        return f"{ts()} {direction} [{len(data):3d}B] {hexs}   <unparsed: {e}>"
    line = (f"{ts()} {direction} [{len(data):3d}B] "
            f"{group_label(group):8s} {cmd_label(cmd):26s} "
            f"payload={payload.hex(' ') or '-'}")
    if payload and (cmd & bp.REPLY_BIT):
        line += f"  status=0x{payload[0]:02x}"
    return line


def print_frame(cap: Capture, direction: str, data: bytes):
    """Decode a BLE-form frame ([group][cmd][payload]), print + capture it."""
    print(decode_line(direction, data))
    try:
        group, cmd, payload = bp.parse_ble_frame(data)
    except Exception:  # noqa: BLE001
        return
    cap.record(direction, group, cmd, payload, data)


def parse_repl_line(line: str):
    """Return (group, cmd, payload) or None. Shared by monitor/gaia REPLs."""
    parts = line.split()
    if not parts:
        return None
    if parts[0] == "raw":
        if len(parts) < 3:
            print("usage: raw <group-hex> <cmd-hex> [payload-hex]")
            return None
        return int(parts[1], 16), int(parts[2], 16), \
            bytes.fromhex("".join(parts[3:]))
    tok = parts[0]
    try:
        cmd = int(tok, 0)
    except ValueError:
        try:
            cmd = Cmd[tok.upper()].value
        except KeyError:
            print(f"unknown command '{tok}' (try 'names')")
            return None
    payload = bytes.fromhex("".join(parts[1:])) if len(parts) > 1 else b""
    return bp.GROUP_BASIC, cmd, payload


def print_catalog():
    if not CATALOG_PATH.exists():
        print("no catalog yet — run a monitor/gaia session first.")
        return
    rows = [json.loads(x) for x in
            CATALOG_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows.sort(key=lambda r: (r["group"], r["cmd"], r["dir"]))
    print(f"{'DIR':3s} {'GROUP':8s} {'CMD':26s} EXAMPLE PAYLOAD")
    for r in rows:
        print(f"{r['dir']:3s} {r['group_name']:8s} {r['cmd_name']:26s} "
              f"{r['example_payload_hex'] or '-'}")
    print(f"\n{len(rows)} distinct (group,cmd,dir) entries — {CATALOG_PATH}")


async def repl(loop, send_coro_or_fn, is_async: bool):
    """Shared interactive loop. send_* takes (group, cmd, payload)."""
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if line in ("quit", "exit", "q"):
            break
        if line in ("help", "?"):
            print(__doc__)
            continue
        if line == "names":
            print(", ".join(c.name.lower() for c in Cmd))
            continue
        if line == "catalog":
            print_catalog()
            continue
        if not line:
            continue
        parsed = parse_repl_line(line)
        if not parsed:
            continue
        try:
            if is_async:
                await send_coro_or_fn(*parsed)
            else:
                send_coro_or_fn(*parsed)
        except Exception as e:  # noqa: BLE001
            print(f"{ts()} send failed: {e}")


async def do_scan(seconds: float = 8.0):
    print(f"scanning {seconds:.0f}s for BLE devices ...")
    devs = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for dev, adv in devs.values():
        rows.append((adv.rssi if adv else -999, dev.address, dev.name or "?"))
    for rssi, addr, name in sorted(rows, reverse=True):
        low = (name or "").lower()
        star = "  <- looks like a radio" if any(
            k in low for k in ("n7600", "benshi", "vr-", "vero")) else ""
        print(f"  {rssi:5d} dBm  {addr}  {name}{star}")
    if not rows:
        print("  (none found)")


async def do_gatt(mac: str):
    print(f"connecting to {mac} for GATT enumeration ...")
    async with BleakClient(mac, timeout=20.0) as client:
        print(f"connected={client.is_connected}\n")
        for svc in client.services:
            mark = "  *** BENSHI GATT SERVICE" \
                if svc.uuid.lower() == bp.GATT_SERVICE.lower() else ""
            print(f"service {svc.uuid}  {svc.description}{mark}")
            for ch in svc.characteristics:
                role = ""
                if ch.uuid.lower() == bp.GATT_WRITE.lower():
                    role = "  <- WRITE (commands out)"
                elif ch.uuid.lower() == bp.GATT_INDICATE.lower():
                    role = "  <- INDICATE (events in)"
                print(f"    char {ch.uuid}  [{','.join(ch.properties)}]{role}")
                for d in ch.descriptors:
                    print(f"        desc {d.uuid}")
        print()


async def do_monitor(mac: str):
    loop = asyncio.get_running_loop()
    cap = Capture("BLE")
    print(f"connecting to {mac} ...")

    def on_disconnect(_):
        print(f"{ts()} *** DISCONNECTED")

    client = BleakClient(mac, disconnected_callback=on_disconnect, timeout=20.0)
    await client.connect()
    print(f"{ts()} connected. subscribing to indicate char ...")

    def on_indicate(_char, data: bytearray):
        print_frame(cap, "RX", bytes(data))

    await client.start_notify(bp.GATT_INDICATE, on_indicate)
    print(f"{ts()} listening. type a command (or 'help', 'quit').\n")

    async def send(group: int, cmd: int, payload: bytes):
        frame = bp.ble_frame(group, cmd, payload)
        print_frame(cap, "TX", frame)
        await client.write_gatt_char(bp.GATT_WRITE, frame, response=True)

    try:
        await repl(loop, send, is_async=True)
    finally:
        await client.disconnect()
        cap.close()
        print("bye")


async def do_gaia_serial(port: str):
    """Classic RFCOMM / GAIA over a Windows SPP COM port (pyserial)."""
    import serial  # lazy: only needed for this mode

    loop = asyncio.get_running_loop()
    cap = Capture("RFCOMM")
    print(f"opening {port} (GAIA/RFCOMM SPP) ...")
    ser = serial.Serial(port, baudrate=115200, timeout=0.2)
    print(f"{ts()} open. streaming; type a command (or 'help', 'quit').\n")
    deframer = bp.GaiaDeframer()
    stop = False

    def reader():
        while not stop:
            try:
                data = ser.read(512)
            except Exception as e:  # noqa: BLE001
                print(f"{ts()} read error: {e}")
                return
            if not data:
                continue
            print(f"{ts()} rx-raw [{len(data):3d}B] {data.hex(' ')}")
            for group, cmd, payload in deframer.feed(data):
                print_frame(cap, "RX", bp.ble_frame(group, cmd, payload))

    reader_task = loop.run_in_executor(None, reader)

    def send_gaia(group: int, cmd: int, payload: bytes):
        wire = bp.gaia_frame(group, cmd, payload)
        print_frame(cap, "TX", bp.ble_frame(group, cmd, payload))
        print(f"        wire {wire.hex(' ')}")
        ser.write(wire)

    try:
        await repl(loop, send_gaia, is_async=False)
    finally:
        stop = True
        ser.close()
        await reader_task
        cap.close()
        print("bye")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "scan":
        asyncio.run(do_scan(float(arg) if arg else 8.0))
    elif cmd == "gatt" and arg:
        asyncio.run(do_gatt(arg))
    elif cmd == "monitor" and arg:
        asyncio.run(do_monitor(arg))
    elif cmd == "gaia" and arg:
        asyncio.run(do_gaia_serial(arg))
    elif cmd == "catalog":
        print_catalog()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

"""Minimal AX.25 UI-frame + APRS codec for the TNC data channel.

The radio's HT_SEND_DATA / DATA_RXD carry raw AX.25 UI frames (no KISS, no
FCS). This module builds outgoing frames (position beacons, messages, raw
status) and decodes incoming ones (uncompressed/compressed positions, Mic-E,
messages, status, objects) into plain dicts for the UI.
"""

from __future__ import annotations

import re
import time

APRS_DEST = "APDW17"      # generic experimental tocall


# ---------------------------------------------------------------- AX.25

def _encode_addr(call: str, last: bool = False, repeated: bool = False) -> bytes:
    call = call.upper().strip()
    ssid = 0
    if "-" in call:
        call, s = call.split("-", 1)
        try:
            ssid = int(s) & 0x0F
        except ValueError:
            ssid = 0
    call = (call + "      ")[:6]
    out = bytes((ord(c) << 1) & 0xFF for c in call)
    ssid_byte = 0x60 | (ssid << 1) | (1 if last else 0)
    if repeated:
        ssid_byte |= 0x80
    return out + bytes([ssid_byte])


def _decode_addr(data: bytes):
    call = "".join(chr(b >> 1) for b in data[:6]).strip()
    ssid = (data[6] >> 1) & 0x0F
    last = bool(data[6] & 0x01)
    repeated = bool(data[6] & 0x80)
    return (f"{call}-{ssid}" if ssid else call), last, repeated


def build_ui_frame(src: str, dest: str, path: list[str], info: bytes) -> bytes:
    addrs = _encode_addr(dest) + _encode_addr(src, last=not path)
    for i, digi in enumerate(path):
        addrs += _encode_addr(digi, last=(i == len(path) - 1))
    return addrs + b"\x03\xf0" + info


def parse_ax25(frame: bytes) -> dict | None:
    if len(frame) < 16:
        return None
    addrs, pos = [], 0
    while pos + 7 <= len(frame):
        call, last, repeated = _decode_addr(frame[pos:pos + 7])
        addrs.append((call, repeated))
        pos += 7
        if last:
            break
    if len(addrs) < 2 or pos + 2 > len(frame):
        return None
    ctrl, pid = frame[pos], frame[pos + 1]
    info = frame[pos + 2:]
    return {
        "dest": addrs[0][0],
        "source": addrs[1][0],
        "path": [c + ("*" if r else "") for c, r in addrs[2:]],
        "ctrl": ctrl, "pid": pid,
        "info": info.decode("latin1"),
    }


# ---------------------------------------------------------------- APRS out

def _aprs_lat(lat: float) -> str:
    ns = "N" if lat >= 0 else "S"
    lat = abs(lat)
    d = int(lat)
    m = (lat - d) * 60
    return f"{d:02d}{m:05.2f}{ns}"


def _aprs_lon(lon: float) -> str:
    ew = "E" if lon >= 0 else "W"
    lon = abs(lon)
    d = int(lon)
    m = (lon - d) * 60
    return f"{d:03d}{m:05.2f}{ew}"


def position_info(lat: float, lon: float, symbol: str = "/$",
                  comment: str = "") -> bytes:
    sym = (symbol + "/$")[:2]
    txt = f"={_aprs_lat(lat)}{sym[0]}{_aprs_lon(lon)}{sym[1]}{comment}"
    return txt.encode("latin1", "replace")


def message_info(addressee: str, text: str, msg_id: str | None = None) -> bytes:
    addr = (addressee.upper() + "         ")[:9]
    txt = f":{addr}:{text}"
    if msg_id:
        txt += "{" + str(msg_id)[:5]
    return txt.encode("latin1", "replace")


def ack_info(addressee: str, msg_id: str) -> bytes:
    addr = (addressee.upper() + "         ")[:9]
    return f":{addr}:ack{msg_id}".encode("latin1")


def status_info(text: str) -> bytes:
    return (">" + text).encode("latin1", "replace")


# ---------------------------------------------------------------- APRS in

def _parse_pos_uncompressed(t: str):
    m = re.match(r"(\d{2})(\d{2}\.\d+)([NS])(.)(\d{3})(\d{2}\.\d+)([EW])(.)(.*)",
                 t, re.S)
    if not m:
        return None
    lat = int(m.group(1)) + float(m.group(2)) / 60
    if m.group(3) == "S":
        lat = -lat
    lon = int(m.group(5)) + float(m.group(6)) / 60
    if m.group(7) == "W":
        lon = -lon
    return {"latitude": round(lat, 6), "longitude": round(lon, 6),
            "symbol": m.group(4) + m.group(8), "comment": m.group(9).strip()}


def _parse_pos_compressed(t: str):
    if len(t) < 13:
        return None
    try:
        sym_table, comp, sym = t[0], t[1:9], t[9]
        y = 0
        x = 0
        for c in comp[:4]:
            y = y * 91 + (ord(c) - 33)
        for c in comp[4:8]:
            x = x * 91 + (ord(c) - 33)
        lat = 90 - y / 380926.0
        lon = -180 + x / 190463.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"latitude": round(lat, 6), "longitude": round(lon, 6),
                "symbol": sym_table + sym, "comment": t[13:].strip()}
    except (ValueError, IndexError):
        return None


def _parse_mic_e(dest: str, info: str):
    """Decode Mic-E (info starts with ` or ')."""
    dcall = dest.split("-")[0]
    if len(dcall) < 6 or len(info) < 9:
        return None
    lat_digits, msg_bits, ns, lon_off, we = [], 0, "S", 0, "E"
    table = {
        **{c: (str(i), 0, "S", 0, "E") for i, c in enumerate("0123456789")},
        **{c: (str(i), 1, "N", 100, "W") for i, c in
           enumerate("PQRSTUVWXY")},
        **{c: (str(i), 1, "N", 100, "W") for i, c in
           enumerate("ABCDEFGHIJ")},
        "K": (" ", 1, "N", 100, "W"), "L": (" ", 0, "S", 0, "E"),
        "Z": (" ", 1, "N", 100, "W"),
    }
    try:
        for i, c in enumerate(dcall[:6]):
            digit, bit, ns_c, off, we_c = table[c]
            lat_digits.append(digit)
            if i == 3:
                ns = ns_c
            if i == 4:
                lon_off = off
            if i == 5:
                we = we_c
        lat_s = "".join(lat_digits)
        lat = int(lat_s[:2]) + float(lat_s[2:4] + "." + lat_s[4:6].replace(" ", "0")) / 60
        if ns == "S":
            lat = -lat
        d28, m28, h28 = ord(info[1]) - 28, ord(info[2]) - 28, ord(info[3]) - 28
        lon_d = d28 + lon_off
        if 180 <= lon_d <= 189:
            lon_d -= 80
        elif 190 <= lon_d <= 199:
            lon_d -= 190
        if m28 >= 60:
            m28 -= 60
        lon = lon_d + (m28 + h28 / 100.0) / 60
        if we == "W":
            lon = -lon
        sym = info[8] + info[7]      # table + code
        comment = info[9:].strip()
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"latitude": round(lat, 6), "longitude": round(lon, 6),
                "symbol": sym, "comment": comment, "mic_e": True}
    except (KeyError, ValueError, IndexError):
        return None


def decode_aprs(frame: dict) -> dict:
    """Classify an AX.25 UI frame's info field into an APRS report."""
    info = frame.get("info", "")
    out = {"type": "other", "raw": info, "source": frame.get("source"),
           "dest": frame.get("dest"), "path": frame.get("path", []),
           "time": int(time.time())}
    if not info:
        return out
    dt = info[0]
    body = info[1:]
    if dt == ":":
        m = re.match(r"([A-Za-z0-9 \-]{9}):(.*)", body, re.S)
        if m:
            text = m.group(2)
            msg_id = None
            idm = re.search(r"\{([A-Za-z0-9]{1,5})$", text)
            if idm:
                msg_id = idm.group(1)
                text = text[:idm.start()]
            out.update(type="message", addressee=m.group(1).strip(),
                       text=text, msg_id=msg_id)
            if re.fullmatch(r"ack[A-Za-z0-9]{1,5}", text):
                out["type"] = "ack"
            return out
    if dt in "!=/@":
        t = body
        if dt in "/@" and len(t) > 7:
            t = t[7:]                      # skip timestamp
        pos = (_parse_pos_uncompressed(t) if t[:1].isdigit()
               else _parse_pos_compressed(t))
        if pos:
            out.update(type="position", **pos)
            return out
    if dt in "`'":
        pos = _parse_mic_e(frame.get("dest", ""), info)
        if pos:
            out.update(type="position", **pos)
            return out
    if dt == ">":
        out.update(type="status", text=body)
        return out
    if dt == ";" and len(body) > 10:
        name = body[:9].strip()
        rest = body[10:]
        if len(rest) > 7:
            pos = _parse_pos_uncompressed(rest[7:])
            if pos:
                out.update(type="object", name=name, **pos)
                return out
    return out

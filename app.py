"""VR-N7600 web control — FastAPI bridge between browser and Bluetooth radio.

Run:  python3 app.py   (serves on 0.0.0.0:8099)

Config in config.json:
  { "mac": "AA:BB:CC:DD:EE:FF", "transport": "auto",
    "rfcomm_channel": null, "port": 8099, "auto_connect": true }
"""

import asyncio
import json
import logging
import pathlib
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import benshi as bp
from radio import AprsRadio, scan_devices

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

import sys
if getattr(sys, "frozen", False):
    # packaged (PyInstaller): static files are unpacked to _MEIPASS (read-only);
    # config lives next to the executable so it stays writable across runs.
    BASE = pathlib.Path(sys._MEIPASS)
    DATA_DIR = pathlib.Path(sys.executable).parent
else:
    BASE = pathlib.Path(__file__).parent
    DATA_DIR = BASE
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "mac": "",
    "transport": "ble",        # auto | ble | rfcomm — user picks and presses Connect
    "rfcomm_channel": None,
    "port": 8099,
    "auto_connect": False,     # off by default; opt-in via the Connect card
    "status_poll_seconds": 15,  # doubles as keep-alive; keep < radio idle timeout
    "rf_poll_seconds": 2,      # fast HT-status poll for the live S-meter
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (OSError, ValueError) as e:
            log.error("bad config.json: %s", e)
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


config = load_config()
radio = AprsRadio()
ws_clients: set[WebSocket] = set()


async def broadcast(kind: str, data):
    msg = json.dumps({"kind": kind, "data": data})
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


radio.event_cb = broadcast


async def connect_radio() -> None:
    if not config.get("mac"):
        raise HTTPException(400, "no radio MAC configured — use scan first")
    await radio.connect(config["mac"], config.get("transport", "auto"),
                        config.get("rfcomm_channel"))


async def keeper():
    """Keep the ACTIVE link solid: while connected, poll status regularly —
    this doubles as a keep-alive so the radio doesn't idle the link out.
    No reconnect loops: connecting is always the user's explicit action
    (the one exception: the opt-in "auto-connect on startup" checkbox gets a
    single attempt at startup, nothing after)."""
    startup_attempted = False
    last_poll = 0.0
    last_rf = 0.0
    while True:
        try:
            if (not startup_attempted and config.get("auto_connect")
                    and config.get("mac") and not radio.connected):
                startup_attempted = True
                log.info("startup connect (opt-in) to %s ...", config["mac"])
                try:
                    await connect_radio()
                    await broadcast("snapshot", radio.snapshot())
                except Exception as e:
                    log.info("startup connect failed: %s", e)
            elif radio.connected:
                loop_t = asyncio.get_running_loop().time()
                if loop_t - last_poll > config.get("status_poll_seconds", 30):
                    last_poll = loop_t
                    await radio.refresh_status()
                    await broadcast("poll", {
                        "ht_status": bp.struct_dict(radio.ht_status)
                        if radio.ht_status else None,
                        "volume": radio.volume,
                        "battery_pct": radio.battery_pct,
                        "battery_voltage": radio.battery_voltage,
                    })
                elif (ws_clients and
                      loop_t - last_rf > config.get("rf_poll_seconds", 2)):
                    # light-weight fast poll: only when a browser is watching.
                    # GET_HT_STATUS carries the radio's own RSSI (S-meter);
                    # READ_RF_STATUS raw rides along as RE material.
                    last_rf = loop_t
                    await radio.refresh_ht_status()
                    raw = await radio.read_rf_status_raw()
                    await broadcast("rf", {
                        "ht_status": bp.struct_dict(radio.ht_status)
                        if radio.ht_status else None,
                        "rf_raw": raw.hex(" ") if raw else None,
                    })
        except Exception as e:
            log.warning("keeper: %s", e)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(keeper())
    yield
    task.cancel()
    await radio.disconnect()


app = FastAPI(title="VR-N7600 Remote", lifespan=lifespan)


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/state")
async def get_state():
    return radio.snapshot()


@app.get("/api/scan")
async def api_scan():
    return await scan_devices()


class ConnectBody(BaseModel):
    mac: str | None = None
    transport: str | None = None
    rfcomm_channel: int | None = None


@app.post("/api/connect")
async def api_connect(body: ConnectBody):
    if body.mac:
        config["mac"] = body.mac.upper()
    if body.transport:
        config["transport"] = body.transport
    if body.rfcomm_channel is not None:
        config["rfcomm_channel"] = body.rfcomm_channel
    save_config(config)
    try:
        await connect_radio()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"connect failed: {e}")
    snap = radio.snapshot()
    await broadcast("snapshot", snap)
    return snap


@app.post("/api/disconnect")
async def api_disconnect():
    config["auto_connect"] = False
    save_config(config)
    await radio.disconnect()
    await broadcast("connection", {"connected": False})
    return {"connected": False}


@app.get("/api/config")
async def api_get_config():
    """Connection config the UI needs to pre-fill the picker."""
    return {"mac": config.get("mac", ""),
            "transport": config.get("transport", "ble"),
            "auto_connect": bool(config.get("auto_connect", False)),
            "port": config.get("port", 8099)}


@app.post("/api/auto_connect/{on}")
async def api_auto_connect(on: int):
    config["auto_connect"] = bool(on)
    save_config(config)
    return {"auto_connect": config["auto_connect"]}


def require_connected():
    if not radio.connected:
        raise HTTPException(409, "radio not connected")


class DebugCmdBody(BaseModel):
    cmd: int                      # command id (BASIC group)
    payload_hex: str = ""
    timeout: float = 3.0


@app.post("/api/debug/command")
async def api_debug_command(body: DebugCmdBody):
    """Send a raw command over the ALREADY-OPEN link and return the raw reply.
    Lets us probe/RE the protocol without opening a second (colliding) BT
    connection to the radio."""
    require_connected()
    try:
        payload = bytes.fromhex(body.payload_hex.replace(" ", ""))
    except ValueError:
        raise HTTPException(400, "payload_hex is not valid hex")
    try:
        reply = await radio.raw_command(body.cmd, payload, body.timeout)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    try:
        name = bp.Cmd(body.cmd).name
    except ValueError:
        name = f"0x{body.cmd:04x}"
    return {"cmd": body.cmd, "cmd_name": name,
            "payload_sent": payload.hex(" "),
            "reply_hex": reply.hex(" "),
            "reply_len": len(reply),
            "status": reply[0] if reply else None}


@app.get("/api/pf")
async def api_get_pf(refresh: int = 0):
    """Programmable-button map: 4 buttons x 4 gestures -> effect codes."""
    require_connected()
    if refresh or radio.pf is None:
        await radio.get_pf()
    try:
        valid = await radio.get_pf_actions()
    except Exception:
        valid = sorted(bp.PF_EFFECT_NAMES)
    return {"pf": radio.pf,
            "valid_effects": [{"code": c, "name": bp.pf_effect_name(c)}
                              for c in valid]}


class PfBody(BaseModel):
    # {key(int as str or int): effect} overrides, e.g. {"6": 21}
    effects: dict[int, int]


@app.post("/api/pf")
async def api_set_pf(body: PfBody):
    require_connected()
    try:
        pf = await radio.set_pf(body.effects)
    except bp.CommandFailed as e:
        raise HTTPException(502, str(e))
    await broadcast("pf", pf)
    return {"pf": pf}


@app.get("/api/fm")
async def api_fm_status():
    require_connected()
    try:
        return bp.struct_dict(await radio.fm_status())
    except bp.CommandFailed as e:
        raise HTTPException(502, str(e))


@app.post("/api/fm/power/{on}")
async def api_fm_power(on: int):
    require_connected()
    await radio.fm_set_power(bool(on))
    return {"ok": True}


@app.post("/api/fm/seek/{direction}")
async def api_fm_seek(direction: str):
    require_connected()
    await radio.fm_seek(direction == "up")
    return {"ok": True}


@app.post("/api/fm/freq/{khz}")
async def api_fm_freq(khz: int):
    require_connected()
    await radio.fm_set_freq(khz * 1000)
    return {"ok": True}


@app.get("/api/regions")
async def api_regions(refresh: int = 0):
    require_connected()
    if refresh or not radio.region_names:
        await radio.read_region_names()
    cur = radio.ht_status.curr_region if radio.ht_status else -1
    return {"names": radio.region_names, "current": cur}


@app.post("/api/region/{region}")
async def api_set_region(region: int):
    require_connected()
    await radio.set_region(region)
    return {"ok": True}


@app.get("/api/channels")
async def api_channels(refresh: int = 0):
    require_connected()
    if refresh or not radio.channels:
        await radio.read_all_channels()
    return [bp.struct_dict(c) for _, c in sorted(radio.channels.items())]


class ChannelBody(BaseModel):
    name: str | None = None
    rx_freq: int | None = None
    tx_freq: int | None = None
    rx_mod: int | None = None
    tx_mod: int | None = None
    rx_sub_audio: int | None = None
    tx_sub_audio: int | None = None
    bandwidth_wide: bool | None = None
    scan: bool | None = None
    tx_disable: bool | None = None
    tx_at_max_power: bool | None = None
    tx_at_med_power: bool | None = None
    mute: bool | None = None
    bclo: bool | None = None
    talk_around: bool | None = None
    rev: bool | None = None
    pre_de_emph_bypass: bool | None = None
    sign: bool | None = None


@app.post("/api/channel/{ch_id}")
async def api_write_channel(ch_id: int, body: ChannelBody):
    require_connected()
    ch = radio.channels.get(ch_id) or await radio.read_channel(ch_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(ch, k, v)
    ch.channel_id = ch_id
    try:
        await radio.write_channel(ch)
    except bp.CommandFailed as e:
        raise HTTPException(502, str(e))
    await broadcast("channel", bp.struct_dict(ch))
    return bp.struct_dict(ch)


class SelectBody(BaseModel):
    vfo: str = "a"
    channel: int


@app.post("/api/select")
async def api_select(body: SelectBody):
    require_connected()
    await radio.set_channel(body.vfo, body.channel)
    return {"ok": True}


class SettingsBody(BaseModel):
    updates: dict
    store: bool = False


@app.get("/api/settings")
async def api_get_settings(refresh: int = 1):
    """Read the current settings block from the radio (fresh GET)."""
    require_connected()
    if refresh or radio.settings is None:
        try:
            await radio.read_settings()
        except bp.CommandFailed as e:
            raise HTTPException(502, str(e))
    fields = radio.settings.fields if radio.settings else {}
    await broadcast("settings", fields)
    return fields


@app.post("/api/settings")
async def api_settings(body: SettingsBody):
    require_connected()
    allowed = {n for n, _ in bp.SETTINGS_FIELDS} | {
        "channel_a", "channel_b", "auto_share_loc_ch"}
    bad = set(body.updates) - allowed
    if bad:
        raise HTTPException(400, f"unknown settings: {sorted(bad)}")
    await radio.write_settings(
        {k: int(v) for k, v in body.updates.items()}, body.store)
    return radio.settings.fields


@app.post("/api/volume/{level}")
async def api_volume(level: int):
    require_connected()
    await radio.set_volume(level)
    return {"volume": radio.volume}


@app.post("/api/power/{on}")
async def api_power(on: int):
    require_connected()
    await radio.set_power(bool(on))
    return {"ok": True}


@app.get("/api/position")
async def api_position():
    require_connected()
    try:
        pos = await radio.get_position()
    except bp.CommandFailed as e:
        raise HTTPException(502, str(e))
    return bp.struct_dict(pos)


@app.post("/api/refresh")
async def api_refresh():
    require_connected()
    await radio.refresh_status()
    snap = radio.snapshot()
    await broadcast("snapshot", snap)
    return snap


@app.get("/api/bss")
async def api_get_bss(refresh: int = 0):
    require_connected()
    if refresh or radio.bss is None:
        await radio.read_bss()
    return radio.bss.fields if radio.bss else {}


class BssBody(BaseModel):
    updates: dict


@app.post("/api/bss")
async def api_set_bss(body: BssBody):
    require_connected()
    try:
        await radio.write_bss(body.updates)
    except bp.CommandFailed as e:
        raise HTTPException(502, str(e))
    return radio.bss.fields


@app.get("/api/aprs/path")
async def api_get_path(refresh: int = 0):
    require_connected()
    if refresh:
        await radio.read_aprs_path()
    return {"path": radio.aprs_path}


class PathBody(BaseModel):
    path: str


@app.post("/api/aprs/path")
async def api_set_path(body: PathBody):
    require_connected()
    await radio.set_aprs_path(body.path)
    return {"path": radio.aprs_path}


class AprsMessageBody(BaseModel):
    to: str
    text: str
    channel_id: int | None = None


@app.post("/api/aprs/message")
async def api_aprs_message(body: AprsMessageBody):
    require_connected()
    try:
        return await radio.send_aprs_message(body.to, body.text, body.channel_id)
    except (bp.ProtocolError, bp.CommandFailed) as e:
        raise HTTPException(502, str(e))


class AprsBeaconBody(BaseModel):
    lat: float
    lon: float
    comment: str = ""
    channel_id: int | None = None


@app.post("/api/aprs/beacon")
async def api_aprs_beacon(body: AprsBeaconBody):
    require_connected()
    try:
        return await radio.send_aprs_beacon(body.lat, body.lon, body.comment,
                                            body.channel_id)
    except (bp.ProtocolError, bp.CommandFailed) as e:
        raise HTTPException(502, str(e))


class AprsStatusBody(BaseModel):
    text: str
    channel_id: int | None = None


@app.post("/api/aprs/status")
async def api_aprs_status(body: AprsStatusBody):
    require_connected()
    try:
        return await radio.send_aprs_status(body.text, body.channel_id)
    except (bp.ProtocolError, bp.CommandFailed) as e:
        raise HTTPException(502, str(e))


@app.get("/api/aprs/log")
async def api_aprs_log():
    return radio.aprs_log[-300:]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"kind": "snapshot",
                                       "data": radio.snapshot()}))
        while True:
            await ws.receive_text()   # keepalive pings from the browser
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(config.get("port", 8099)))

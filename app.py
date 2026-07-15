"""VR-N7600 web control — FastAPI bridge between browser and Bluetooth radio.

Run:  python3 app.py   (serves on 0.0.0.0:8084)

Config in config.json:
  { "mac": "AA:BB:CC:DD:EE:FF", "transport": "auto",
    "rfcomm_channel": null, "port": 8084, "auto_connect": true }
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

BASE = pathlib.Path(__file__).parent
CONFIG_PATH = BASE / "config.json"

DEFAULT_CONFIG = {
    "mac": "",
    "transport": "auto",       # auto | ble | rfcomm
    "rfcomm_channel": None,
    "port": 8084,
    "auto_connect": True,
    "status_poll_seconds": 30,
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
    """Auto-connect / reconnect loop and periodic status poll."""
    last_poll = 0.0
    while True:
        try:
            if config.get("auto_connect") and config.get("mac") \
                    and not radio.connected:
                log.info("auto-connecting to %s ...", config["mac"])
                try:
                    await connect_radio()
                    await broadcast("snapshot", radio.snapshot())
                except Exception as e:
                    log.info("auto-connect failed: %s", e)
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
        except Exception as e:
            log.warning("keeper: %s", e)
        await asyncio.sleep(15)


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


@app.post("/api/auto_connect/{on}")
async def api_auto_connect(on: int):
    config["auto_connect"] = bool(on)
    save_config(config)
    return {"auto_connect": config["auto_connect"]}


def require_connected():
    if not radio.connected:
        raise HTTPException(409, "radio not connected")


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
    uvicorn.run(app, host="0.0.0.0", port=int(config.get("port", 8084)))

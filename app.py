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
import re
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio
import benshi as bp
import prop
from radio import AprsRadio, list_com_ports, scan_devices

import os
import sys
if getattr(sys, "frozen", False):
    # packaged (PyInstaller): static files are unpacked to _MEIPASS (read-only);
    # config lives next to the executable so it stays writable across runs.
    BASE = pathlib.Path(sys._MEIPASS)
    DATA_DIR = pathlib.Path(sys.executable).parent
else:
    BASE = pathlib.Path(__file__).parent
    DATA_DIR = BASE
if os.environ.get("VRN7600_DATA"):
    # explicit data dir (Docker: mount a volume here for config + history)
    DATA_DIR = pathlib.Path(os.environ["VRN7600_DATA"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"

_LOG_FMT = "%(asctime)s %(name)s %(levelname)s %(message)s"
if getattr(sys, "frozen", False):
    # windowed exe: no console (sys.stderr is None) — log to a rotating file
    # next to the exe instead, or every message and crash would vanish.
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(DATA_DIR / "vrn7600.log", maxBytes=1_000_000,
                              backupCount=2, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(_LOG_FMT))
    logging.basicConfig(level=logging.INFO, handlers=[_fh])
else:
    logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
log = logging.getLogger("app")

DEFAULT_CONFIG = {
    "mac": "",
    "transport": "ble",        # auto | ble | rfcomm | serial — user picks and presses Connect
    "rfcomm_channel": None,
    "com_port": None,          # bonded SPP port for transport "serial", e.g. "COM5"
    "port": 8099,
    "auto_connect": False,     # off by default; opt-in via the Connect card
    "status_poll_seconds": 15,  # doubles as keep-alive; keep < radio idle timeout
    "rf_poll_seconds": 2,      # fast HT-status poll for the live S-meter
    "access_token": "",        # set non-empty to require a token on /api + /ws
                               # (do this before port-forwarding for remote use)
    "station_lat": None,       # home QTH for propagation analytics (distance/
    "station_lon": None,       # bearing); auto-updated by each sent beacon
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


# PropView-style propagation analytics: every decoded APRS report also goes
# into a persistent rolling history (the radio is our TNC).
prop_store = prop.PropStore(DATA_DIR / "aprs_history.jsonl")


async def on_radio_event(kind: str, data):
    if kind == "aprs":
        try:
            prop_store.add(data)
        except Exception as e:                    # noqa: BLE001
            log.warning("prop store: %s", e)
    await broadcast(kind, data)


radio.event_cb = on_radio_event

# ---- voice bridge (AOC RFCOMM SBC <-> browser) --------------------------
bridge = audio.AudioBridge(ffmpeg=config.get("ffmpeg_path"))
audio_clients: dict[WebSocket, dict] = {}     # ws -> {"codec","cid"}
opus_rx: audio.OpusRxStreamer | None = None
mic_decoder: audio.WebmMicDecoder | None = None
recorder = audio.RxRecorder(DATA_DIR)
ptt_owner: str | None = None                  # client id currently keying


def _sync_bridge_addr() -> None:
    """The AOC channel is opened by BT address; keep it current with config."""
    mac = config.get("mac") or ""
    try:
        bridge.address = int(mac.replace(":", ""), 16)
    except ValueError:
        bridge.address = 0


_sync_bridge_addr()


async def _on_rx_pcm(pcm: bytes) -> None:
    """RX tee: recorder + raw-PCM clients + the shared opus encoder."""
    recorder.feed(pcm)
    if opus_rx and opus_rx.active:
        opus_rx.feed(pcm)
    dead = []
    for ws, meta in audio_clients.items():
        if meta["codec"] != "pcm":
            continue
        try:
            await ws.send_bytes(pcm)
        except Exception:
            dead.append(ws)
    for ws in dead:
        audio_clients.pop(ws, None)


async def _broadcast_opus(pkt: bytes) -> None:
    """One raw opus packet (20 ms @ 48 kHz) to every opus-capable client."""
    dead = []
    for ws, meta in audio_clients.items():
        if meta["codec"] != "opus":
            continue
        try:
            await ws.send_bytes(pkt)
        except Exception:
            dead.append(ws)
    for ws in dead:
        audio_clients.pop(ws, None)


async def _ensure_opus_rx() -> None:
    global opus_rx
    if any(m["codec"] == "opus" for m in audio_clients.values()):
        if opus_rx is None and bridge.ffmpeg:
            opus_rx = audio.OpusRxStreamer(bridge.ffmpeg)
            opus_rx.packet_cb = _broadcast_opus
        if opus_rx and not opus_rx.active:
            await opus_rx.start()
    elif opus_rx and opus_rx.active:
        await opus_rx.stop()


bridge.rx_cb = _on_rx_pcm


def token_ok(supplied: str | None) -> bool:
    want = config.get("access_token") or ""
    return not want or supplied == want


async def connect_radio() -> None:
    if not config.get("mac"):
        raise HTTPException(400, "no radio MAC configured — use scan first")
    await radio.connect(config["mac"], config.get("transport", "auto"),
                        config.get("rfcomm_channel"),
                        config.get("com_port"))
    # The radio's BT SoC stays reachable in soft-off and classic connects
    # succeed against the bond (live-verified) — so power it on when the
    # operator connects, like HTCommander does. Opt out: wake_on_connect=false.
    if (config.get("wake_on_connect", True) and radio.ht_status
            and not radio.ht_status.is_power_on):
        try:
            await radio.set_power(True)
            await radio.refresh_ht_status()
            log.info("radio was soft-off — powered it on (wake_on_connect)")
        except Exception as e:                    # noqa: BLE001
            log.warning("wake_on_connect: %s", e)


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
        recorder.maybe_close()        # finalize an over once the gap passes
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(keeper())
    yield
    task.cancel()
    await bridge.close()
    await radio.disconnect()


app = FastAPI(title="VR-N7600 Remote", lifespan=lifespan)


@app.middleware("http")
async def auth_gate(request, call_next):
    """Single-token gate for remote exposure. When access_token is set, every
    /api/* call must present it (X-Auth-Token header or ?token=). The page and
    static assets stay open so the browser can load and prompt for the token;
    all state-changing surface is behind /api and the websockets."""
    if config.get("access_token") and request.url.path.startswith("/api/"):
        supplied = (request.headers.get("X-Auth-Token")
                    or request.query_params.get("token"))
        if not token_ok(supplied):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/sw.js")
async def service_worker():
    """Served from the root so the service worker's scope covers '/'."""
    return FileResponse(BASE / "static" / "sw.js",
                        media_type="application/javascript")


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
    com_port: str | None = None


@app.post("/api/connect")
async def api_connect(body: ConnectBody):
    if body.mac:
        config["mac"] = body.mac.upper()
    if body.transport:
        config["transport"] = body.transport
    if body.rfcomm_channel is not None:
        config["rfcomm_channel"] = body.rfcomm_channel
    if body.com_port is not None:
        config["com_port"] = body.com_port or None
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
            "com_port": config.get("com_port") or "",
            "auto_connect": bool(config.get("auto_connect", False)),
            "port": config.get("port", 8099)}


@app.get("/api/comports")
async def api_comports():
    """Serial ports on the bridge machine (the radio's bonded SPP link is
    one of these on Windows) — for the Connect card's COM picker."""
    return list_com_ports()


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


@app.get("/api/debug/htstatus")
async def api_debug_htstatus():
    """Raw GET_HT_STATUS payload + parse, for status-bit RE work."""
    require_connected()
    raw = await radio._command(bp.Cmd.GET_HT_STATUS)
    st = bp.HTStatus.parse(raw, 1)
    rf = await radio.read_rf_status_raw()
    return {"raw": raw.hex(" "), "parsed": bp.struct_dict(st),
            "rf_raw": rf.hex(" ") if rf else None}


@app.post("/api/progfunc/{effect}")
async def api_progfunc(effect: int, state: int = -1):
    """Remote button press (DO_PROG_FUNC). state -1 = full press+release
    cycle, else a single 1/0 edge."""
    require_connected()
    try:
        if state < 0:
            await radio.do_prog_func(effect, 1)
            reply = await radio.do_prog_func(effect, 0)
        else:
            reply = await radio.do_prog_func(effect, state)
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(502, f"progfunc: {e}")
    return {"effect": effect, "reply_hex": reply.hex(" ") if reply else None}


@app.get("/api/debug/events")
async def api_debug_events(n: int = 40):
    """Raw event payloads captured off the wire (newest last)."""
    return [{"t": t, "ev": ev, "hex": hx} for t, ev, hx in radio.event_log[-n:]]


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
        report = await radio.send_aprs_beacon(body.lat, body.lon, body.comment,
                                              body.channel_id)
    except (bp.ProtocolError, bp.CommandFailed) as e:
        raise HTTPException(502, str(e))
    # the operator's own beacon is the natural home fix for prop analytics
    config["station_lat"], config["station_lon"] = body.lat, body.lon
    save_config(config)
    return report


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


@app.get("/api/aprs/prop")
async def api_aprs_prop():
    """PropView-style propagation analytics over the rolling packet history:
    hourly heatmap, direct/digi meters, DX leaderboard. No radio needed —
    works off stored history even while disconnected."""
    return prop_store.analytics(config.get("station_lat"),
                                config.get("station_lon"))


class StationBody(BaseModel):
    lat: float
    lon: float


@app.get("/api/aprs/station")
async def api_get_station():
    return {"lat": config.get("station_lat"), "lon": config.get("station_lon")}


@app.post("/api/aprs/station")
async def api_set_station(body: StationBody):
    """Home QTH for distance/bearing analytics (also set by every beacon)."""
    config["station_lat"], config["station_lon"] = body.lat, body.lon
    save_config(config)
    return {"lat": body.lat, "lon": body.lon}


@app.get("/api/auth/check")
async def api_auth_check():
    """200 if the presented token is accepted (or no token is required).
    The middleware already rejected bad tokens, so reaching here means OK."""
    return {"ok": True, "auth_required": bool(config.get("access_token"))}


# ---------------------------------------------------------- voice / audio
@app.get("/api/audio/devices")
async def api_audio_devices():
    """Legacy name — audio now rides the AOC RFCOMM channel, not OS devices."""
    return {"devices": [], "status": bridge.status()}


@app.get("/api/audio/status")
async def api_audio_status():
    st = bridge.status()
    st["aoc_connected"] = bool(radio.ht_status and
                               getattr(radio.ht_status, "is_aoc_connected", False))
    st["opus_available"] = bool(bridge.ffmpeg)
    st["listeners"] = {"opus": sum(1 for m in audio_clients.values()
                                   if m["codec"] == "opus"),
                       "pcm": sum(1 for m in audio_clients.values()
                                  if m["codec"] == "pcm")}
    st["ptt_owner"] = ptt_owner
    return st


@app.post("/api/audio/relay/{on}")
async def api_audio_relay(on: int):
    require_connected()
    await radio.ensure_audio_relay(bool(on))
    return {"ok": True}


@app.post("/api/ptt/{on}")
async def api_ptt(on: int, cid: str = ""):
    """Key (1) / unkey (0) transmit. AOC PTT is implicit: the radio keys
    itself when SBC audio frames start arriving and unkeys on the end frame,
    so key = start the encoder pipeline, unkey = flush + end frame.

    Multi-operator arbitration: first ?cid= to key owns the transmitter;
    a different cid gets 409 until release. The owner's audio-WS dropping
    force-unkeys (dead-man, see ws_audio)."""
    global ptt_owner, mic_decoder
    keyed = bool(on)
    cid = cid[:40]
    if keyed:
        if bridge.tx_active and ptt_owner and cid != ptt_owner:
            raise HTTPException(409, "transmitter keyed by another operator")
        try:
            _sync_bridge_addr()
            await bridge.start_tx()
        except Exception as e:                    # noqa: BLE001
            raise HTTPException(502, f"ptt: {e}")
        ptt_owner = cid or "unknown"
    else:
        if bridge.tx_active and ptt_owner and cid and cid != ptt_owner:
            raise HTTPException(409, "transmitter keyed by another operator")
        try:
            if mic_decoder:                       # flush the webm tail first
                await mic_decoder.stop()
                mic_decoder = None
            await bridge.stop_tx()
        except Exception as e:                    # noqa: BLE001
            raise HTTPException(502, f"ptt: {e}")
        ptt_owner = None
    await broadcast("ptt", {"tx": keyed, "owner": ptt_owner})
    return {"tx": keyed, "owner": ptt_owner}


async def _force_unkey(reason: str) -> None:
    """Dead-man release: owner vanished (WS drop) while keyed."""
    global ptt_owner, mic_decoder
    log.warning("force unkey: %s", reason)
    try:
        if mic_decoder:
            await mic_decoder.stop()
            mic_decoder = None
        await bridge.stop_tx()
    except Exception as e:                        # noqa: BLE001
        log.warning("force unkey: %s", e)
    ptt_owner = None
    await broadcast("ptt", {"tx": False, "owner": None})


@app.get("/api/recordings")
async def api_recordings():
    """Newest-first list of recorded RX overs (one WAV per over)."""
    return recorder.list()


@app.get("/api/recordings/{name}")
async def api_recording(name: str):
    if not re.fullmatch(r"rx-\d{8}-\d{6}\.wav", name):
        raise HTTPException(400, "bad name")
    path = recorder.dir / name
    if not path.exists():
        raise HTTPException(404, "gone")
    return FileResponse(path, media_type="audio/wav")


@app.delete("/api/recordings/{name}")
async def api_recording_delete(name: str):
    if not re.fullmatch(r"rx-\d{8}-\d{6}\.wav", name):
        raise HTTPException(400, "bad name")
    try:
        (recorder.dir / name).unlink()
    except OSError:
        raise HTTPException(404, "gone")
    return {"ok": True}


class PhoneStatusBody(BaseModel):
    state: int


@app.post("/api/phone_status")
async def api_phone_status(body: PhoneStatusBody):
    """Experimental HFP call-state control (SET_PHONE_STATUS 51)."""
    require_connected()
    try:
        reply = await radio.set_phone_status(body.state)
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(502, f"phone_status: {e}")
    return {"state": body.state, "reply_hex": reply.hex(" ") if reply else None}


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    """Bidirectional radio audio. Codec negotiated by ?codec=:
      pcm  (default): both ways raw int16 mono at audio.RADIO_RATE (32 kHz).
      opus: server->client raw opus packets (20 ms, 48 kHz mono, ~24 kbit/s);
            client->server MediaRecorder webm/opus chunks while keyed.
    ?cid= ties the connection to the PTT owner for dead-man release."""
    global mic_decoder
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=1008)
        return
    codec = ("opus" if ws.query_params.get("codec") == "opus"
             and bridge.ffmpeg else "pcm")
    cid = (ws.query_params.get("cid") or "")[:40]
    await ws.accept()
    audio_clients[ws] = {"codec": codec, "cid": cid}
    try:
        await _ensure_opus_rx()
        if not bridge.rx_active:
            try:
                _sync_bridge_addr()
                await bridge.start_rx()
            except Exception as e:                # noqa: BLE001
                await ws.send_text(json.dumps({"error": str(e)}))
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:      # operator mic -> radio
                if codec == "opus":
                    if mic_decoder is None and bridge.tx_active:
                        mic_decoder = audio.WebmMicDecoder(
                            bridge.ffmpeg, bridge.feed_tx)
                        await mic_decoder.start()
                    if mic_decoder:
                        mic_decoder.feed(msg["bytes"])
                else:
                    bridge.feed_tx(msg["bytes"])
            elif msg.get("text") == "ping":
                continue
    except WebSocketDisconnect:
        pass
    finally:
        audio_clients.pop(ws, None)
        if cid and ptt_owner == cid and bridge.tx_active:
            await _force_unkey(f"audio ws of PTT owner {cid} dropped")
        await _ensure_opus_rx()
        if not audio_clients and not bridge.tx_active:    # last listener left
            await bridge.stop_rx()
            recorder.close()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=1008)
        return
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

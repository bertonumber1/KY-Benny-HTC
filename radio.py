"""Async Bluetooth client for Benshi-protocol radios (VR-N7600).

Transports:
  * BLE GATT via bleak (preferred)
  * Classic Bluetooth RFCOMM (GAIA SPP) via native AF_BLUETOOTH socket

Exposes a high-level async API plus an event callback for radio-pushed
notifications (status, channel and settings changes, RX data, position).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import time

from bleak import BleakClient, BleakScanner

import benshi as bp
from benshi import (Channel, Cmd, CommandFailed, DevInfo, Event, GaiaDeframer,
                    HTStatus, Position, ProtocolError, Settings)

log = logging.getLogger("radio")

EVENTS_TO_REGISTER = [
    Event.HT_STATUS_CHANGED,
    Event.HT_CH_CHANGED,
    Event.HT_SETTINGS_CHANGED,
    Event.DATA_RXD,
    Event.POSITION_CHANGED,
    Event.FREQ_SCAN_STATUS_CHANGED,
]


class RfcommTransport:
    """GAIA-framed classic-BT RFCOMM stream."""

    def __init__(self, mac: str, channel: int | None):
        self.mac = mac
        self.channel = channel
        self.sock: socket.socket | None = None
        self._deframer = GaiaDeframer()
        self.on_frame = None      # (group, cmd, payload)
        self.on_disconnect = None
        self._reader_task = None

    @staticmethod
    def find_channel(mac: str) -> int | None:
        """Locate the GAIA SPP channel via SDP."""
        try:
            out = subprocess.run(["sdptool", "records", mac], timeout=15,
                                 capture_output=True, text=True).stdout.lower()
        except Exception as e:
            log.warning("sdptool failed: %s", e)
            return None
        chan, seen_gaia = None, False
        for line in out.splitlines():
            line = line.strip()
            if "1107-d102" in line or "00001107" in line:
                seen_gaia = True
            if seen_gaia and line.startswith("channel:"):
                try:
                    chan = int(line.split(":")[1])
                except ValueError:
                    pass
                break
        return chan

    async def connect(self):
        loop = asyncio.get_running_loop()
        chan = self.channel
        if chan is None:
            chan = await loop.run_in_executor(None, self.find_channel, self.mac)
        candidates = [chan] if chan else [1, 2, 3, 4, 5]
        last_err = None
        for c in candidates:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                              socket.BTPROTO_RFCOMM)
            s.setblocking(False)
            try:
                await loop.sock_connect(s, (self.mac, c))
                self.sock = s
                self.channel = c
                break
            except OSError as e:
                last_err = e
                s.close()
        if self.sock is None:
            raise ConnectionError(f"RFCOMM connect failed: {last_err}")
        self._reader_task = asyncio.create_task(self._reader())
        log.info("RFCOMM connected to %s channel %s", self.mac, self.channel)

    async def _reader(self):
        loop = asyncio.get_running_loop()
        try:
            while self.sock:
                data = await loop.sock_recv(self.sock, 512)
                if not data:
                    break
                for group, cmd, payload in self._deframer.feed(data):
                    if self.on_frame:
                        self.on_frame(group, cmd, payload)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            if self.on_disconnect:
                self.on_disconnect()

    async def send(self, group: int, cmd: int, payload: bytes):
        if not self.sock:
            raise ConnectionError("not connected")
        await asyncio.get_running_loop().sock_sendall(
            self.sock, bp.gaia_frame(group, cmd, payload))

    async def close(self):
        sock, self.sock = self.sock, None
        if self._reader_task:
            self._reader_task.cancel()
        if sock:
            try:
                sock.close()
            except OSError:
                pass


class BleTransport:
    def __init__(self, mac: str):
        self.mac = mac
        self.client: BleakClient | None = None
        self.on_frame = None
        self.on_disconnect = None

    async def connect(self):
        def disconnected(_):
            if self.on_disconnect:
                self.on_disconnect()

        client = BleakClient(self.mac, disconnected_callback=disconnected,
                             timeout=20.0)
        await client.connect()
        try:
            def handler(_, data: bytearray):
                try:
                    group, cmd, payload = bp.parse_ble_frame(bytes(data))
                except ProtocolError:
                    return
                if self.on_frame:
                    self.on_frame(group, cmd, payload)

            await client.start_notify(bp.GATT_INDICATE, handler)
        except Exception:
            await client.disconnect()
            raise
        self.client = client
        log.info("BLE connected to %s", self.mac)

    async def send(self, group: int, cmd: int, payload: bytes):
        if not self.client or not self.client.is_connected:
            raise ConnectionError("not connected")
        await self.client.write_gatt_char(
            bp.GATT_WRITE, bp.ble_frame(group, cmd, payload), response=True)

    async def close(self):
        client, self.client = self.client, None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


class Radio:
    def __init__(self):
        self.transport = None
        self.transport_kind = None
        self.mac = None
        self.dev_info: DevInfo | None = None
        self.settings: Settings | None = None
        self.ht_status: HTStatus | None = None
        self.channels: dict[int, Channel] = {}
        self.volume: int | None = None
        self.battery_pct: int | None = None
        self.battery_voltage: float | None = None
        self.position: Position | None = None
        self.connected = False
        self.event_cb = None          # async callable(kind, data-dict)
        self._pending: dict[int, asyncio.Future] = {}
        self._cmd_lock = asyncio.Lock()
        self._last_write = 0.0

    # ------------------------------------------------------------ transport

    async def connect(self, mac: str, transport: str = "auto",
                      rfcomm_channel: int | None = None):
        await self.disconnect()
        self.mac = mac.upper()
        errors = []
        kinds = ["ble", "rfcomm"] if transport == "auto" else [transport]
        for kind in kinds:
            t = (BleTransport(self.mac) if kind == "ble"
                 else RfcommTransport(self.mac, rfcomm_channel))
            t.on_frame = self._on_frame
            t.on_disconnect = self._on_disconnect
            try:
                await t.connect()
                self.transport, self.transport_kind = t, kind
                break
            except Exception as e:
                errors.append(f"{kind}: {e}")
                log.warning("connect via %s failed: %s", kind, e)
        if not self.transport:
            raise ConnectionError("; ".join(errors))
        self.connected = True
        await self._init_radio()

    async def disconnect(self):
        t, self.transport = self.transport, None
        self.connected = False
        if t:
            t.on_disconnect = None
            await t.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    def _on_disconnect(self):
        log.warning("radio link lost")
        self.connected = False
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._emit("connection", {"connected": False})

    # ------------------------------------------------------------- commands

    async def _command(self, cmd: Cmd, payload: bytes = b"",
                       timeout: float = 4.0, expect_reply: bool = True) -> bytes:
        """Send a BASIC-group command and await its reply payload."""
        if not self.transport:
            raise ConnectionError("not connected")
        async with self._cmd_lock:
            fut = None
            if expect_reply:
                fut = asyncio.get_running_loop().create_future()
                self._pending[int(cmd)] = fut
            # radio misbehaves if writes are back-to-back; pace like the app
            gap = time.monotonic() - self._last_write
            if gap < 0.02:
                await asyncio.sleep(0.02 - gap)
            await self.transport.send(bp.GROUP_BASIC, int(cmd), payload)
            self._last_write = time.monotonic()
            if not expect_reply:
                return b""
            try:
                reply = await asyncio.wait_for(fut, timeout)
            finally:
                self._pending.pop(int(cmd), None)
            if not reply:
                raise ProtocolError(f"{cmd.name}: empty reply")
            if reply[0] != bp.Status.SUCCESS:
                raise CommandFailed(int(cmd), reply[0])
            return reply

    def _on_frame(self, group: int, cmd: int, payload: bytes):
        if group != bp.GROUP_BASIC:
            log.debug("frame group=%d cmd=0x%04x %s", group, cmd, payload.hex())
            return
        if cmd & bp.REPLY_BIT:
            base = cmd & ~bp.REPLY_BIT
            fut = self._pending.get(base)
            if fut and not fut.done():
                fut.set_result(payload)
            return
        if cmd == Cmd.EVENT_NOTIFICATION and payload:
            self._handle_event(payload)

    # --------------------------------------------------------------- events

    def _emit(self, kind: str, data: dict):
        if self.event_cb:
            asyncio.ensure_future(self.event_cb(kind, data))

    def _handle_event(self, payload: bytes):
        ev = Event(payload[0]) if payload[0] < len(Event) else Event.UNKNOWN
        try:
            if ev == Event.HT_STATUS_CHANGED:
                self.ht_status = HTStatus.parse(payload, 1)
                self._emit("ht_status", bp.struct_dict(self.ht_status))
            elif ev == Event.HT_CH_CHANGED and len(payload) >= 26:
                ch_id = payload[1]
                ext = self.dev_info.channel_ext_size() if self.dev_info else 0
                ch = Channel.parse(payload, 2, ext, ch_id)
                self.channels[ch_id] = ch
                self._emit("channel", bp.struct_dict(ch))
            elif ev == Event.HT_SETTINGS_CHANGED:
                self.settings = Settings.parse(payload, 1)
                self._emit("settings", self.settings.fields)
            elif ev == Event.POSITION_CHANGED:
                self.position = Position.parse(payload, 1)
                self._emit("position", bp.struct_dict(self.position))
            elif ev == Event.DATA_RXD:
                self._emit("data_rxd", {"hex": payload[1:].hex()})
            else:
                self._emit("event", {"type": ev.name, "hex": payload[1:].hex()})
        except ProtocolError as e:
            log.warning("bad event %s: %s", ev.name, e)

    # ------------------------------------------------------------ high level

    async def _init_radio(self):
        self.dev_info = DevInfo.parse(
            await self._command(Cmd.GET_DEV_INFO, bytes([3])))
        log.info("dev info: %s", self.dev_info)
        for ev in EVENTS_TO_REGISTER:
            try:
                await self._command(Cmd.REGISTER_NOTIFICATION, bytes([int(ev)]),
                                    expect_reply=False)
            except Exception as e:
                log.warning("register %s: %s", ev.name, e)
        await self.refresh_status()
        try:
            self.settings = Settings.parse(
                await self._command(Cmd.READ_SETTINGS), 1)
        except (CommandFailed, ProtocolError, asyncio.TimeoutError) as e:
            log.warning("read settings: %s", e)
        self._emit("connection", {"connected": True})

    async def refresh_status(self):
        try:
            self.ht_status = HTStatus.parse(
                await self._command(Cmd.GET_HT_STATUS), 1)
        except (CommandFailed, asyncio.TimeoutError) as e:
            log.warning("ht status: %s", e)
        try:
            r = await self._command(Cmd.GET_VOLUME)
            self.volume = r[1]
        except (CommandFailed, asyncio.TimeoutError, IndexError):
            pass
        try:
            r = await self._command(
                Cmd.READ_STATUS,
                int(bp.StatusType.BATTERY_LEVEL_AS_PERCENTAGE).to_bytes(2, "big"))
            self.battery_pct = r[3]
        except (CommandFailed, asyncio.TimeoutError, IndexError):
            pass
        try:
            r = await self._command(
                Cmd.READ_STATUS,
                int(bp.StatusType.BATTERY_VOLTAGE).to_bytes(2, "big"))
            self.battery_voltage = int.from_bytes(r[3:5], "big") / 1000.0
        except (CommandFailed, asyncio.TimeoutError, IndexError):
            pass

    async def read_channel(self, ch_id: int) -> Channel:
        r = await self._command(Cmd.READ_RF_CH, bytes([ch_id]))
        ext = self.dev_info.channel_ext_size() if self.dev_info else 0
        ch = Channel.parse(r, 2, ext, r[1])
        self.channels[ch.channel_id] = ch
        return ch

    async def read_all_channels(self) -> list[Channel]:
        count = self.dev_info.channel_count if self.dev_info else 16
        out = []
        for i in range(count):
            try:
                out.append(await self.read_channel(i))
            except (CommandFailed, ProtocolError, asyncio.TimeoutError) as e:
                log.warning("read ch %d: %s", i, e)
        return out

    async def write_channel(self, ch: Channel):
        ext = self.dev_info.channel_ext_size() if self.dev_info else 0
        payload = bytes([ch.channel_id]) + ch.to_bytes(ext)
        await self._command(Cmd.WRITE_RF_CH, payload)
        self.channels[ch.channel_id] = ch
        await self._command(Cmd.STORE_SETTINGS, expect_reply=False)

    async def write_settings(self, updates: dict, store: bool = False):
        if self.settings is None:
            self.settings = Settings.parse(
                await self._command(Cmd.READ_SETTINGS), 1)
        self.settings.fields.update(updates)
        await self._command(Cmd.WRITE_SETTINGS, self.settings.to_bytes(),
                            expect_reply=False)
        if store:
            await self._command(Cmd.STORE_SETTINGS, expect_reply=False)

    async def set_channel(self, vfo: str, ch_id: int):
        key = "channel_a" if vfo.lower() == "a" else "channel_b"
        await self.write_settings({key: ch_id})

    async def set_dual_watch(self, mode: int):
        await self.write_settings({"double_channel": mode})

    async def set_scan(self, on: bool):
        await self.write_settings({"scan": 1 if on else 0})

    async def set_squelch(self, level: int):
        await self.write_settings({"squelch_level": max(0, min(15, level))})

    async def set_volume(self, level: int):
        await self._command(Cmd.SET_VOLUME,
                            bytes([max(0, min(15, level))]), expect_reply=False)
        self.volume = level

    async def set_power(self, on: bool):
        await self._command(Cmd.SET_HT_ON_OFF, bytes([1 if on else 0]),
                            expect_reply=False)

    async def get_position(self) -> Position:
        r = await self._command(Cmd.GET_POSITION)
        self.position = Position.parse(r, 1)
        return self.position

    # -------------------------------------------------------------- helpers

    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "transport": self.transport_kind,
            "mac": self.mac,
            "dev_info": bp.struct_dict(self.dev_info) if self.dev_info else None,
            "ht_status": bp.struct_dict(self.ht_status) if self.ht_status else None,
            "settings": self.settings.fields if self.settings else None,
            "volume": self.volume,
            "battery_pct": self.battery_pct,
            "battery_voltage": self.battery_voltage,
            "position": bp.struct_dict(self.position) if self.position else None,
            "channels": [bp.struct_dict(c) for _, c in sorted(self.channels.items())],
        }


async def scan_devices(seconds: float = 8.0) -> list[dict]:
    """BLE scan plus already-paired classic devices."""
    found = {}
    try:
        devices = await BleakScanner.discover(timeout=seconds)
        for d in devices:
            if d.name:
                found[d.address.upper()] = {"mac": d.address.upper(),
                                            "name": d.name, "source": "ble"}
    except Exception as e:
        log.warning("BLE scan failed: %s", e)
    try:
        out = subprocess.run(["bluetoothctl", "devices"], timeout=10,
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                mac = parts[1].upper()
                if mac not in found:
                    found[mac] = {"mac": mac, "name": parts[2],
                                  "source": "paired"}
    except Exception as e:
        log.warning("bluetoothctl scan failed: %s", e)
    return sorted(found.values(), key=lambda x: x["name"] or "")


# --------------------------------------------------------------- APRS mixin

import aprs as aprs_codec
from benshi import BssSettings, TncReassembler, fragment_tnc


class AprsRadio(Radio):
    def __init__(self):
        super().__init__()
        self.bss: BssSettings | None = None
        self.aprs_path: str = ""
        self.aprs_log: list[dict] = []      # decoded RX/TX reports
        self._reassembler = TncReassembler()
        self._msg_counter = 0

    # -- overrides ---------------------------------------------------------

    async def _init_radio(self):
        await super()._init_radio()
        try:
            await self.read_bss()
        except Exception as e:
            log.warning("read bss: %s", e)
        try:
            await self.read_aprs_path()
        except Exception as e:
            log.warning("read aprs path: %s", e)

    def _handle_event(self, payload: bytes):
        ev = payload[0]
        if ev == int(Event.DATA_RXD):
            done = self._reassembler.feed(payload[1:])
            if done:
                frame, ch_id = done
                self._on_tnc_frame(frame, ch_id)
            return
        if ev == int(Event.BSS_SETTINGS_CHANGED):
            try:
                self.bss = BssSettings.parse(payload, 1)
                self._emit("bss", self.bss.fields)
            except Exception as e:
                log.warning("bss event: %s", e)
            return
        super()._handle_event(payload)

    def _on_tnc_frame(self, frame: bytes, ch_id):
        ax = aprs_codec.parse_ax25(frame)
        if ax:
            report = aprs_codec.decode_aprs(ax)
        else:
            report = {"type": "raw", "raw": frame.hex(),
                      "source": "?", "time": int(__import__("time").time())}
        report["channel_id"] = ch_id
        report["dir"] = "rx"
        self.aprs_log.append(report)
        del self.aprs_log[:-300]
        self._emit("aprs", report)

    # -- BSS / path --------------------------------------------------------

    async def read_bss(self) -> BssSettings:
        r = await self._command(Cmd.READ_BSS_SETTINGS, bytes([3]))
        self.bss = BssSettings.parse(r, 1)
        return self.bss

    async def write_bss(self, updates: dict):
        if self.bss is None:
            await self.read_bss()
        self.bss.fields.update(updates)
        if self.dev_info:
            self.bss.size = BssSettings.size_for_fw(self.dev_info.soft_ver)
        await self._command(Cmd.WRITE_BSS_SETTINGS, self.bss.to_bytes())

    async def read_aprs_path(self) -> str:
        r = await self._command(Cmd.GET_APRS_PATH)
        self.aprs_path = r[1:].split(b"\x00")[0].decode("ascii", "replace").strip()
        return self.aprs_path

    async def set_aprs_path(self, path: str):
        await self._command(Cmd.SET_APRS_PATH,
                            path.upper().encode("ascii", "replace"),
                            expect_reply=False)
        self.aprs_path = path.upper()

    # -- TNC TX ------------------------------------------------------------

    def _my_call(self) -> str:
        if not self.bss:
            raise ProtocolError("BSS settings not loaded (no callsign)")
        call = str(self.bss.fields.get("aprs_callsign", "")).strip()
        if not call:
            raise ProtocolError("no APRS callsign configured on radio")
        ssid = int(self.bss.fields.get("aprs_ssid", 0))
        return f"{call}-{ssid}" if ssid else call

    def _path_list(self) -> list[str]:
        p = self.aprs_path.replace(" ", "")
        return [s for s in p.split(",") if s] if p else ["WIDE1-1"]

    async def send_tnc_frame(self, frame: bytes, channel_id: int | None = None):
        for frag in fragment_tnc(frame, channel_id):
            await self._command(Cmd.HT_SEND_DATA, frag)

    async def send_aprs_message(self, to: str, text: str,
                                channel_id: int | None = None) -> dict:
        self._msg_counter = (self._msg_counter % 99) + 1
        src = self._my_call()
        info = aprs_codec.message_info(to, text, str(self._msg_counter))
        frame = aprs_codec.build_ui_frame(src, aprs_codec.APRS_DEST,
                                          self._path_list(), info)
        await self.send_tnc_frame(frame, channel_id)
        report = {"type": "message", "dir": "tx", "source": src,
                  "addressee": to.upper(), "text": text,
                  "msg_id": str(self._msg_counter),
                  "time": int(__import__("time").time())}
        self.aprs_log.append(report)
        self._emit("aprs", report)
        return report

    async def send_aprs_beacon(self, lat: float, lon: float,
                               comment: str = "",
                               channel_id: int | None = None) -> dict:
        src = self._my_call()
        sym = str(self.bss.fields.get("aprs_symbol", "/$")) if self.bss else "/$"
        info = aprs_codec.position_info(lat, lon, sym, comment)
        frame = aprs_codec.build_ui_frame(src, aprs_codec.APRS_DEST,
                                          self._path_list(), info)
        await self.send_tnc_frame(frame, channel_id)
        report = {"type": "position", "dir": "tx", "source": src,
                  "latitude": lat, "longitude": lon, "comment": comment,
                  "symbol": sym, "time": int(__import__("time").time())}
        self.aprs_log.append(report)
        self._emit("aprs", report)
        return report

    async def send_aprs_status(self, text: str,
                               channel_id: int | None = None) -> dict:
        src = self._my_call()
        frame = aprs_codec.build_ui_frame(src, aprs_codec.APRS_DEST,
                                          self._path_list(),
                                          aprs_codec.status_info(text))
        await self.send_tnc_frame(frame, channel_id)
        report = {"type": "status", "dir": "tx", "source": src, "text": text,
                  "time": int(__import__("time").time())}
        self.aprs_log.append(report)
        self._emit("aprs", report)
        return report

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["bss"] = self.bss.fields if self.bss else None
        snap["aprs_path"] = self.aprs_path
        snap["aprs_log"] = self.aprs_log[-100:]
        return snap

"""AOC voice bridge — the VR-N7600's "BS AOC" RFCOMM audio channel <-> browser.

How the radio really does voice (FINDINGS §12b; HFP was a dead end §12):
    The radio exposes a vendor RFCOMM service, UUID
    39144315-32FA-40DB-85ED-FBFEBA2D86E6 ("BS AOC", channel 2). Everything on
    it is 0x7e-delimited / 0x7d-escaped (escaped byte = next XOR 0x20). The
    first unescaped byte of a frame is the command:
        0x00 audio (both directions)   0x01 audio end   0x02 ack   0x09 echo
    An audio frame's payload is concatenated SBC frames: 32 kHz mono,
    16 blocks, 8 subbands, loudness, bitpool 18 (header 0x9c 0x71 0x12,
    44 bytes each, 4 ms of audio).
    PTT is implicit: the radio KEYS as soon as SBC audio frames arrive and
    UNKEYS on the end frame  7e 01 00 01 00 00 00 00 00 00 7e.
    RX works the moment the channel is open — squelch opening streams SBC
    at us (HT_STATUS.is_aoc_connected mirrors this link).

Codec: ffmpeg does SBC both ways (validated live: decode of off-air capture
and encode round-trip at -b:a 88k give byte-identical framing, header 0x71
bitpool 18). We pipe through two small ffmpeg subprocesses instead of porting
SBC to Python.

The browser talks plain PCM: int16 mono little-endian at RADIO_RATE (32 kHz)
over /ws/audio, same as before — only the rate changed. app.py drives:
    start_rx(loop)  on first audio-WS client   -> connect + spawn decoder
    stop_rx()       when the last client left
    start_tx()      on PTT down                -> spawn encoder
    feed_tx(pcm)    mic PCM from the operator's WS
    stop_tx()       on PTT up                  -> flush, send END frame
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid as uuidlib
from typing import Callable, Optional

log = logging.getLogger("audio")

RADIO_RATE = 32000            # SBC as the radio speaks it
CHANNELS = 1
# TX encodes at bitpool 40 like HTCommander (the radio RXes to us at 18, but
# live tests showed our bp18 TX airs a keyed carrier with broken/no
# modulation while bp40 airs clean audio): 88 B per 4 ms SBC frame.
SBC_FRAME_BYTES = 88
SBC_BITRATE = "176k"          # -b:a that yields bitpool 40
FRAMES_PER_PACKET = 3         # <300 B per 0x7e frame, like HTCommander
# The radio's playout needs a lead: exact real-time delivery airs choppy,
# a 0.3 s head start airs clean (0.5 s also fine; live-tested). Costs the
# same 0.3 s in PTT-to-air latency.
TX_LEAD_S = 0.3
SBC_BYTES_PER_SEC = SBC_FRAME_BYTES * 250     # 250 frames/s @ 4 ms
AOC_UUID = uuidlib.UUID("39144315-32FA-40DB-85ED-FBFEBA2D86E6")

CMD_AUDIO = 0x00
CMD_END = 0x01
CMD_ACK = 0x02
CMD_AUDIO_RX = 0x03
CMD_ECHO = 0x09
END_FRAME = bytes.fromhex("7e 01 00 01 00 00 00 00 00 00 7e".replace(" ", ""))

try:
    from winrt.windows.devices.bluetooth import BluetoothDevice
    from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
    from winrt.windows.networking.sockets import StreamSocket
    from winrt.windows.storage.streams import (DataReader, DataWriter,
                                               InputStreamOptions)
    HAVE_WINRT = True
except Exception as e:                                    # noqa: BLE001
    HAVE_WINRT = False
    log.warning("winrt unavailable (%s) — AOC voice bridge disabled", e)


def _app_dir() -> str:
    """Directory the app lives in: next to the exe when packaged, else here."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg(configured: str | None = None) -> Optional[str]:
    """ffmpeg lives wherever the user put it: config wins, then PATH, then
    next to the app (drop ffmpeg.exe or an ffmpeg/bin tree beside the exe),
    then common install spots."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    here = _app_dir()
    for cand in (configured,
                 shutil.which("ffmpeg"),
                 os.path.join(here, exe),
                 os.path.join(here, "ffmpeg", "bin", exe),
                 os.path.join(here, "ffmpeg", exe),
                 os.path.expanduser("~/Desktop/ffmpeg/bin/" + exe),
                 r"C:\ffmpeg\bin\ffmpeg.exe" if os.name == "nt" else None):
        if cand and os.path.isfile(cand):
            return cand
    return None


def escape(data: bytes) -> bytes:
    """0x7d/0x7e are reserved on the wire; escape = 7d, byte^0x20."""
    out = bytearray()
    for b in data:
        if b in (0x7D, 0x7E):
            out += bytes((0x7D, b ^ 0x20))
        else:
            out.append(b)
    return bytes(out)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill an ffmpeg pipe process and release its transports (otherwise
    proactor pipes warn 'unclosed transport' at GC time)."""
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass


class Deframer:
    """Split the incoming byte stream into unescaped frame payloads."""

    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for b in data:
            if b == 0x7E:
                if self.buf:
                    frames.append(bytes(self.buf))
                    self.buf = bytearray()
                self.esc = False
            elif self.esc:
                self.buf.append(b ^ 0x20)
                self.esc = False
            elif b == 0x7D:
                self.esc = True
            else:
                self.buf.append(b)
        return frames


class AudioBridge:
    """One AOC link + two ffmpeg pipes. Single instance per process."""

    def __init__(self, address: int = 0, ffmpeg: str | None = None):
        self.address = address                 # radio BT address as int
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.rx_cb: Optional[Callable[[bytes], None]] = None  # async(pcm)
        self.rx_active = False
        self.tx_active = False
        self.connected = False
        self.last_error: str | None = None
        self._sock = None
        self._writer = None
        self._reader_task: Optional[asyncio.Task] = None
        self._dec: Optional[asyncio.subprocess.Process] = None
        self._dec_task: Optional[asyncio.Task] = None
        self._enc: Optional[asyncio.subprocess.Process] = None
        self._enc_task: Optional[asyncio.Task] = None
        self._sbc_out = bytearray()            # encoder output, 44 B aligned
        self._write_lock = asyncio.Lock()
        self._conn_lock = asyncio.Lock()       # RX and TX may race to connect

    # ---- status ----------------------------------------------------------
    def status(self) -> dict:
        return {
            "have_audio": HAVE_WINRT and self.ffmpeg is not None,
            "ffmpeg": self.ffmpeg,
            "rate": RADIO_RATE,
            "link_up": self.connected,
            "rx_active": self.rx_active,
            "tx_active": self.tx_active,
            "error": self.last_error,
        }

    # ---- AOC socket ------------------------------------------------------
    async def connect(self) -> None:
        """Open the AOC RFCOMM channel and start the frame reader."""
        async with self._conn_lock:
            if self.connected:
                return
            if not HAVE_WINRT:
                raise RuntimeError("winrt Bluetooth packages not installed")
            if not self.ffmpeg:
                raise RuntimeError("ffmpeg not found — set audio.ffmpeg_path "
                                   "in config.json or add ffmpeg to PATH")
            if not self.address:
                raise RuntimeError("radio Bluetooth address unknown")
            self.loop = asyncio.get_running_loop()
            try:
                dev = await BluetoothDevice.from_bluetooth_address_async(
                    self.address)
                res = await dev.get_rfcomm_services_for_id_async(
                    RfcommServiceId.from_uuid(AOC_UUID))
                if res.services.size == 0:
                    raise RuntimeError("AOC audio service not found — radio "
                                       "asleep, out of range, or channel held "
                                       "by another app")
                svc = res.services.get_at(0)
                self._sock = StreamSocket()
                await self._sock.connect_async(svc.connection_host_name,
                                               svc.connection_service_name)
                self._writer = DataWriter(self._sock.output_stream)
            except Exception as e:
                self.last_error = str(e)
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:                     # noqa: BLE001
                        pass
                self._sock = self._writer = None
                raise
            self.connected = True
            self.last_error = None
            self._reader_task = asyncio.create_task(self._read_loop())
            log.info("AOC channel connected")

    async def disconnect(self) -> None:
        await self._stop_decoder()
        await self._stop_encoder(send_end=False)
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:                             # noqa: BLE001
                pass
        self._sock = self._writer = None
        self.connected = False
        log.info("AOC channel closed")

    async def _send_frame(self, payload: bytes) -> None:
        """payload = cmd byte + body, pre-escape. END_FRAME goes raw."""
        data = b"\x7e" + escape(payload) + b"\x7e"
        async with self._write_lock:
            self._writer.write_bytes(data)
            await self._writer.store_async()

    async def _read_loop(self) -> None:
        """Socket -> deframe -> route. Runs for the life of the connection."""
        reader = DataReader(self._sock.input_stream)
        reader.input_stream_options = InputStreamOptions.PARTIAL
        deframer = Deframer()
        try:
            while True:
                n = await reader.load_async(4096)
                if n == 0:
                    raise ConnectionError("AOC link EOF")
                for fr in deframer.feed(bytes(reader.read_buffer(n))):
                    cmd = fr[0]
                    if cmd in (CMD_AUDIO, CMD_AUDIO_RX) and len(fr) > 1:
                        await self._on_rx_sbc(fr[1:])
                    # 0x02 ack / 0x09 echo: nothing to do yet
        except asyncio.CancelledError:
            pass
        except Exception as e:                            # noqa: BLE001
            self.last_error = str(e)
            log.warning("AOC reader died: %s", e)
            self.connected = False
            await self._stop_decoder()
            await self._stop_encoder(send_end=False)

    # ---- RX: radio -> browser (SBC -> ffmpeg -> PCM) ---------------------
    async def _on_rx_sbc(self, sbc: bytes) -> None:
        if self._dec and self._dec.stdin:
            try:
                self._dec.stdin.write(sbc)
                await self._dec.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self._stop_decoder()

    async def start_rx(self) -> None:
        """Spawn the SBC decoder; rx_cb starts receiving PCM whenever the
        radio's squelch opens."""
        if self.rx_active:
            return
        await self.connect()
        self._dec = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-probesize", "32",
            "-f", "sbc", "-i", "pipe:0",
            "-f", "s16le", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._dec_task = asyncio.create_task(self._pump_decoded())
        self.rx_active = True
        log.info("RX decoder up (ffmpeg pid %s)", self._dec.pid)

    async def _pump_decoded(self) -> None:
        """Decoder PCM -> rx_cb in ~20 ms chunks."""
        chunk = RADIO_RATE // 50 * 2                      # 20 ms of int16
        try:
            while self._dec and self._dec.stdout:
                data = await self._dec.stdout.read(chunk)
                if not data:
                    break
                if self.rx_cb:
                    res = self.rx_cb(data)
                    if asyncio.iscoroutine(res):
                        await res
        except asyncio.CancelledError:
            pass
        except Exception as e:                            # noqa: BLE001
            log.warning("rx pump: %s", e)

    async def _stop_decoder(self) -> None:
        if self._dec_task:
            self._dec_task.cancel()
            self._dec_task = None
        if self._dec:
            await _reap(self._dec)
            self._dec = None
        self.rx_active = False

    async def stop_rx(self) -> None:
        await self._stop_decoder()
        if not self.tx_active:
            await self.disconnect()

    # ---- TX: browser -> radio (PCM -> ffmpeg -> SBC -> frames) -----------
    async def start_tx(self) -> None:
        """Spawn the SBC encoder. The radio keys itself when frames flow."""
        if self.tx_active:
            return
        await self.connect()
        self._sbc_out = bytearray()
        self._enc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(RADIO_RATE), "-ac", str(CHANNELS),
            "-i", "pipe:0",
            "-c:a", "sbc", "-b:a", SBC_BITRATE,
            "-f", "sbc", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._enc_task = asyncio.create_task(self._pump_encoded())
        self.tx_active = True
        log.info("TX encoder up (ffmpeg pid %s)", self._enc.pid)

    def feed_tx(self, pcm: bytes) -> None:
        """Mic PCM (int16 mono 32 kHz) from the operator's WS."""
        if not self.tx_active or not self._enc or not self._enc.stdin:
            return
        try:
            self._enc.stdin.write(pcm)
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _pump_encoded(self) -> None:
        """Encoder SBC -> whole-frame-aligned 0x7e audio packets. The mic
        delivers in real time, so pacing is inherited from the source — but
        we hold back TX_LEAD_S before the first packet so the radio's playout
        keeps that much buffer ahead for the rest of the transmission."""
        packet = SBC_FRAME_BYTES * FRAMES_PER_PACKET
        lead = int(TX_LEAD_S * SBC_BYTES_PER_SEC)
        primed = False
        try:
            while self._enc and self._enc.stdout:
                data = await self._enc.stdout.read(4096)
                if not data:
                    break
                self._sbc_out += data
                if not primed:
                    if len(self._sbc_out) < lead:
                        continue
                    primed = True
                while len(self._sbc_out) >= packet:
                    body = bytes(self._sbc_out[:packet])
                    del self._sbc_out[:packet]
                    await self._send_frame(bytes([CMD_AUDIO]) + body)
        except asyncio.CancelledError:
            pass
        except Exception as e:                            # noqa: BLE001
            log.warning("tx pump: %s", e)

    async def stop_tx(self) -> None:
        """Flush what's left, then send the END frame to unkey the radio."""
        await self._stop_encoder(send_end=True)
        if not self.rx_active:
            await self.disconnect()

    async def _stop_encoder(self, send_end: bool) -> None:
        enc, self._enc = self._enc, None
        if self._enc_task:
            task = self._enc_task
            self._enc_task = None
            if enc and enc.stdin:
                try:
                    enc.stdin.close()                     # EOF -> flush
                except Exception:                         # noqa: BLE001
                    pass
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        if enc:
            await _reap(enc)
        # ship any whole frames the flush produced, drop a partial tail
        tail = bytes(self._sbc_out[:len(self._sbc_out)
                                   // SBC_FRAME_BYTES * SBC_FRAME_BYTES])
        self._sbc_out = bytearray()
        if self.connected:
            try:
                if send_end and tail:
                    await self._send_frame(bytes([CMD_AUDIO]) + tail)
                if send_end:
                    async with self._write_lock:
                        self._writer.write_bytes(END_FRAME)
                        await self._writer.store_async()
            except Exception as e:                        # noqa: BLE001
                log.warning("tx end frame: %s", e)
        self.tx_active = False

    # ---- teardown --------------------------------------------------------
    async def close(self) -> None:
        await self.disconnect()


# ---------------------------------------------------------------- Opus relay

class OggPacketParser:
    """Extract raw packets from an Ogg byte stream (read-only, no CRC check).

    Enough for a live ffmpeg ogg/opus pipe: accumulates pages, joins lacing
    segments (and continued packets), yields each finished packet."""

    def __init__(self):
        self.buf = bytearray()
        self._partial = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"OggS")
            if i < 0:
                if len(self.buf) > 3:          # keep a possible partial magic
                    del self.buf[:-3]
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < 27:
                break
            nsegs = self.buf[26]
            if len(self.buf) < 27 + nsegs:
                break
            segs = self.buf[27:27 + nsegs]
            body_len = sum(segs)
            total = 27 + nsegs + body_len
            if len(self.buf) < total:
                break
            body = self.buf[27 + nsegs:total]
            del self.buf[:total]
            pos = 0
            for s in segs:
                self._partial += body[pos:pos + s]
                pos += s
                if s < 255:                    # lacing < 255 ends a packet
                    out.append(bytes(self._partial))
                    self._partial = bytearray()
        return out


class OpusRxStreamer:
    """PCM (int16 mono RADIO_RATE) -> ffmpeg libopus -> raw opus packets.

    One shared instance serves every opus-capable browser: ~24 kbit/s versus
    512 kbit/s for the raw-PCM fallback path."""

    def __init__(self, ffmpeg: str):
        self.ffmpeg = ffmpeg
        self.packet_cb: Optional[Callable[[bytes], None]] = None  # async
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._skip = 2                          # OpusHead + OpusTags

    @property
    def active(self) -> bool:
        return self._proc is not None

    async def start(self) -> None:
        if self._proc:
            return
        self._skip = 2
        self._proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(RADIO_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-frame_duration", "20", "-ar", "48000",
            "-page_duration", "20000",          # one ogg page per packet
            "-f", "ogg", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._task = asyncio.create_task(self._pump())
        log.info("opus RX encoder up (pid %s)", self._proc.pid)

    def feed(self, pcm: bytes) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(pcm)
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _pump(self) -> None:
        parser = OggPacketParser()
        try:
            while self._proc and self._proc.stdout:
                data = await self._proc.stdout.read(4096)
                if not data:
                    break
                for pkt in parser.feed(data):
                    if self._skip:
                        self._skip -= 1
                        continue
                    if self.packet_cb:
                        res = self.packet_cb(pkt)
                        if asyncio.iscoroutine(res):
                            await res
        except asyncio.CancelledError:
            pass
        except Exception as e:                            # noqa: BLE001
            log.warning("opus rx pump: %s", e)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._proc:
            await _reap(self._proc)
            self._proc = None


class RxRecorder:
    """Record RX audio to WAV files, one file per over.

    The AOC channel only carries SBC while the squelch is open, so PCM flow
    IS the squelch: a gap longer than GAP_S closes the current file. Keeps
    the newest KEEP files in <dir>/recordings."""

    GAP_S = 1.2
    MAX_S = 120           # hard cap per file
    KEEP = 30

    def __init__(self, data_dir):
        import pathlib
        self.dir = pathlib.Path(data_dir) / "recordings"
        self.enabled = True
        self._wav = None
        self._path = None
        self._frames = 0
        self._last = 0.0

    def feed(self, pcm: bytes) -> None:
        if not self.enabled:
            return
        import time as _t
        import wave
        now = _t.monotonic()
        if self._wav and (now - self._last > self.GAP_S
                          or self._frames > RADIO_RATE * self.MAX_S):
            self.close()
        if self._wav is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            name = _t.strftime("rx-%Y%m%d-%H%M%S") + ".wav"
            self._path = self.dir / name
            self._wav = wave.open(str(self._path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(RADIO_RATE)
            log.info("recording %s", name)
        self._wav.writeframes(pcm)
        self._frames += len(pcm) // 2
        self._last = now

    def maybe_close(self) -> None:
        """Finalize the open file once the squelch gap has passed (poll me)."""
        import time as _t
        if self._wav and _t.monotonic() - self._last > self.GAP_S:
            self.close()

    def close(self) -> None:
        if self._wav:
            try:
                self._wav.close()
            except Exception:                             # noqa: BLE001
                pass
            # drop blips shorter than 300 ms — squelch crashes, not overs
            if self._frames < RADIO_RATE * 0.3 and self._path:
                try:
                    self._path.unlink()
                except OSError:
                    pass
            self._wav = None
            self._path = None
            self._frames = 0
            self._prune()

    def _prune(self) -> None:
        try:
            files = sorted(self.dir.glob("rx-*.wav"))
            for f in files[:-self.KEEP]:
                f.unlink()
        except OSError:
            pass

    def list(self) -> list[dict]:
        try:
            out = []
            for f in sorted(self.dir.glob("rx-*.wav"), reverse=True):
                st = f.stat()
                out.append({"name": f.name, "bytes": st.st_size,
                            "seconds": round(max(0, st.st_size - 44)
                                             / (RADIO_RATE * 2), 1),
                            "mtime": int(st.st_mtime)})
            return out
        except OSError:
            return []


class WebmMicDecoder:
    """Browser MediaRecorder (webm/opus) chunks -> PCM int16 mono RADIO_RATE.

    One instance per PTT press: MediaRecorder emits a fresh WebM header at
    every start(), which is exactly one ffmpeg run."""

    def __init__(self, ffmpeg: str, pcm_cb: Callable[[bytes], None]):
        self.ffmpeg = ffmpeg
        self.pcm_cb = pcm_cb
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-i", "pipe:0",
            "-f", "s16le", "-ar", str(RADIO_RATE), "-ac", "1",
            "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._task = asyncio.create_task(self._pump())
        log.info("mic webm decoder up (pid %s)", self._proc.pid)

    def feed(self, webm: bytes) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(webm)
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _pump(self) -> None:
        try:
            while self._proc and self._proc.stdout:
                data = await self._proc.stdout.read(4096)
                if not data:
                    break
                self.pcm_cb(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:                            # noqa: BLE001
            log.warning("mic decoder pump: %s", e)

    async def stop(self) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()                  # flush the tail
            except Exception:                             # noqa: BLE001
                pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._proc:
            await _reap(self._proc)
            self._proc = None

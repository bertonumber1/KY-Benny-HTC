"""HFP voice bridge — radio Bluetooth SCO audio <-> browser over a WebSocket.

How the radio exposes voice (see FINDINGS §1c):
    The VR-N7600 advertises a classic-Bluetooth Hands-Free service (UUID
    0000111F). Once bonded, the OS presents the radio as a pair of mono
    8 kHz audio endpoints — on Windows they enumerate under WDM-KS as
        "...Hands-Free HF Audio...(VR-N7600)"  (one input, one output).
        input  endpoint = RX  (audio the radio pulls off-air -> we capture)
        output endpoint = TX  (audio we play -> radio keys it to air)
    On Linux the same SCO link shows up via PipeWire/PulseAudio-HFP (or
    BlueALSA) as an 8 kHz mono source/sink; matching by name still works.

This module owns only the OS audio side. PTT keying lives in radio.py and is
driven by the caller around start_tx()/stop_tx(). We stream raw 16-bit signed
little-endian mono PCM at RADIO_RATE both directions; the browser runs its
AudioContext at the same rate so nobody has to resample.

Design: one bridge per process (single radio). RX frames are handed to
an async callback (app.py broadcasts them to every audio-WS client). TX PCM is
pushed in from the active operator's WS and drained by the output callback.
Everything sounddevice touches runs on its own thread; we hop back to the
asyncio loop with call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import logging
import queue
from typing import Callable, Optional

log = logging.getLogger("audio")

RADIO_RATE = 8000          # HFP narrowband (CVSD) is 8 kHz mono
CHANNELS = 1
DTYPE = "int16"
BLOCK = 160                # 20 ms @ 8 kHz — matches typical SCO framing
DEFAULT_NAME_HINT = "VR-N7600"

try:
    import numpy as np
    import sounddevice as sd
    HAVE_AUDIO = True
except Exception as e:                                   # noqa: BLE001
    np = None
    sd = None
    HAVE_AUDIO = False
    log.warning("audio libs unavailable (%s) — voice bridge disabled", e)


def _is_hf_audio(name: str, hint: str) -> bool:
    n = name.lower()
    return "hands-free" in n and "hf" in n and hint.lower() in n


def list_devices() -> list[dict]:
    """Every audio endpoint the host sees, tagged with whether it looks like
    the radio's Hands-Free link. Powers the /api/audio/devices picker."""
    if not HAVE_AUDIO:
        return []
    out = []
    for i, d in enumerate(sd.query_devices()):
        ha = sd.query_hostapis(d["hostapi"])["name"]
        out.append({
            "index": i,
            "name": d["name"],
            "hostapi": ha,
            "in": d["max_input_channels"],
            "out": d["max_output_channels"],
            "rate": int(d["default_samplerate"]),
            "is_hf": _is_hf_audio(d["name"], DEFAULT_NAME_HINT),
        })
    return out


def find_radio_devices(hint: str = DEFAULT_NAME_HINT
                       ) -> tuple[Optional[int], Optional[int]]:
    """(input_index, output_index) for the radio's Hands-Free endpoints, or
    (None, None) if the SCO link isn't up. Prefer WDM-KS on Windows — it gives
    the raw 8 kHz SCO stream without the shared-mode resampler in the way."""
    if not HAVE_AUDIO:
        return None, None
    in_idx = out_idx = None
    in_ks = out_ks = None
    for i, d in enumerate(sd.query_devices()):
        if not _is_hf_audio(d["name"], hint):
            continue
        ks = sd.query_hostapis(d["hostapi"])["name"] == "Windows WDM-KS"
        if d["max_input_channels"] > 0:
            if in_idx is None or (ks and in_ks is None):
                in_idx, in_ks = i, ks
        if d["max_output_channels"] > 0:
            if out_idx is None or (ks and out_ks is None):
                out_idx, out_ks = i, ks
    return in_idx, out_idx


class AudioBridge:
    """Bidirectional PCM pump between the radio's SCO endpoints and callers."""

    def __init__(self, hint: str = DEFAULT_NAME_HINT):
        self.hint = hint
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._rx_stream = None
        self._tx_stream = None
        self._tx_q: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        self._tx_buf = bytearray()
        self.rx_cb: Optional[Callable[[bytes], None]] = None   # async, PCM in
        self.rx_active = False
        self.tx_active = False
        self.in_idx: Optional[int] = None
        self.out_idx: Optional[int] = None

    # ---- discovery -------------------------------------------------------
    def status(self) -> dict:
        in_idx, out_idx = (find_radio_devices(self.hint)
                           if HAVE_AUDIO else (None, None))
        return {
            "have_audio": HAVE_AUDIO,
            "rate": RADIO_RATE,
            "rx_device": in_idx,
            "tx_device": out_idx,
            "link_up": in_idx is not None and out_idx is not None,
            "rx_active": self.rx_active,
            "tx_active": self.tx_active,
        }

    # ---- RX: radio -> browser -------------------------------------------
    def start_rx(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the radio's capture endpoint and push PCM to rx_cb."""
        if not HAVE_AUDIO or self.rx_active:
            return
        self.loop = loop
        in_idx, _ = find_radio_devices(self.hint)
        if in_idx is None:
            raise RuntimeError("radio Hands-Free input not found — is the "
                               "SCO/HFP link up? (start a call on the radio "
                               "or check pairing)")
        self.in_idx = in_idx

        def cb(indata, frames, time_info, status):        # sd thread
            if status:
                log.debug("rx status: %s", status)
            if self.rx_cb and self.loop:
                data = bytes(indata)
                self.loop.call_soon_threadsafe(self._dispatch_rx, data)

        self._rx_stream = sd.RawInputStream(
            samplerate=RADIO_RATE, blocksize=BLOCK, device=in_idx,
            channels=CHANNELS, dtype=DTYPE, callback=cb)
        self._rx_stream.start()
        self.rx_active = True
        log.info("RX bridge up on device %d", in_idx)

    def _dispatch_rx(self, data: bytes) -> None:
        if self.rx_cb:
            res = self.rx_cb(data)
            if asyncio.iscoroutine(res):
                asyncio.ensure_future(res)

    def stop_rx(self) -> None:
        if self._rx_stream:
            self._rx_stream.stop(); self._rx_stream.close()
            self._rx_stream = None
        self.rx_active = False

    # ---- TX: browser -> radio -------------------------------------------
    def start_tx(self) -> None:
        """Open the radio's playback endpoint. Feed it with feed_tx()."""
        if not HAVE_AUDIO or self.tx_active:
            return
        _, out_idx = find_radio_devices(self.hint)
        if out_idx is None:
            raise RuntimeError("radio Hands-Free output not found")
        self.out_idx = out_idx
        self._tx_buf = bytearray()
        with self._tx_q.mutex:
            self._tx_q.queue.clear()

        def cb(outdata, frames, time_info, status):       # sd thread
            if status:
                log.debug("tx status: %s", status)
            need = frames * 2 * CHANNELS
            while len(self._tx_buf) < need:
                try:
                    self._tx_buf += self._tx_q.get_nowait()
                except queue.Empty:
                    break
            if len(self._tx_buf) >= need:
                outdata[:] = bytes(self._tx_buf[:need])
                del self._tx_buf[:need]
            else:
                outdata[:len(self._tx_buf)] = bytes(self._tx_buf)
                outdata[len(self._tx_buf):] = b"\x00" * (need - len(self._tx_buf))
                self._tx_buf.clear()

        self._tx_stream = sd.RawOutputStream(
            samplerate=RADIO_RATE, blocksize=BLOCK, device=out_idx,
            channels=CHANNELS, dtype=DTYPE, callback=cb)
        self._tx_stream.start()
        self.tx_active = True
        log.info("TX bridge up on device %d", out_idx)

    def feed_tx(self, pcm: bytes) -> None:
        """Queue mic PCM (int16 mono 8 kHz) for playback to the radio."""
        if not self.tx_active:
            return
        try:
            self._tx_q.put_nowait(pcm)
        except queue.Full:
            log.debug("tx queue full — dropping frame (operator too fast?)")

    def stop_tx(self) -> None:
        if self._tx_stream:
            self._tx_stream.stop(); self._tx_stream.close()
            self._tx_stream = None
        self.tx_active = False

    def close(self) -> None:
        self.stop_rx()
        self.stop_tx()

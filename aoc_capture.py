"""Capture SBC audio from the VR-N7600's BS AOC RFCOMM channel.

Connects, deframes the 0x7e/0x7d stream, and writes the raw SBC payload of
audio frames (cmd 0x00/0x03) to captures/rx.sbc for offline decode:
    ffmpeg -f sbc -i captures/rx.sbc -f s16le -ar 32000 -ac 1 captures/rx.raw

Usage: aoc_capture.py [seconds=10]
"""
import asyncio
import sys

from winrt.windows.devices.bluetooth import BluetoothDevice
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.networking.sockets import StreamSocket
from winrt.windows.storage.streams import DataReader, InputStreamOptions
import uuid as uuidlib

MAC = 0x38D200013ECC
AOC_UUID = uuidlib.UUID("39144315-32FA-40DB-85ED-FBFEBA2D86E6")

CMD_NAMES = {0x00: "audio", 0x01: "end", 0x02: "ack", 0x03: "audio3",
             0x09: "echo"}


class Deframer:
    """0x7e-delimited, 0x7d-escaped (next byte XOR 0x20) frame splitter."""

    def __init__(self):
        self.buf = bytearray()
        self.esc = False
        self.in_frame = False

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == 0x7E:
                if self.in_frame and self.buf:
                    frames.append(bytes(self.buf))
                self.buf.clear()
                self.esc = False
                self.in_frame = True
                continue
            if not self.in_frame:
                continue
            if self.esc:
                self.buf.append(b ^ 0x20)
                self.esc = False
            elif b == 0x7D:
                self.esc = True
            else:
                self.buf.append(b)
        return frames


async def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    dev = await BluetoothDevice.from_bluetooth_address_async(MAC)
    sid = RfcommServiceId.from_uuid(AOC_UUID)
    res = await dev.get_rfcomm_services_for_id_async(sid)
    if res.services.size == 0:
        print("AOC service not found (radio asleep?)")
        return
    svc = res.services.get_at(0)
    sock = StreamSocket()
    await sock.connect_async(svc.connection_host_name,
                             svc.connection_service_name)
    print(f"CONNECTED — capturing {secs:.0f}s of AOC frames...")
    reader = DataReader(sock.input_stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL

    deframer = Deframer()
    counts = {}
    sbc = bytearray()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + secs
    while loop.time() < deadline:
        try:
            n = await asyncio.wait_for(reader.load_async(4096),
                                       timeout=max(0.1, deadline - loop.time()))
        except asyncio.TimeoutError:
            break
        if n == 0:
            print("EOF")
            break
        for fr in deframer.feed(bytes(reader.read_buffer(n))):
            cmd = fr[0]
            counts[cmd] = counts.get(cmd, 0) + 1
            if cmd in (0x00, 0x03):
                sbc.extend(fr[1:])
    sock.close()

    print("frame counts:", {f"{c:#04x}({CMD_NAMES.get(c, '?')})": n
                            for c, n in sorted(counts.items())})
    print(f"SBC payload: {len(sbc)} bytes")
    if sbc:
        # sanity: SBC syncword + header
        i = sbc.find(0x9C)
        print(f"first syncword at {i}, header {sbc[i+1]:#04x} "
              f"bitpool {sbc[i+2]}")
        with open("captures/rx.sbc", "wb") as f:
            f.write(sbc)
        print("wrote captures/rx.sbc")


asyncio.run(main())

"""Probe: connect to the VR-N7600's BS AOC audio RFCOMM service via WinRT
and dump whatever arrives. Ctrl-C or timeout to exit."""
import asyncio
import sys

from winrt.windows.devices.bluetooth import BluetoothDevice
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.networking.sockets import StreamSocket
from winrt.windows.storage.streams import DataReader, InputStreamOptions
import uuid as uuidlib

MAC = 0x38D200013ECC
AOC_UUID = uuidlib.UUID("39144315-32FA-40DB-85ED-FBFEBA2D86E6")


async def main():
    dev = await BluetoothDevice.from_bluetooth_address_async(MAC)
    print("device:", dev.name, hex(dev.bluetooth_address))
    sid = RfcommServiceId.from_uuid(AOC_UUID)
    res = await dev.get_rfcomm_services_for_id_async(sid)
    print("services found:", res.services.size, "error:", res.error)
    if res.services.size == 0:
        # fall back: enumerate everything the radio offers
        allres = await dev.get_rfcomm_services_async()
        for i in range(allres.services.size):
            s = allres.services.get_at(i)
            print("  svc:", s.service_id.uuid, s.connection_service_name)
        return
    svc = res.services.get_at(0)
    print("connecting to", svc.connection_host_name.display_name,
          svc.connection_service_name)
    sock = StreamSocket()
    await sock.connect_async(svc.connection_host_name,
                             svc.connection_service_name)
    print("CONNECTED to AOC channel")
    reader = DataReader(sock.input_stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL
    total = 0
    try:
        while True:
            n = await asyncio.wait_for(
                reader.load_async(512), timeout=float(sys.argv[1])
                if len(sys.argv) > 1 else 8.0)
            if n == 0:
                print("EOF")
                break
            buf = bytes(reader.read_buffer(n))
            total += len(buf)
            print(f"rx [{len(buf):3d}B] {buf.hex(' ')}")
    except asyncio.TimeoutError:
        print(f"no more data (total {total}B) — link stays open until close")
    sock.close()
    print("closed")


asyncio.run(main())

# KY-Benny(HTC)

**Web remote control for Benshi-protocol radios (VR-N7600 / BTECH UV-Pro family)** — first sketch & shell.

Browser control of the VR-N7600 over Bluetooth + IP: the Pi holds the
Bluetooth link to the radio (same Benshi/GAIA command protocol the vendor
app uses) and serves a web UI, so the radio can be operated from any browser
on the network — no phone app, no channel-binding to a second device.

## Run

```
python3 app.py            # http://<pi>:8099
```

or install the service:

```
sudo cp vrn7600.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vrn7600.service
```

## First connect

1. Power the radio on with Bluetooth enabled (pairing mode for first use).
2. Open the web UI → **Scan**, click the radio, **Connect**
   (or put its MAC in `config.json`).
3. Transport `auto` tries BLE GATT first, then classic RFCOMM (GAIA SPP).
   With `auto_connect: true` the bridge reconnects whenever the radio
   reappears.

## What works

Tabbed UI: **Dashboard · Channels · APRS · Settings · Log**

- **Dashboard**: virtual radio screen (LCD-style panel mirroring the
  physical display — VFO A/B lines, active channel arrow, TX/RX/scan/DW/GPS
  icons, volume/squelch, battery bar, RSSI), plus dual watch, volume,
  squelch, channel scan, power on/off, GPS fix.
- **Channels**: full memory table read from the radio (name, RX/TX freq,
  mode, tones, bandwidth, power, scan, flags); editor writes everything back
  (WRITE_RF_CH + STORE_SETTINGS), CTCSS dropdowns, A/B select per row.
- **APRS**: live decoded packet feed (positions incl. Mic-E, messages,
  status, objects) from the radio's TNC data channel; send APRS messages,
  position beacons (with browser geolocation) and status; full beacon/BSS
  config (callsign, SSID, symbol, path, interval, smart beacon, Mic-E,
  PTT-release beacons, packet format).
- **Settings**: the radio settings the app exposes — audio (mic gains,
  speaker, tones, tail eliminate, NS), VOX, TX limits, power saving,
  display, GPS/positioning, KISS TNC — with optional persist-to-flash.
- Live updates over WebSocket from radio event notifications + 30 s poll.

## Roadmap

The end goal: **full remote voice operation through the web UI** — talk and
listen from any browser, no channel-binding to a secondary device, nothing
but the radio + this bridge.

- [ ] TX/RX **audio** over IP with PTT (radio streams SBC over classic BT;
      bridge to browser via WebSocket/WebRTC)
- [ ] KISS-over-IP bridge (radio has a KISS TNC mode)
- [ ] APRS map view
- [ ] Live test pass against VR-N7600 hardware

## Files

- `benshi.py` — protocol: GAIA/BLE framing, command set, bit-packed structs
  (dev info, channels, settings, HT status, position, BSS/APRS settings,
  TNC data fragmentation/reassembly).
- `aprs.py` — AX.25 UI-frame + APRS codec (positions, Mic-E, messages,
  status, objects).
- `radio.py` — async client: bleak GATT + AF_BLUETOOTH RFCOMM transports,
  request/reply matching, event notifications, APRS send/receive.
- `app.py` — FastAPI: REST + WebSocket push + static UI. Port in
  `config.json` (default 8099).

## Credits

This project stands on a combination of the efforts of
[Kyle Husmann, KC3SLD (khusmann)](https://github.com/khusmann/benlink) —
whose benlink project documented the Benshi command protocol — and
[Ylian Saint-Hilaire (Ylianst)](https://github.com/Ylianst/HTCommander) —
whose HTCommander proved full PC control including Bluetooth audio.
Own-hardware interoperability project.

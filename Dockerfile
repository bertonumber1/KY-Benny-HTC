# VR-N7600 web bridge — multi-arch (linux/amd64, linux/arm64).
#
# Bluetooth comes from the HOST: run with host networking, the host D-Bus
# socket mounted (BlueZ, for BLE via bleak) and privileged (AF_BLUETOOTH
# RFCOMM sockets). See docker-compose.yml for the working invocation.
#
# Voice note: the AOC audio bridge is currently Windows-only (WinRT); in a
# container the app runs with voice disabled — control + APRS + propagation
# all work. ffmpeg is not installed for that reason; when the Linux voice
# port lands it will be added back.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py radio.py benshi.py aprs.py prop.py audio.py ./
COPY static/ static/

# config.json + aprs_history.jsonl + vrn7600.log live on a volume
ENV VRN7600_DATA=/data
VOLUME /data

EXPOSE 8099
CMD ["python", "app.py"]

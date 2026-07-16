"""PropView-style APRS propagation analytics.

Inspired by RF-YVY/APRS-PropView, but fed from OUR TNC: the radio itself.
Every decoded APRS report (aprs.py via radio.AprsRadio) is appended here.
Reports persist to a JSONL file so heatmaps/leaderboards survive restarts;
analytics work over a rolling 48 h window.

"Direct" = heard with no digipeater used-flag (*) in the path, i.e. a
simplex/direct RF copy — the interesting signal for band-opening spotting.
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import time

log = logging.getLogger("prop")

WINDOW_S = 48 * 3600
BANDS_KM = [10, 25, 50, 100, 250]        # heatmap band edges; +1 open-ended
MAX_REPORTS = 50_000


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _is_direct(report: dict) -> bool:
    return not any("*" in p for p in report.get("path") or [])


def _band_index(km: float) -> int:
    for i, edge in enumerate(BANDS_KM):
        if km <= edge:
            return i
    return len(BANDS_KM)


class PropStore:
    """Rolling APRS packet history + analytics over it."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.reports: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        cutoff = time.time() - WINDOW_S
        kept, total = [], 0
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    total += 1
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("time", 0) >= cutoff:
                        kept.append(r)
        except OSError as e:
            log.warning("history read: %s", e)
            return
        self.reports = kept[-MAX_REPORTS:]
        # compact the file when most of it has aged out of the window
        if total > 2 * len(self.reports) + 100:
            try:
                with self.path.open("w", encoding="utf-8") as f:
                    for r in self.reports:
                        f.write(json.dumps(r) + "\n")
                log.info("history compacted: %d -> %d", total, len(self.reports))
            except OSError as e:
                log.warning("history compact: %s", e)
        log.info("prop history: %d reports in window", len(self.reports))

    def add(self, report: dict) -> None:
        self.reports.append(report)
        cutoff = time.time() - WINDOW_S
        while self.reports and self.reports[0].get("time", 0) < cutoff:
            self.reports.pop(0)
        del self.reports[:-MAX_REPORTS]
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(report) + "\n")
        except OSError as e:
            log.warning("history append: %s", e)

    # ------------------------------------------------------------ analytics

    def analytics(self, home_lat: float | None = None,
                  home_lon: float | None = None) -> dict:
        now = time.time()
        have_home = home_lat is not None and home_lon is not None
        hour0 = int(now // 3600) - 23          # oldest of the 24 buckets
        hours = [{"t": (hour0 + i) * 3600, "n": 0, "direct": 0, "digi": 0,
                  "max_km": 0.0, "bands": [0] * (len(BANDS_KM) + 1)}
                 for i in range(24)]
        best: dict[str, dict] = {}             # per-station farthest fix
        stations_hour: set[str] = set()
        stations_24h: set[str] = set()
        meter = {"direct_hour": 0, "digi_hour": 0, "max_km_hour": 0.0,
                 "base_direct": 0, "base_km": 0.0,
                 "packets_24h": 0}

        for r in self.reports:
            if r.get("dir") == "tx":
                continue
            t = r.get("time", 0)
            idx = int(t // 3600) - hour0
            src = r.get("source") or "?"
            direct = _is_direct(r)
            km = None
            if (have_home and r.get("latitude") is not None
                    and r.get("longitude") is not None):
                km = haversine_km(home_lat, home_lon,
                                  r["latitude"], r["longitude"])
                if km > best.get(src, {}).get("km", -1):
                    best[src] = {
                        "call": src, "km": round(km, 1),
                        "bearing": round(bearing_deg(
                            home_lat, home_lon,
                            r["latitude"], r["longitude"])),
                        "time": t, "direct": direct,
                        "lat": r["latitude"], "lon": r["longitude"],
                    }
            if 0 <= idx < 24:
                h = hours[idx]
                h["n"] += 1
                h["direct" if direct else "digi"] += 1
                if km is not None:
                    h["bands"][_band_index(km)] += 1
                    if km > h["max_km"]:
                        h["max_km"] = round(km, 1)
                stations_24h.add(src)
                meter["packets_24h"] += 1
            if now - t <= 3600:
                stations_hour.add(src)
                meter["direct_hour" if direct else "digi_hour"] += 1
                if km is not None and km > meter["max_km_hour"]:
                    meter["max_km_hour"] = round(km, 1)

        meter["base_direct"] = max((h["direct"] for h in hours), default=0)
        meter["base_km"] = max((h["max_km"] for h in hours), default=0.0)
        meter["stations_hour"] = len(stations_hour)
        meter["stations_24h"] = len(stations_24h)

        leaderboard = sorted(best.values(), key=lambda e: -e["km"])[:10]
        return {
            "home": ({"lat": home_lat, "lon": home_lon} if have_home else None),
            "bands_km": BANDS_KM,
            "hours": hours,
            "leaderboard": leaderboard,
            "meter": meter,
        }

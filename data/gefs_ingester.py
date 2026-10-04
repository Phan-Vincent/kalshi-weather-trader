#!/usr/bin/env python3
"""
data/gefs_ingester.py — NOAA GEFS ensemble via byte-range GRIB2 subsetting.

Downloads t2m for 5 GEFS members (control + 4 ensemble) across
5 forecast hours (f24-f48). Extracts ALL 21 cities from each download.

Concurrent: 30 files in ~9s, 4.2MB total per cycle.
Hour-cached. Zero cost (public S3).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import ssl
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import xarray as xr

ROOT = Path(__file__).resolve().parent.parent

GEFS_BUCKET = "https://noaa-gefs-pds.s3.amazonaws.com"
N_MEMBERS = 5
FORECAST_HOURS = [24, 30, 36, 42, 48]
CONCURRENT_WORKERS = 8
# A local day needs at least this many forecast-hour samples before its per-member max/min is
# trusted as a daily high/low. f24-f48 at 6h spacing leave some local days with only 1-2 samples
# (e.g. a 00Z run puts only evening hours on the near local day); a single evening reading is not a
# real daily high — under-reads the afternoon peak (audit follow-up 2026-06-25). Thin days dropped.
MIN_SAMPLES_PER_DAY = 3

_CACHE: dict = {}
_CACHE_HOUR: str = ""
_TZ_CACHE: dict = {}


def _gefs_disk_cache_path(hour: str) -> Path:
    """Hour-keyed on-disk GEFS cache (hour like '2026-06-29T14')."""
    return ROOT / "cache" / f"gefs-{hour.replace(':', '')}.json"


def _prune_gefs_disk_cache(dirpath: Path, keep: int = 6) -> None:
    """Keep only the most recent `keep` hourly GEFS cache files."""
    try:
        files = sorted(dirpath.glob("gefs-*.json"))
        for f in files[:-keep]:
            f.unlink()
    except Exception:
        pass


def _station_tz(city: str):
    """Cached ZoneInfo for a city's station tz (None if unmapped)."""
    try:
        from .weather_data import STATION_TZ
    except ImportError:
        from data.weather_data import STATION_TZ
    tzname = STATION_TZ.get(city.upper())
    if not tzname:
        return None
    if tzname not in _TZ_CACHE:
        from zoneinfo import ZoneInfo
        _TZ_CACHE[tzname] = ZoneInfo(tzname)
    return _TZ_CACHE[tzname]


def _valid_local_date(run_dt: datetime, fhour: int, city: str) -> str:
    """Station-LOCAL calendar date (ISO) that forecast hour `fhour` (from `run_dt`) is valid on —
    used to bucket GEFS temps by the local day they belong to, so a next-local-day forecast hour
    is NOT folded into today's high/low (audit #1/#3, 2026-06-25 look-ahead leakage)."""
    from datetime import timedelta
    valid_utc = run_dt + timedelta(hours=fhour)
    tz = _station_tz(city)
    return valid_utc.astimezone(tz).date().isoformat() if tz else valid_utc.date().isoformat()


def _aggregate_by_date(member_day_temps, min_samples: int = MIN_SAMPLES_PER_DAY):
    """Per-member daily_high/daily_low for each station-local date, keeping only days whose member
    temp lists are dense enough (>= min_samples forecast hours) to bracket the diurnal peak/trough.
    Pure function over {member: {local_date: [temps]}} -> {local_date: {daily_high, daily_low, count}}
    so the anti-leak bucketing/aggregation is unit-testable without the GRIB download (audit
    2026-06-25): thin 1-2-sample days are dropped so an evening reading can't masquerade as a high."""
    all_dates = set()
    for day_temps in member_day_temps.values():
        all_dates.update(day_temps.keys())
    by_date = {}
    for d in sorted(all_dates):
        highs, lows = [], []
        for day_temps in member_day_temps.values():
            temps = day_temps.get(d)
            if temps and len(temps) >= min_samples:
                highs.append(round(max(temps), 1))
                lows.append(round(min(temps), 1))
        if highs:
            by_date[d] = {'daily_high': highs, 'daily_low': lows, 'count': len(highs)}
    return by_date


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _gefs_run(now: Optional[datetime] = None) -> Tuple[str, str]:
    """Return (date, run_hour) for the most recent FULLY-POSTED GEFS run.
    Probes S3 backwards from the current 6h block and returns the first cycle
    whose data is completely uploaded."""
    if now is None:
        now = datetime.now(timezone.utc)
    from datetime import timedelta

    # Completeness sentinel: probe the LAST file fetch_gefs_ensemble() needs — the highest
    # perturbation member at the highest forecast hour. GEFS posts members/hours
    # progressively, so if this file exists the whole set we download is up. Probing only
    # gec00/f024 (control, mid-range) used to commit us to a half-uploaded 00Z cycle at
    # ~04:00 UTC (00Z fully lands ~05:00-05:30 UTC) → empty download → spurious "0 cities".
    last_member = f"gep{N_MEMBERS - 1:02d}"
    last_fhour = max(FORECAST_HOURS)

    # Try runs from newest backwards: current 6h block, then -6h, -12h, etc
    base_hour = (now.hour // 6) * 6
    for offset in [0, -6, -12, -18, -24]:
        candidate_hour = base_hour + offset
        if candidate_hour < 0:
            candidate_date = (now - timedelta(days=1)).strftime('%Y%m%d')
            candidate_hour += 24
        else:
            candidate_date = now.strftime('%Y%m%d')
        rh = f'{candidate_hour:02d}'

        check_url = (f"{GEFS_BUCKET}/gefs.{candidate_date}/{rh}/atmos/pgrb2ap5/"
                     f"{last_member}.t{rh}z.pgrb2a.0p50.f{last_fhour:03d}.idx")
        try:
            req = urllib.request.Request(check_url, headers={'User-Agent': 'kwb-gefs/1.0'})
            urllib.request.urlopen(req, timeout=5, context=_ctx())
            return candidate_date, rh
        except Exception:
            continue

    return (now - timedelta(days=1)).strftime('%Y%m%d'), '18'


def _download_one(date: str, run: str, member: str, fhour: int) -> Optional[Tuple[str, int, bytes]]:
    """Download one GEFS t2m GRIB2 message. Returns (member, fhour, data) or None."""
    ctx = _ctx()
    path = f"gefs.{date}/{run}/atmos/pgrb2ap5/{member}.t{run}z.pgrb2a.0p50.f{fhour:03d}"
    idx_url = f"{GEFS_BUCKET}/{path}.idx"

    try:
        # Step 1: Download .idx
        req = urllib.request.Request(idx_url, headers={'User-Agent': 'kwb-gefs/1.0'})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            idx_text = r.read().decode('utf-8', errors='replace')

        # Find t2m offset
        offset = 0
        for line in idx_text.split('\n'):
            if 'TMP' in line and '2 m above ground' in line:
                parts = line.strip().split(':')
                if len(parts) >= 2:
                    offset = int(parts[1])
                break
        if not offset:
            return None

        data_url = f"{GEFS_BUCKET}/{path}"

        # Step 2: Get message length
        req2 = urllib.request.Request(data_url, headers={
            'User-Agent': 'kwb-gefs/1.0',
            'Range': f'bytes={offset}-{offset+15}'
        })
        with urllib.request.urlopen(req2, timeout=10, context=ctx) as r:
            header = r.read()
        msg_len = struct.unpack('>Q', b'\x00\x00\x00\x00' + header[12:16])[0]
        if msg_len < 1000:
            return None

        # Step 3: Download full message
        req3 = urllib.request.Request(data_url, headers={
            'User-Agent': 'kwb-gefs/1.0',
            'Range': f'bytes={offset}-{offset+msg_len-1}'
        })
        with urllib.request.urlopen(req3, timeout=15, context=ctx) as r:
            grib_data = r.read()

        return (member, fhour, grib_data)

    except Exception:
        return None


def fetch_gefs_ensemble(
    stations: Optional[Dict] = None,
    force: bool = False,
) -> Dict[str, Dict]:
    """
    Fetch GEFS 5-member ensemble for all cities.

    Returns per-city dict with:
        daily_high: [member0_max, member1_max, ...]  in °F
        daily_low:  [member0_min, member1_min, ...]  in °F

    Concurrent download, hour-cached.
    """
    global _CACHE, _CACHE_HOUR

    current_hour = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')
    if not force and current_hour == _CACHE_HOUR and _CACHE:
        return _CACHE

    # On-disk hour cache: the LIVE and PAPER fair-value builds run as two SEPARATE
    # processes within one cycle, so the in-memory cache can't be shared. Reuse the
    # first process's GEFS result from disk instead of re-downloading (~14s/203MB).
    # Pure memoization, hour-keyed → identical results.
    if not force:
        _disk = _gefs_disk_cache_path(current_hour)
        if _disk.exists():
            try:
                cached = json.loads(_disk.read_text())
                if cached:
                    _CACHE, _CACHE_HOUR = cached, current_hour
                    print(json.dumps({"event": "gefs_disk_cache_hit",
                                      "hour": current_hour, "cities": len(cached)}),
                          file=sys.stderr)
                    return cached
            except Exception:
                pass  # corrupt/partial cache → fall through to a fresh fetch

    if stations is None:
        try:
            from .weather_data import STATIONS  # package import (kalshi_weather.data)
        except ImportError:
            from data.weather_data import STATIONS  # direct-run / cwd fallback
        stations = STATIONS

    date, run = _gefs_run()
    members = ['gec00'] + [f'gep{i:02d}' for i in range(1, N_MEMBERS)]

    # ── Concurrent download ──
    tasks = [(date, run, m, fh) for m in members for fh in FORECAST_HOURS]
    downloaded: Dict[Tuple[str, int], bytes] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as ex:
        futures = {ex.submit(_download_one, d, r, m, fh): (m, fh)
                   for d, r, m, fh in tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    member, fhour, data = result
                    downloaded[(member, fhour)] = data
            except Exception:
                continue

    if not downloaded:
        return {}

    # ── Parse and extract for all cities, bucketing each forecast hour by station-LOCAL day ──
    # fhour fXX has UTC valid time = run + XX h; f24-f48 span ~24h and cross local calendar days,
    # so a market's daily high/low must use ONLY the hours that fall on that local day. The prior
    # code took max/min across ALL hours, folding next-local-day temps into today's high/low =
    # look-ahead leakage (audit #1/#3, 2026-06-25). Mirrors Open-Meteo's _in_local_day in fair_value.
    _run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    temp_files = []
    # city -> member -> local_date_iso -> [temps]
    member_day_temps: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
    city_list = [(city, meta['lat'], meta['lon']) for city, meta in sorted(stations.items())]

    for (member, fhour), grib_data in downloaded.items():
        try:
            tmp = tempfile.NamedTemporaryFile(suffix='.grib2', delete=False)
            tmp.write(grib_data)
            tmp.close()
            temp_files.append(tmp.name)

            ds = xr.open_dataset(tmp.name, engine='cfgrib')
            var_name = list(ds.data_vars)[0]

            for city, lat, lon in city_list:
                try:
                    pt = ds[var_name].sel(latitude=lat, longitude=360+lon, method='nearest')
                    t_f = round(float(pt.values) * 9/5 - 459.67, 1)
                    ld = _valid_local_date(_run_dt, fhour, city)
                    (member_day_temps.setdefault(city, {}).setdefault(member, {})
                        .setdefault(ld, []).append(t_f))
                except Exception:
                    pass
        except Exception:
            continue

    # Cleanup temp files
    for tf in temp_files:
        try:
            os.unlink(tf)
        except Exception:
            pass

    # ── Compute daily max/min per member, PER LOCAL DATE (thin days dropped, see _aggregate_by_date) ──
    results: Dict[str, Dict] = {}
    for city, members_data in member_day_temps.items():
        by_date = _aggregate_by_date(members_data)
        if by_date:
            results[city] = {'by_date': by_date, 'date': date, 'run': run}

    _CACHE = results
    _CACHE_HOUR = current_hour
    if results:  # only persist a real result; never cache a transient 0-city miss
        try:
            _disk = _gefs_disk_cache_path(current_hour)
            _disk.parent.mkdir(parents=True, exist_ok=True)
            tmp = _disk.with_suffix('.tmp')
            tmp.write_text(json.dumps(results))
            tmp.replace(_disk)
            _prune_gefs_disk_cache(_disk.parent)
        except Exception:
            pass
    return results


def get_gefs_daily_high(city: str, date_iso: Optional[str] = None) -> List[float]:
    by_date = fetch_gefs_ensemble().get(city.upper(), {}).get('by_date', {})
    if date_iso and date_iso in by_date:
        return by_date[date_iso].get('daily_high', [])
    if by_date:  # fallback: earliest local day available
        return by_date[sorted(by_date)[0]].get('daily_high', [])
    return []


if __name__ == "__main__":
    t0 = time.time()
    data = fetch_gefs_ensemble()
    t = time.time() - t0
    n = sum(1 for d in data.values() if d.get('by_date'))
    print(f"GEFS: {n} cities in {t:.1f}s")
    for city in ['SEA', 'HOU', 'NYC', 'LAX']:
        bd = data.get(city, {}).get('by_date', {})
        for d in sorted(bd):
            h = bd[d].get('daily_high', [])
            if h:
                print(f"  {city} {d}: {len(h)} members, {min(h):.0f}-{max(h):.0f}°F")

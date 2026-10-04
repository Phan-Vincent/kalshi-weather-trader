#!/usr/bin/env python3
"""Bootstrap climatological priors for Kalshi weather markets.

Fetches 25 years of daily max/min temps from Open-Meteo Archive API
for each station in the Kalshi universe. Saves to state/climatology.json.

Usage:
    python3 bin/bootstrap_climatology.py [--force]
        --force   Re-fetch even if state/climatology.json exists

One-time run: ~2-5 minutes (22 cities × ~4s API call).
Writes ~2MB file. Loads in <0.1s at cycle runtime.
"""

import json
import math
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure kalshi_weather package is importable
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTOMATIONS = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_STATE_DIR = os.path.join(_AUTOMATIONS, "kalshi-weather", "state")
if _AUTOMATIONS not in sys.path:
    sys.path.insert(0, _AUTOMATIONS)

from kalshi_weather.data.weather_data import STATIONS

_OUT_PATH = os.path.join(_STATE_DIR, "climatology.json")

# Window in days for smoothing ECDF (±window days around target date)
SMOOTH_WINDOW = 3

# Open-Meteo Archive API base
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Range: 25 years
START_YEAR = 2000
END_YEAR = 2024


def _c_to_f(c: Optional[float]) -> Optional[float]:
    """Celsius to Fahrenheit."""
    if c is None:
        return None
    return round(c * 9.0 / 5.0 + 32.0, 1)


def _get_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_daily(
    lat: float, lon: float, start: str, end: str
) -> Optional[Dict[str, Any]]:
    """Fetch daily max/min temps from Open-Meteo Archive."""
    # FIX 2026-07-03 (weather-model QA): build the 25-yr climatology on the STATION-LOCAL day
    # (timezone=auto), consistent with Kalshi settlement, the local-day forecast bucketing, and the
    # now-local-day actuals. WARNING: the SHIPPED state/climatology.json was built with the old
    # timezone=UTC, so for western cities its daily means/quantiles are biased (UTC-day extremes ran
    # up to ~12°F hot on some days). This code fix only takes effect on a fresh rebuild — a separate,
    # deliberate, heavy operation (25yr × 21 cities of archive fetches). Rebuild to make the prior +
    # KDE base fully consistent with local-day actuals; until then a bounded western-city bias remains.
    url = (
        f"{ARCHIVE_URL}?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        "&daily=temperature_2m_max,temperature_2m_min"
        "&timezone=auto"
    )
    ctx = _get_ssl_ctx()
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-weather-bernard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [ERROR] API fetch failed: {e}", file=sys.stderr)
        return None


def _build_day_index(raw: Dict[str, Any]) -> Dict[str, List[float]]:
    """Convert API response to dict[MM-DD] = [temp_f, ...] for both max and min.

    Returns:
        {"max": {"01-01": [68.0, 72.1, ...], ...},
         "min": {"01-01": [55.0, 58.2, ...], ...}}
    """
    daily = raw.get("daily", {})
    dates = daily.get("time", [])
    max_c = daily.get("temperature_2m_max", [])
    min_c = daily.get("temperature_2m_min", [])

    max_idx: Dict[str, List[float]] = {}
    min_idx: Dict[str, List[float]] = {}

    for i, date_str in enumerate(dates):
        mmdd = date_str[5:10]  # "2024-06-08" -> "06-08"
        if i < len(max_c) and max_c[i] is not None:
            f = _c_to_f(max_c[i])
            if f is not None:
                max_idx.setdefault(mmdd, []).append(f)
        if i < len(min_c) and min_c[i] is not None:
            f = _c_to_f(min_c[i])
            if f is not None:
                min_idx.setdefault(mmdd, []).append(f)

    return {"max": max_idx, "min": min_idx}


def _compute_stats(temps: List[float]) -> Dict[str, Any]:
    """Compute summary stats from a list of temperature observations."""
    if not temps:
        return {"mean": None, "std": None, "p5": None, "p25": None, "p50": None, "p75": None, "p95": None, "n": 0}

    sorted_t = sorted(temps)
    n = len(sorted_t)
    mean = sum(sorted_t) / n
    variance = sum((t - mean) ** 2 for t in sorted_t) / n
    std = math.sqrt(variance)

    def _percentile(p: float) -> float:
        k = (n - 1) * p / 100.0
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_t[int(k)]
        return sorted_t[int(f)] * (c - k) + sorted_t[int(c)] * (k - f)

    return {
        "mean": round(mean, 2),
        "std": round(std, 2),
        "p5": round(_percentile(5), 1),
        "p25": round(_percentile(25), 1),
        "p50": round(_percentile(50), 1),
        "p75": round(_percentile(75), 1),
        "p95": round(_percentile(95), 1),
        "sorted": sorted_t,  # raw sorted values for ECDF queries
        "n": n,
    }


def _get_windowed_observations(
    day_index: Dict[str, List[float]], mmdd: str, window: int = SMOOTH_WINDOW
) -> List[float]:
    """Get observations for mmdd ± window days, wrapping month boundaries."""
    month, day = int(mmdd[:2]), int(mmdd[3:])
    obs: List[float] = []

    for offset in range(-window, window + 1):
        # Calculate offset day
        d = day + offset
        m = month
        # Simple month wraparound (ignores varying month lengths for simplicity;
        # worst case: Feb 29 or month boundaries 31->1 will miss 1-2 days)
        while d < 1:
            m -= 1
            if m < 1:
                m = 12
            d += 31  # approximate; we catch exact via key lookup
        while d > 31:
            m += 1
            if m > 12:
                m = 1
            d -= 31

        key = f"{m:02d}-{d:02d}"
        if key in day_index:
            obs.extend(day_index[key])

    return obs


def _build_climatology(city_idx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build the full climatology dict keyed by MM-DD.

    Each entry: { "daily_high": {stats}, "daily_low": {stats} }
    """
    max_idx = city_idx.get("max", {})
    min_idx = city_idx.get("min", {})

    # Union of all MM-DD keys present
    all_dates = sorted(set(list(max_idx.keys()) + list(min_idx.keys())))

    climo: Dict[str, Dict[str, Any]] = {}
    for mmdd in all_dates:
        h_obs = _get_windowed_observations(max_idx, mmdd)
        l_obs = _get_windowed_observations(min_idx, mmdd)
        climo[mmdd] = {
            "daily_high": _compute_stats(h_obs),
            "daily_low": _compute_stats(l_obs),
        }
    return climo


def main() -> None:
    force = "--force" in sys.argv

    if os.path.exists(_OUT_PATH) and not force:
        print(f"Climatology exists at {_OUT_PATH}")
        print("  Use --force to re-fetch.")
        return

    os.makedirs(_STATE_DIR, exist_ok=True)
    start_str = f"{START_YEAR}-01-01"
    end_str = f"{END_YEAR}-12-31"

    from datetime import datetime, timezone
    climatology: Dict[str, Any] = {
        "metadata": {
            "source": "Open-Meteo Archive API",
            "start_date": start_str,
            "end_date": end_str,
            "smooth_window_days": SMOOTH_WINDOW,
            # Provenance stamp (2026-07-06): the shipped artifact was silently built
            # timezone=UTC before the 2026-07-03 fix, biasing western-city priors.
            # Loaders check day_boundary to detect a stale UTC-built artifact.
            "day_boundary": "station_local",  # timezone=auto in _fetch_daily
            "built_utc": datetime.now(timezone.utc).isoformat(),
            "cities": {},
        },
        "data": {},
    }

    city_codes = sorted(STATIONS.keys())
    print(f"Fetching 25-year climatology for {len(city_codes)} cities...")
    print()

    for i, code in enumerate(city_codes, 1):
        meta = STATIONS[code]
        print(f"  [{i}/{len(city_codes)}] {code} ({meta['name']}) "
              f"@ {meta['lat']:.3f}, {meta['lon']:.3f} ...", end=" ", flush=True)

        raw = None
        for attempt in range(3):
            raw = _fetch_daily(meta["lat"], meta["lon"], start_str, end_str)
            if raw is not None:
                break
            print(f"RETRY {attempt+1}/3...", end=" ", flush=True)
            time.sleep(10.0 * (attempt + 1))
        if raw is None:
            print("SKIP (fetch failed after 3 retries)")
            continue

        day_index = _build_day_index(raw)
        climo = _build_climatology(day_index)

        # Sample sanity check
        sample_key = "06-08"
        if sample_key in climo:
            h = climo[sample_key]["daily_high"]
            l = climo[sample_key]["daily_low"]
            print(f"OK (Jun 8: high={h['mean']}°F ±{h['std']:.1f}, "
                  f"low={l['mean']}°F ±{l['std']:.1f}, n={h['n']})")
        else:
            print(f"OK ({len(climo)} days)")

        climatology["data"][code] = {
            "station": meta["station"],
            "name": meta["name"],
            "lat": meta["lat"],
            "lon": meta["lon"],
            "days": climo,
        }
        climatology["metadata"]["cities"][code] = meta["name"]

        # Rate limit: 1 req/s is fine for Open-Meteo
        if i < len(city_codes):
            time.sleep(1.0)

    # Summary stats
    total_days = sum(len(c["days"]) for c in climatology["data"].values())
    climatology["metadata"]["total_city_days"] = total_days

    # Atomic write: a live cycle may be reading the artifact concurrently. Write to
    # a temp file in the same dir, then os.replace (atomic on POSIX) so no reader ever
    # sees a torn half-written prior.
    _tmp = _OUT_PATH + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(climatology, f, indent=2)
    os.replace(_tmp, _OUT_PATH)

    file_size_mb = os.path.getsize(_OUT_PATH) / (1024 * 1024)
    print(f"\nDone. {len(city_codes)} cities × ~365 days = ~{total_days} records")
    print(f"File: {_OUT_PATH} ({file_size_mb:.1f} MB)")
    print(f"Ready for model/prior.py to load.")


if __name__ == "__main__":
    main()
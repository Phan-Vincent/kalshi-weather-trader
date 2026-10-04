#!/usr/bin/env python3
"""
model/persistence.py — Short-term persistence prior for weather markets.

Weather has strong autocorrelation: today's temperature is correlated with
yesterday's. This module provides a prior based on recent observed temperatures
from Open-Meteo Archive API, to be blended with the climatological prior.

Persistence prior: P(T > threshold | yesterday = Y)
  = 1 - Φ((threshold - Y) / persistence_std)

Where persistence_std is the day-to-day variance of daily max/min
(typically 4-6°F for daily max, 3-5°F for daily low).

FIX 2026-06-15: added to improve discrimination. Climatology alone
is weak (same prior every June 15, regardless of whether there's a
heat wave). Persistence adds a weather-state-dependent signal.
"""

from __future__ import annotations

import json
import math
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

# ── Persistence std estimates (day-to-day temperature volatility) ───
# These are reasonable defaults based on CONUS climate data.
# Daily max varies more day-to-day than daily min (diurnal cycle).
PERSISTENCE_STD: Dict[str, Dict[str, float]] = {
    "daily_high": {"default": 5.0, "min": 2.0},
    "daily_low":  {"default": 4.0, "min": 1.5},
}

_STATE_DIR = Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"
_CACHE_DIR = _STATE_DIR / "persistence_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def yesterday_local_date(tzname: Optional[str], now_utc: Optional[datetime] = None) -> str:
    """'Yesterday' (YYYY-MM-DD) in the STATION'S local day, not UTC's.

    LEAK-2 fix (quant review 2026-07-01): during the 00-06Z window, UTC has already rolled to
    the next date while US stations are still ON the resolution day — so UTC-"yesterday" is
    the station's CURRENT local day, and "yesterday's observed temp" was actually the
    resolution day's own temp → same-day look-ahead into the persistence prior, OUTSIDE the
    LEAK-1 guard. Falls back to UTC-yesterday when tzname is missing/bad."""
    now = now_utc or datetime.now(timezone.utc)
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(tzname))
        except Exception:
            pass
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_yesterday_temp(
    lat: float, lon: float, mtype: str, date_str: Optional[str] = None
) -> Optional[float]:
    """Fetch the observed max/min temperature for `date_str` (default: UTC-yesterday, the
    legacy behavior) from Open-Meteo Archive. Leak-safe callers pass
    date_str=yesterday_local_date(tzname).

    Returns temperature in °F, or None on failure.
    """
    yesterday = date_str or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Check cache first. The `_loc` suffix versions the cache after the 2026-07-03 UTC→station-local
    # day-boundary fix (below) so stale UTC-day entries are never served.
    cache_key = f"{lat:.2f}_{lon:.2f}_{yesterday}_{mtype}_loc"
    cache_path = _CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                data = json.load(f)
            if data.get("date") == yesterday:
                return data.get("temp_f")
        except (OSError, json.JSONDecodeError):
            pass

    field = "temperature_2m_max" if mtype == "daily_high" else "temperature_2m_min"
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={yesterday}&end_date={yesterday}"
        f"&daily={field}"
        # FIX 2026-07-03 (weather-model QA): station-LOCAL day (timezone=auto), consistent with the
        # LEAK-2 station-local "yesterday" date and Kalshi settlement. UTC-day was off by up to ~12°F
        # for western cities, corrupting the persistence prior that feeds LIVE pricing.
        "&timezone=auto"
    )
    
    ctx = _get_ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-weather-bernard/1.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
        value_c = data.get("daily", {}).get(field, [None])[0]
        if value_c is not None:
            temp_f = round(value_c * 9.0 / 5.0 + 32.0, 1)
            # Cache
            try:
                with open(cache_path, "w") as f:
                    json.dump({"date": yesterday, "temp_f": temp_f}, f)
            except OSError:
                pass
            return temp_f
    except Exception as e:
        print(f"  [warn] persistence fetch failed: {e}", file=sys.stderr)
    
    return None


def persistence_exceedance_prob(
    threshold_f: float,
    yesterday_f: float,
    mtype: str = "daily_high",
) -> float:
    """P(actual > threshold | yesterday = Y).

    Uses Gaussian CDF with persistence_std as the scale.
    """
    std = PERSISTENCE_STD.get(mtype, {}).get("default", 5.0)
    z = (threshold_f - yesterday_f) / std
    # Standard normal survival function: 1 - Φ(z)
    sf = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
    return sf


def persistence_below_prob(
    threshold_f: float,
    yesterday_f: float,
    mtype: str = "daily_high",
) -> float:
    """P(actual < threshold | yesterday = Y)."""
    return 1.0 - persistence_exceedance_prob(threshold_f, yesterday_f, mtype)


def persistence_between_prob(
    low_f: float,
    high_f: float,
    yesterday_f: float,
    mtype: str = "daily_high",
) -> float:
    """P(low < actual <= high | yesterday = Y)."""
    exceed_low = persistence_exceedance_prob(low_f, yesterday_f, mtype)
    exceed_high = persistence_exceedance_prob(high_f, yesterday_f, mtype)
    return exceed_low - exceed_high


def blend_with_climatology(
    climo_prob: Optional[float],
    persistence_prob: Optional[float],
    persistence_weight: float = 0.25,
) -> Optional[float]:
    """Blend climatological prior with persistence prior.

    persistence_weight controls how much recent observations influence
    the prior. 0.25 means 25% persistence, 75% climatology.
    """
    if climo_prob is None and persistence_prob is None:
        return None
    if climo_prob is None:
        return persistence_prob
    if persistence_prob is None:
        return climo_prob
    
    return persistence_weight * persistence_prob + (1.0 - persistence_weight) * climo_prob


def persistence_weight(hours_to_close: Optional[float] = None) -> float:
    """Weight for persistence prior vs climatology.

    Persistence matters most at short horizons (1-2 days) and decays
    as the forecast lead time increases. Beyond 5 days, persistence
    adds negligible signal.

    Returns weight ∈ [0, 1].
    """
    if hours_to_close is None:
        return 0.15
    # Clamp h >= 0: a past/closed market (negative hours_to_close) otherwise flips
    # the exponent positive and the weight DIVERGES (e.g. h=-336 -> ~2813), which
    # blows up the persistence/climatology blend in fair_value (observed fair_prob
    # ~600). A stale market is treated as h=0 (maximum persistence), and the result
    # is hard-bounded to the intended [0.02, 0.25] range.
    h = max(hours_to_close, 0.0)
    # Exponential decay: weight = 0.25 * exp(-h / 36)
    # ~0.25 at 0h, ~0.13 at 24h, ~0.07 at 48h, ~0.03 at 72h
    weight = 0.25 * math.exp(-h / 36.0)
    return round(min(0.25, max(weight, 0.02)), 4)


# ── Smoke test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Persistence Prior Smoke Test ===\n")
    
    # Test probability computation
    print("P(T>100°F | yesterday=95°F, daily_high):")
    p = persistence_exceedance_prob(100.0, 95.0, "daily_high")
    print(f"  = {p:.4f} (should be ~0.16)")
    
    print("\nP(T>100°F | yesterday=105°F, daily_high):")
    p = persistence_exceedance_prob(100.0, 105.0, "daily_high")
    print(f"  = {p:.4f} (should be ~0.84)")
    
    print("\nP(T>100°F | yesterday=100°F, daily_high):")
    p = persistence_exceedance_prob(100.0, 100.0, "daily_high")
    print(f"  = {p:.4f} (should be ~0.50)")
    
    print("\nPersistence weights vs horizon:")
    for h in [0, 12, 24, 36, 48, 72, 96, 120]:
        w = persistence_weight(h)
        print(f"  {h:>3d}h: w={w:.4f}")
    
    # Try fetching real data
    print("\nFetching yesterday's PHX high temp...")
    temp = fetch_yesterday_temp(33.45, -112.07, "daily_high")
    if temp:
        print(f"  PHX yesterday max: {temp}°F")
        print(f"  P(T>105°F | yesterday): {persistence_exceedance_prob(105.0, temp, 'daily_high'):.4f}")
    else:
        print("  Fetch failed (expected if no internet or API down)")
    
    print("\nDone.")

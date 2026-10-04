#!/usr/bin/env python3
"""
data/historical_forecast.py — real historical forecasts for backtesting.

Open-Meteo's ENSEMBLE api does not serve past dates (members come back null), but
the HISTORICAL-FORECAST api archives the real deterministic forecast and carries
genuine forecast error (measured mean |forecast-actual| ≈ 1.3°F, matching the
~1.6°F we see forward in the 12-24h band — i.e. NOT analysis-perfect, so it's a
fair test). The effective lead is short (~12-18h, the trading sweet spot).

Returns the forecast daily high/low (the model's "loc"). The backtest scores the
KDE-shifted-to-forecast distribution (the production fix) against real obs — exact
ensemble spread isn't needed because the KDE uses climatological spread + the
shrink knob. Disk-cached so tuning sweeps are instant.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "state" / "backtest-cache"
CACHE.mkdir(parents=True, exist_ok=True)

_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def _ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, timeout: int = 30) -> Optional[dict]:
    import time as _time
    import urllib.error
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=_ctx()) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Open-Meteo rate-limit (429): honor Retry-After, else exponential backoff (2/4/8s).
            if e.code == 429 and attempt < 3:
                # Retry-After may be delay-seconds OR an HTTP-date (RFC 7231); int() on a date raises
                # ValueError that escapes the loop and crashes the backtest — parse defensively.
                try:
                    _ra = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    _ra = 0
                wait = _ra or (2 ** (attempt + 1))
                _time.sleep(min(wait, 30))
                continue
            return None
        except Exception:
            return None
    return None


def fetch_historical_forecast(lat: float, lon: float, date_iso: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (forecast_high_f, forecast_low_f) for date_iso — the archived real
    forecast (short lead ~12-18h). Cached. (None, None) on failure."""
    # `fcL_` prefix versions the cache after the 2026-07-03 UTC→station-local day-boundary fix below.
    key = CACHE / f"fcL_{lat:.3f}_{lon:.3f}_{date_iso}.json"
    if key.exists():
        try:
            d = json.load(open(key))
            return d.get("hi"), d.get("lo")
        except Exception:
            pass
    # FIX 2026-07-03 (weather-model QA): station-LOCAL day (timezone=auto) so the backtest scores on
    # the same day boundary production settles/forecasts on (UTC-day was off up to ~12°F for the west).
    url = (f"{_URL}?latitude={lat}&longitude={lon}"
           f"&start_date={date_iso}&end_date={date_iso}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit=fahrenheit&timezone=auto")
    d = _get(url)
    if not d or "daily" not in d:
        return None, None
    try:
        hi = d["daily"]["temperature_2m_max"][0]
        lo = d["daily"]["temperature_2m_min"][0]
    except (KeyError, IndexError):
        return None, None
    try:
        json.dump({"hi": hi, "lo": lo}, open(key, "w"))
    except Exception:
        pass
    return hi, lo


if __name__ == "__main__":
    hi, lo = fetch_historical_forecast(25.79, -80.29, "2026-06-15")
    print(f"MIA 2026-06-15 forecast: high={hi}F low={lo}F")

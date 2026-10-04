#!/usr/bin/env python3
"""Same-day forecast look-ahead guard (LEAK-1, 2026-06-29 audit).

The forecast path took max/min over the resolving local day's Open-Meteo/NWS hours;
for a SAME-DAY market those hours are already ELAPSED (≈observed), so the "forecast"
read the answer. estimate_market_prob now routes same-day/past markets through the
climatology-only path when USE_FORECAST is on (source -> prior_only, no ensemble
temp in the record). Future-dated markets are unaffected. Override for research:
KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST=1. Runs under plain python3."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.fair_value import estimate_market_prob, _local_today_str  # noqa: E402

FUTURE_CLOSE = "2099-01-01T00:00:00+00:00"


def _market(date_iso):
    return {
        "ticker": f"KXTEST-{date_iso}-B75", "market_type": "daily_high",
        "city_code": "ZZZ",  # unknown city -> tz=None -> deterministic UTC-day bucketing
        "date_iso": date_iso, "bin_kind": "above", "threshold_f": 75.0,
        "bin_low": None, "bin_high": None,
        "_raw_market": {"close_time": FUTURE_CLOSE},
    }


def _om(date_iso):
    hours = [f"{date_iso}T{h:02d}:00:00+00:00" for h in range(0, 24, 3)]
    temps = [70.0, 73.0, 77.0, 82.0, 79.0, 75.0, 72.0, 70.0]  # peak 82 at 09:00Z
    return {"model_temps_f": {"om": temps}, "utc_hours": hours, "spreads_f": [1.0] * 8}


def _run(date_iso, env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return estimate_market_prob(_market(date_iso), forecast_om=_om(date_iso))
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_sameday_blocked_routes_to_climatology():
    today = _local_today_str(None)
    r = _run(today, {"KALSHI_WEATHER_USE_FORECAST": "1"})
    assert r["source"] == "prior_only", r["source"]
    assert r["model_temp_forecast"] is None, r["model_temp_forecast"]
    assert "leak-guard" in r["rationale"], r["rationale"]


def test_future_market_still_uses_forecast():
    import datetime as _dt
    future = (_dt.date.fromisoformat(_local_today_str(None)) + _dt.timedelta(days=5)).isoformat()
    r = _run(future, {"KALSHI_WEATHER_USE_FORECAST": "1"})
    assert r["source"] == "ensemble", r["source"]            # forecast path used, not blocked
    assert r["model_temp_forecast"] is not None, r           # an ensemble temp was produced
    assert "leak-guard" not in r["rationale"], r["rationale"]


def test_override_re_enables_sameday_forecast():
    today = _local_today_str(None)
    r = _run(today, {"KALSHI_WEATHER_USE_FORECAST": "1",
                     "KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST": "1"})
    assert r["source"] == "ensemble", r["source"]


if __name__ == "__main__":
    test_sameday_blocked_routes_to_climatology()
    test_future_market_still_uses_forecast()
    test_override_re_enables_sameday_forecast()
    print("ok")

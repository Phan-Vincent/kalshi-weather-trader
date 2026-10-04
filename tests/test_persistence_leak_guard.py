#!/usr/bin/env python3
"""LEAK-2 regression tests (quant review 2026-07-01): the persistence prior must never blend the
market day's OWN observed temperature into fair_prob.

Two failure modes are pinned:
1. Date math: during 00-06Z, UTC-"yesterday" IS the station's current local day for US stations —
   `yesterday_local_date` must return the STATION-LOCAL yesterday instead.
2. The fair_value belt-and-braces guard: if the resolved "yesterday" is >= the market's date_iso
   (same-day/past-dated market on any tz edge), persistence must be skipped entirely.

No network: the fetch is either cache-served (tmp dir) or stubbed via sys.modules.
"""
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.persistence import yesterday_local_date, fetch_yesterday_temp  # noqa: E402
import model.persistence as persistence_mod                               # noqa: E402


# ── 1. Station-local yesterday across the 00-06Z boundary ─────────────────────
def test_yesterday_local_date_00_06z_window():
    # 02Z on Jul 2: UTC date has rolled to Jul 2, but LA is still on Jul 1 evening.
    now = datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc)
    assert yesterday_local_date("America/Los_Angeles", now) == "2026-06-30"
    assert yesterday_local_date("America/Phoenix", now) == "2026-06-30"
    assert yesterday_local_date("America/New_York", now) == "2026-06-30"
    # The OLD (leaky) behavior: UTC fallback returns Jul 1 — the resolution day itself.
    assert yesterday_local_date(None, now) == "2026-07-01"


def test_yesterday_local_date_midday_agrees_with_utc():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)   # LA local Jul 2 05:00
    assert yesterday_local_date("America/Los_Angeles", now) == "2026-07-01"
    assert yesterday_local_date(None, now) == "2026-07-01"


def test_yesterday_local_date_bad_tz_falls_back_to_utc():
    now = datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc)
    assert yesterday_local_date("Not/AZone", now) == "2026-07-01"


# ── 2. fetch_yesterday_temp honors the explicit date (cache-key check, no network) ──
def test_fetch_uses_explicit_date_str(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence_mod, "_CACHE_DIR", tmp_path)
    lat, lon, date = 33.94, -118.41, "2026-06-30"
    # `_loc` cache-key version marker (2026-07-03 UTC→station-local actuals fix).
    cache = tmp_path / f"{lat:.2f}_{lon:.2f}_{date}_daily_high_loc.json"
    cache.write_text(json.dumps({"date": date, "temp_f": 77.0}))
    assert fetch_yesterday_temp(lat, lon, "daily_high", date_str=date) == 77.0


# ── 3. fair_value guard: same-day yesterday ⇒ persistence skipped ─────────────
def _stub_kw(monkeypatch, ydate, calls):
    """Install stub kalshi_weather.* modules so fair_value's in-function imports hit them."""
    def _fetch(lat, lon, mtype, date_str=None):
        calls.append(("fetch", date_str))
        return 95.0
    pers = types.ModuleType("kalshi_weather.model.persistence")
    pers.yesterday_local_date = lambda tzname, now_utc=None: ydate
    pers.fetch_yesterday_temp = _fetch
    pers.persistence_exceedance_prob = lambda *a, **k: (calls.append(("prob",)) or 0.9)
    pers.persistence_below_prob = lambda *a, **k: 0.9
    pers.persistence_between_prob = lambda *a, **k: 0.9
    pers.persistence_weight = lambda h: 0.25
    wd = types.ModuleType("kalshi_weather.data.weather_data")
    wd.STATIONS = {"LAX": {"lat": 33.94, "lon": -118.41}}
    pkg = types.ModuleType("kalshi_weather"); pkg.__path__ = []
    mdl = types.ModuleType("kalshi_weather.model"); mdl.__path__ = []
    dat = types.ModuleType("kalshi_weather.data"); dat.__path__ = []
    for name, m in [("kalshi_weather", pkg), ("kalshi_weather.model", mdl),
                    ("kalshi_weather.model.persistence", pers),
                    ("kalshi_weather.data", dat), ("kalshi_weather.data.weather_data", wd)]:
        monkeypatch.setitem(sys.modules, name, m)


def _mkt(date_iso):
    return {"ticker": f"KXHIGHLAX-TEST-T85", "market_type": "daily_high", "city_code": "LAX",
            "date_iso": date_iso, "bin_kind": "above", "threshold_f": 85.0,
            "bin_low": None, "bin_high": None}


def test_fair_value_skips_persistence_when_yesterday_is_market_day(monkeypatch):
    from model.fair_value import estimate_market_prob
    calls = []
    _stub_kw(monkeypatch, ydate="2099-07-15", calls=calls)      # "yesterday" == market day
    out = estimate_market_prob(_mkt("2099-07-15"))
    assert out.get("fair_prob") is not None
    assert ("fetch", "2099-07-15") not in calls
    assert not any(c[0] == "fetch" for c in calls), f"guard failed — fetch called: {calls}"


def test_fair_value_applies_persistence_with_prior_day(monkeypatch):
    from model.fair_value import estimate_market_prob
    calls = []
    _stub_kw(monkeypatch, ydate="2099-07-14", calls=calls)      # genuine prior-day observation
    out = estimate_market_prob(_mkt("2099-07-15"))
    assert out.get("fair_prob") is not None
    assert ("fetch", "2099-07-14") in calls, f"expected fetch with local-yesterday, got {calls}"


# ── 4. Persistence A/B gate (experiment #8): KALSHI_WEATHER_USE_PERSISTENCE=0 disables it ──
def test_fair_value_persistence_gate_off(monkeypatch):
    from model.fair_value import estimate_market_prob
    calls = []
    _stub_kw(monkeypatch, ydate="2099-07-14", calls=calls)      # genuine prior day — would normally fetch
    monkeypatch.setenv("KALSHI_WEATHER_USE_PERSISTENCE", "0")
    out = estimate_market_prob(_mkt("2099-07-15"))
    assert out.get("fair_prob") is not None                     # still produces a (climo-only) fair_prob
    assert not any(c[0] == "fetch" for c in calls), f"gate off but persistence ran: {calls}"


def test_fair_value_persistence_gate_default_on(monkeypatch):
    from model.fair_value import estimate_market_prob
    calls = []
    _stub_kw(monkeypatch, ydate="2099-07-14", calls=calls)
    monkeypatch.delenv("KALSHI_WEATHER_USE_PERSISTENCE", raising=False)   # unset ⇒ default "1"
    estimate_market_prob(_mkt("2099-07-15"))
    assert ("fetch", "2099-07-14") in calls, "default should keep persistence ON"

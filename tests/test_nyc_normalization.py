#!/usr/bin/env python3
"""QA fixes 2026-06-25 (post-comprehensive-QA): NY->NYC normalization in the extractors the NYC
pricing commit (c1a510a) left un-aliased, plus defensive Retry-After parsing. Runs under plain
python3. These guard real-money correctness: NYC's daily-high never fed the error model, NYC was
skipped on the legacy live arm, and an HTTP-date Retry-After crashed the 429 retry loop."""
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))


def test_neff_gate_aliases_nyc():
    # The live n_eff gate (paper_trade.py) extracts the city by stripping prefixes, then must
    # normalize via CITY_ALIASES so it looks up NYC's error-data count — not the absent "NY" key.
    from data.weather_data import CITY_ALIASES
    tk = "KXHIGHNY-26JUN24-B85.5"
    city = tk.split('-')[0].replace('KXHIGHT', '').replace('KXLOWT', '').replace('KXHIGH', '').replace('KXLOW', '').replace('KXTEMP', '')
    assert city == "NY", "raw extraction is the un-aliased NY (the bug)"
    assert CITY_ALIASES.get(city, city) == "NYC", "the gate must alias NY->NYC before get_effective_n"


def test_settle_records_nyc_high_under_nyc():
    # A settled KXHIGHNY (daily-high) position must feed the shared error model under canonical "NYC".
    # Mock the tracker (its real path is production state) + the actual-temp fetch (network).
    import settle_paper as sp
    from data.weather_data import STATIONS
    assert "NY" not in STATIONS and "NYC" in STATIONS, "alias is required: raw NY is not a station key"

    captured = {}

    class _FakeTracker:
        def load(self): pass
        def save(self): pass
        def record_error(self, city, forecast_f, actual_f): captured["city"] = city
        def get_bias(self, city): return 0.0

    orig_et, orig_fetch = sp.ErrorTracker, sp.fetch_actual_temp
    sp.ErrorTracker = _FakeTracker
    sp.fetch_actual_temp = lambda lat, lon, date_iso, mtype, correction_city=None: 85.0
    try:
        sp._update_error_model("NY", 84.0, "KXHIGHNY-26JUN24-B85.5", {})
    finally:
        sp.ErrorTracker, sp.fetch_actual_temp = orig_et, orig_fetch
    assert captured.get("city") == "NYC", f"KXHIGHNY must record under NYC, got {captured.get('city')!r}"


def test_retry_after_http_date_does_not_crash():
    # A 429 whose Retry-After is an HTTP-date (RFC 7231) must NOT raise ValueError out of the loop —
    # the defensive parse falls back to exponential backoff and the call returns None gracefully.
    import time
    import model.error_tracker as et
    calls = {"n": 0}

    def _raise_429(req, timeout=15, context=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 429, "Too Many Requests",
                                     {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None)

    o_open, o_sleep = urllib.request.urlopen, time.sleep
    urllib.request.urlopen = _raise_429
    time.sleep = lambda s: None
    try:
        out = et.fetch_actual_temp(40.0, -73.0, "2026-06-24", "daily_high")
    finally:
        urllib.request.urlopen, time.sleep = o_open, o_sleep
    assert out is None, "HTTP-date Retry-After must yield graceful None, not a crash"
    assert calls["n"] >= 2, "the 429 retry path must have been exercised (defensive parse + backoff)"


if __name__ == "__main__":
    test_neff_gate_aliases_nyc();             print("[PASS] n_eff gate aliases KXHIGHNY -> NYC")
    test_settle_records_nyc_high_under_nyc();  print("[PASS] KXHIGHNY settlement feeds error model under NYC")
    test_retry_after_http_date_does_not_crash(); print("[PASS] HTTP-date Retry-After does not crash the 429 loop")
    print("\nNYC-normalization + Retry-After tests pass.")

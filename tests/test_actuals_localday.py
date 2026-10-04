#!/usr/bin/env python3
"""Regression: actual-temperature fetches aggregate over the STATION-LOCAL day, not the UTC day.

Weather-model QA 2026-07-03 (fix #4). fetch_actual_temp / persistence.fetch_yesterday_temp requested
Open-Meteo archive daily extremes with timezone=UTC. Kalshi settles the local-day high/low and the
forecast path buckets by local day, so the UTC-day extreme was the wrong quantity — for western
(Pacific/Mountain) cities the UTC calendar day folds in the previous local day's afternoon (measured
~12°F wrong for Seattle), corrupting the bias EWMA, paper settlement, persistence prior, and every
skill/Brier/CRPS score. Fixed by timezone=auto (Open-Meteo resolves the tz from lat/lon).

These tests intercept the request URL (no network) and assert timezone=auto. Runs under python3/pytest.
"""
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model import error_tracker, persistence  # noqa: E402


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        # Celsius daily payload; value irrelevant to the URL assertion.
        return json.dumps({"daily": {"temperature_2m_max": [20.0],
                                     "temperature_2m_min": [10.0]}}).encode()


def _capture_url(call):
    """Run `call` with urllib.request.urlopen stubbed; return the requested URL."""
    captured = {}
    real = urllib.request.urlopen

    def _fake(req, *a, **k):
        captured["url"] = req.full_url if hasattr(req, "full_url") else req
        return _FakeResp()

    urllib.request.urlopen = _fake
    try:
        call()
    finally:
        urllib.request.urlopen = real
    return captured.get("url", "")


def test_fetch_actual_temp_uses_local_day():
    url = _capture_url(lambda: error_tracker.fetch_actual_temp(47.44, -122.31, "2026-06-20", "daily_high"))
    assert "timezone=auto" in url, url
    assert "timezone=UTC" not in url, url


def test_fetch_yesterday_temp_uses_local_day():
    # Point persistence's cache at an empty temp dir so it actually builds the request.
    saved = persistence._CACHE_DIR
    persistence._CACHE_DIR = Path(tempfile.mkdtemp())
    try:
        url = _capture_url(lambda: persistence.fetch_yesterday_temp(
            47.44, -122.31, "daily_high", date_str="2026-06-20"))
    finally:
        persistence._CACHE_DIR = saved
    assert "timezone=auto" in url, url
    assert "timezone=UTC" not in url, url


def test_persistence_cache_key_is_versioned():
    # The cache key must carry the _loc version marker so pre-fix UTC-day entries are never served.
    saved_dir = persistence._CACHE_DIR
    tmp = Path(tempfile.mkdtemp())
    persistence._CACHE_DIR = tmp
    try:
        _capture_url(lambda: persistence.fetch_yesterday_temp(1.0, 2.0, "daily_high", date_str="2026-06-20"))
        written = list(tmp.glob("*.json"))
        assert written and written[0].name.endswith("_loc.json"), [p.name for p in written]
    finally:
        persistence._CACHE_DIR = saved_dir


if __name__ == "__main__":
    test_fetch_actual_temp_uses_local_day()
    test_fetch_yesterday_temp_uses_local_day()
    test_persistence_cache_key_is_versioned()
    print("OK — actual-temp fetches use station-local day (timezone=auto) + versioned caches")

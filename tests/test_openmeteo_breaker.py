"""Open-Meteo negative cache + circuit breaker (data/weather_data.py).

WHY: _http_json costs ~94s per dead URL (3x30s timeouts + backoff); the fair-value
build calls fetch_openmeteo per TICKER and run-cycle.sh runs 3 builds. On 2026-07-02
and again 2026-07-03 an Open-Meteo outage turned that into a 46min-2.5h cycle stall
that held .cycle.lock and starved LIVE trading slots. These tests pin the two guards
that make an outage cost O(breaker_N) fetch attempts instead of O(tickers x builds):
a per-(lat,lon)-per-hour negative cache and a consecutive-failure circuit breaker
with an hour-keyed cross-process disk marker.

No network I/O: _http_json is monkeypatched in every test.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import weather_data as wd  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_om_state(monkeypatch, tmp_path):
    """Fresh breaker/cache state per test, disk marker under tmp (never the repo cache/)."""
    monkeypatch.setattr(wd, "_OM_CACHE", {})
    monkeypatch.setattr(wd, "_OM_CACHE_HOUR", "")
    monkeypatch.setattr(wd, "_OM_FAIL_CACHE", {})
    monkeypatch.setattr(wd, "_OM_CONSEC_FAILS", 0)
    monkeypatch.setattr(wd, "_OM_BREAKER_DIR", tmp_path / "cache")
    yield


class _CountingRaiser:
    def __init__(self, exc=None):
        self.calls = 0
        self.exc = exc

    def __call__(self, url, *a, **kw):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"hourly": {"time": ["2026-07-03T00:00"], "temperature_2m": [70.0]}}


def test_negative_cache_skips_second_attempt_same_station(monkeypatch):
    http = _CountingRaiser(OSError("handshake timed out"))
    monkeypatch.setattr(wd, "_http_json", http)
    with pytest.raises(OSError):
        wd.fetch_openmeteo(39.856, -104.673)
    assert http.calls == 1
    # Same station again this hour: no network attempt at all.
    with pytest.raises(wd.OpenMeteoSkipped):
        wd.fetch_openmeteo(39.856, -104.673)
    assert http.calls == 1


def test_breaker_opens_after_n_distinct_failures_and_writes_marker(monkeypatch, tmp_path):
    http = _CountingRaiser(OSError("handshake timed out"))
    monkeypatch.setattr(wd, "_http_json", http)
    coords = [(30.0, -90.0), (31.0, -91.0), (32.0, -92.0)]
    for lat, lon in coords:
        with pytest.raises(OSError):
            wd.fetch_openmeteo(lat, lon)
    assert http.calls == len(coords)
    # 4th, previously-unseen station: breaker is open, no network attempt.
    with pytest.raises(wd.OpenMeteoSkipped):
        wd.fetch_openmeteo(45.0, -120.0)
    assert http.calls == len(coords)
    markers = list((tmp_path / "cache").glob("om-breaker-*.json"))
    assert len(markers) == 1


def test_disk_marker_trips_breaker_in_fresh_process(monkeypatch):
    """A marker written by an earlier build this hour opens the breaker immediately."""
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    wd._OM_BREAKER_DIR.mkdir(parents=True, exist_ok=True)
    (wd._OM_BREAKER_DIR / f"om-breaker-{hour}.json").write_text("{}")
    http = _CountingRaiser(OSError("should never be called"))
    monkeypatch.setattr(wd, "_http_json", http)
    with pytest.raises(wd.OpenMeteoSkipped):
        wd.fetch_openmeteo(30.0, -90.0)
    assert http.calls == 0


def test_success_resets_consecutive_failures(monkeypatch):
    fail = _CountingRaiser(OSError("down"))
    monkeypatch.setattr(wd, "_http_json", fail)
    for lat in (30.0, 31.0):  # 2 failures — one short of the default breaker N=3
        with pytest.raises(OSError):
            wd.fetch_openmeteo(lat, -90.0)
    ok = _CountingRaiser()
    monkeypatch.setattr(wd, "_http_json", ok)
    result = wd.fetch_openmeteo(32.0, -92.0)
    assert result["source"] == "open_meteo_ensemble"
    assert wd._OM_CONSEC_FAILS == 0


def test_failure_in_model_loop_also_counts(monkeypatch):
    """The per-model fetches inside a partially-successful call still record failure."""
    state = {"calls": 0}

    def flaky(url, *a, **kw):
        state["calls"] += 1
        if state["calls"] == 1:  # combined call succeeds
            return {"hourly": {"time": ["2026-07-03T00:00"], "temperature_2m": [70.0]}}
        raise OSError("model endpoint down")

    monkeypatch.setattr(wd, "_http_json", flaky)
    with pytest.raises(OSError):
        wd.fetch_openmeteo(30.0, -90.0)
    assert wd._OM_CONSEC_FAILS == 1
    # And the station is negative-cached: retry this hour is skipped.
    with pytest.raises(wd.OpenMeteoSkipped):
        wd.fetch_openmeteo(30.0, -90.0)
    assert state["calls"] == 2  # no further network attempts

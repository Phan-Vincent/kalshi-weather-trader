"""Open-Meteo TIME-BUDGET breaker trip (data/weather_data.py, 2026-07-08).

The consecutive-failure breaker only fires on 3 failures. A DEGRADED (slow-but-succeeding)
Open-Meteo instead accumulates a 30-47min fair-value build across ~20 cities x 8-9 models,
holding .cycle.lock (root cause of the 09:20 PDT live-slot starvation — no station failed 3x
in a row). These tests pin the time-based trip: once cumulative SUCCESSFUL-fetch wall-clock
this hour crosses the budget, the breaker opens (same hour-keyed marker) and the rest of the
hour's fetches skip. No network I/O.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import weather_data as wd  # noqa: E402

HOUR = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "_OM_CACHE", {})              # else a prior test's fetch is cache-hit
    monkeypatch.setattr(wd, "_OM_CACHE_HOUR", "")
    monkeypatch.setattr(wd, "_OM_FAIL_CACHE", {})
    monkeypatch.setattr(wd, "_OM_CONSEC_FAILS", 0)
    monkeypatch.setattr(wd, "_OM_TIME_SPENT_SEC", 0.0)
    monkeypatch.setattr(wd, "_OM_TIME_HOUR", "")
    monkeypatch.setattr(wd, "_OM_TIME_BUDGET_SEC", 10.0)          # small budget for the test
    monkeypatch.setattr(wd, "_OM_BREAKER_DIR", tmp_path / "cache")
    yield


def _markers(tmp_path):
    return list((tmp_path / "cache").glob("om-breaker-*.json"))


def test_cumulative_time_trips_breaker(tmp_path):
    wd._om_note_elapsed(HOUR, 6.0)                                # 6 < 10 → no trip yet
    assert wd._om_breaker_open(HOUR) is False
    assert _markers(tmp_path) == []
    wd._om_note_elapsed(HOUR, 6.0)                                # cumulative 12 ≥ 10 → TRIP
    assert wd._om_breaker_open(HOUR) is True
    assert wd._OM_CONSEC_FAILS == wd._OM_BREAKER_N                # this process now skips too
    marks = _markers(tmp_path)
    assert len(marks) == 1
    import json
    assert json.loads(marks[0].read_text())["reason"] == "time_budget_exceeded"


def test_under_budget_never_trips(tmp_path):
    for _ in range(5):
        wd._om_note_elapsed(HOUR, 1.0)                           # 5s total < 10s
    assert wd._om_breaker_open(HOUR) is False
    assert _markers(tmp_path) == []


def test_new_hour_resets_accumulator(tmp_path):
    wd._om_note_elapsed(HOUR, 8.0)                               # 8 < 10
    wd._om_note_elapsed("2026-01-01T00", 8.0)                    # different hour → reset to 8, not 16
    assert wd._om_breaker_open("2026-01-01T00") is False
    assert _markers(tmp_path) == []


def test_budget_zero_disables_time_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "_OM_TIME_BUDGET_SEC", 0.0)
    wd._om_note_elapsed(HOUR, 9999.0)
    assert wd._om_breaker_open(HOUR) is False
    assert _markers(tmp_path) == []


def test_fetch_openmeteo_charges_time_on_success(monkeypatch):
    """Wiring: a successful fetch_openmeteo charges its wall-clock to the time budget exactly once."""
    monkeypatch.setattr(wd, "_http_json",
                        lambda url, *a, **kw: {"hourly": {"time": ["2026-07-08T00:00"], "temperature_2m": [70.0]}})
    seen = []
    monkeypatch.setattr(wd, "_om_note_elapsed", lambda hour, elapsed: seen.append((hour, elapsed)))
    res = wd.fetch_openmeteo(30.0, -90.0)
    assert res["source"] == "open_meteo_ensemble"
    assert len(seen) == 1 and seen[0][0] == HOUR and seen[0][1] >= 0.0


def test_end_to_end_slow_success_trips_and_survives_the_success_reset(monkeypatch):
    """End-to-end through the REAL _om_note_elapsed: a slow-but-SUCCEEDING fetch charges > budget and
    trips the breaker — and the trip must survive fetch_openmeteo's `_OM_CONSEC_FAILS = 0` success
    reset at L526 (the marker + re-armed count persist for the hour)."""
    monkeypatch.setattr(wd, "_http_json",
                        lambda url, *a, **kw: {"hourly": {"time": ["2026-07-08T00:00"], "temperature_2m": [70.0]}})
    monkeypatch.setattr(wd, "_OM_TIME_BUDGET_SEC", 5.0)
    # deterministic clock: _t0 reads 0.0; the post-fetch charge reads 10.0 → elapsed 10 > 5 → trip
    n = {"i": 0}

    def _mono():
        n["i"] += 1
        return 0.0 if n["i"] == 1 else 10.0

    monkeypatch.setattr(wd.time, "monotonic", _mono)
    res = wd.fetch_openmeteo(30.0, -90.0)
    assert res["source"] == "open_meteo_ensemble"          # the fetch itself SUCCEEDED (reset ran)…
    assert wd._om_breaker_open(HOUR) is True                # …yet the time-trip survived the reset
    assert wd._OM_CONSEC_FAILS == wd._OM_BREAKER_N

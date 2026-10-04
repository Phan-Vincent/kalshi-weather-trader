#!/usr/bin/env python3
"""
tests/test_postmortem_basis.py — Drawdown fix #3 (2026-07-13).

Two fixes in bin/postmortem.py:
  A. Mislabel (unconditional): the forecast_too_warm lesson hardcoded "daily-high", so a
     daily_LOW market (SEA/PHIL lows) that classified forecast_too_warm was reported as a
     daily-high. The label is now derived from market_type.
  B. Station-basis (gated, default OFF): Kalshi settles on the official NWS station but the
     postmortem scored the raw Open-Meteo grid. When KALSHI_WEATHER_POSTMORTEM_STATION_BASIS=1,
     the learned per-city grid→station offset is applied (same one settle_paper already uses).
     Default OFF means the observed value — which flows to LESSONS.md→live side-bias — is
     unchanged, so landing this code cannot splice the futility stream.

Run: python3 -m pytest tests/test_postmortem_basis.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bin.postmortem as postmortem
from model import error_tracker


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def read(self):
        return json.dumps(self._p).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# ── A. mislabel fix (unconditional) ──────────────────────────────────

def test_daily_low_lesson_not_labeled_daily_high():
    loss = {
        "ticker": "KXLOWTSEA-26JUL04-T60",
        "side": "yes",
        "fair_prob_at_open": 0.72,
        "entry_cents": 40,
        "pnl_cents": -300,
        "settlement_result": "no",
        "market_parsed": {
            "market_type": "daily_low", "bin_kind": "above", "threshold_f": 60.0,
            "bin_low": None, "bin_high": None, "city_code": "SEA", "date_iso": "2026-07-04",
        },
    }
    observed = {"low_f": 55.0, "high_f": 70.0, "source": "open-meteo-archive"}
    c = postmortem._classify_failure(loss, observed)
    assert "forecast_too_warm" in c["failure_mode"], c["failure_mode"]
    assert "daily-low" in c["lesson"], c["lesson"]
    assert "daily-high" not in c["lesson"], c["lesson"]


def test_daily_high_lesson_still_labeled_daily_high():
    loss = {
        "ticker": "KXHIGHTATL-26JUL04-T90",
        "side": "yes",
        "fair_prob_at_open": 0.72,
        "entry_cents": 40,
        "pnl_cents": -300,
        "settlement_result": "no",
        "market_parsed": {
            "market_type": "daily_high", "bin_kind": "above", "threshold_f": 90.0,
            "bin_low": None, "bin_high": None, "city_code": "ATL", "date_iso": "2026-07-04",
        },
    }
    observed = {"low_f": 70.0, "high_f": 85.0, "source": "open-meteo-archive"}
    c = postmortem._classify_failure(loss, observed)
    assert "forecast_too_warm" in c["failure_mode"]
    assert "daily-high" in c["lesson"]


# ── B. station-basis correction (gated) ──────────────────────────────

def _seed_corrections(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "actual-corrections.json").write_text(json.dumps({
        "corrections": {"SEA": {"daily_low": {"correction_f": 1.8, "n_events": 6}}}
    }))
    monkeypatch.setenv("KALSHI_WEATHER_STATE_DIR", str(state))
    error_tracker._CORRECTIONS_CACHE["mtime"] = None   # force reload of the freshly-written file
    monkeypatch.setattr(postmortem, "STATIONS", {"SEA": {"lat": 47.6, "lon": -122.3}})
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResp({"daily": {"temperature_2m_max": [70.0],
                                             "temperature_2m_min": [55.0]}}),
    )


def test_station_basis_on_applies_correction(tmp_path, monkeypatch):
    _seed_corrections(tmp_path, monkeypatch)
    monkeypatch.setattr(postmortem, "_STATION_BASIS", True)
    out = postmortem._fetch_observed("SEA", "2026-07-04")
    assert abs(out["low_f"] - (55.0 + 1.8)) < 1e-9        # station-corrected
    assert abs(out["high_f"] - 70.0) < 1e-9               # no daily_high offset → unchanged
    assert out["source"] == "open-meteo-archive+station-corr"


def test_station_basis_off_is_raw_grid(tmp_path, monkeypatch):
    _seed_corrections(tmp_path, monkeypatch)
    monkeypatch.setattr(postmortem, "_STATION_BASIS", False)
    out = postmortem._fetch_observed("SEA", "2026-07-04")
    assert abs(out["low_f"] - 55.0) < 1e-9                # raw grid, default behavior
    assert out["source"] == "open-meteo-archive"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

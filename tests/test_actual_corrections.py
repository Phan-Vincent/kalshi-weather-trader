#!/usr/bin/env python3
"""
tests/test_actual_corrections.py — per-city grid→station actual correction (audit 2026-07-06 #7).

Model scoring read the Open-Meteo grid actual, ~1.5°F colder than Kalshi's settlement station.
learn_actual_corrections.py writes a per-city offset; fetch_actual_temp adds it when a
correction_city is passed (and returns the RAW grid otherwise, so reconcile/learn don't feed
back on themselves). These tests pin both.

Run: python3 -m pytest tests/test_actual_corrections.py
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import model.error_tracker as et


def _write_corrections(tmp_path, monkeypatch):
    doc = {"corrections": {"HOU": {"daily_low": {"correction_f": 1.8, "n_events": 6}},
                           "NOLA": {"daily_high": {"correction_f": -0.7, "n_events": 5}}}}
    (tmp_path / "actual-corrections.json").write_text(json.dumps(doc))
    monkeypatch.setenv("KALSHI_WEATHER_STATE_DIR", str(tmp_path))
    et._CORRECTIONS_CACHE["mtime"] = None  # force reload


def test_correction_lookup(tmp_path, monkeypatch):
    _write_corrections(tmp_path, monkeypatch)
    assert et._actual_correction_f(None, "daily_low") == 0.0          # no city → raw
    assert et._actual_correction_f("HOU", "daily_low") == 1.8
    assert et._actual_correction_f("hou", "daily_low") == 1.8         # case-insensitive
    assert et._actual_correction_f("HOU", "daily_high") == 0.0        # metric with no correction
    assert et._actual_correction_f("SEA", "daily_low") == 0.0         # city with no correction


def _mock_grid(monkeypatch, celsius):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"daily": {
            "temperature_2m_max": [celsius], "temperature_2m_min": [celsius]}}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())


def test_fetch_applies_correction_only_with_city(tmp_path, monkeypatch):
    _write_corrections(tmp_path, monkeypatch)
    _mock_grid(monkeypatch, celsius=20.0)          # 20C = 68.0F raw grid
    raw = et.fetch_actual_temp(29.6, -95.3, "2026-07-04", "daily_low")
    corrected = et.fetch_actual_temp(29.6, -95.3, "2026-07-04", "daily_low", correction_city="HOU")
    assert raw == 68.0, "no correction_city → raw grid"
    assert corrected == 69.8, "correction_city=HOU adds +1.8F"


def test_missing_corrections_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_STATE_DIR", str(tmp_path))  # no file written
    et._CORRECTIONS_CACHE["mtime"] = None
    _mock_grid(monkeypatch, celsius=20.0)
    assert et.fetch_actual_temp(29.6, -95.3, "2026-07-04", "daily_low", correction_city="HOU") == 68.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

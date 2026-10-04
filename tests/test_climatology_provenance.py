#!/usr/bin/env python3
"""
tests/test_climatology_provenance.py — Regression for the 2026-07-06 audit finding
that the shipped state/climatology.json predated the 2026-07-03 timezone fix (built
timezone=UTC) while everyone believed the fix was live. The prior had no provenance
field, so nothing could detect the staleness.

The rebuild now stamps metadata.day_boundary="station_local", and Climatology.load()
alerts if that stamp is missing/wrong. These tests pin both directions.

Run: python3 -m pytest tests/test_climatology_provenance.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model.prior import Climatology
import trader.notify as notify


def _write_climo(tmp: Path, day_boundary) -> Path:
    meta = {"source": "test", "start_date": "2000-01-01", "end_date": "2024-12-31"}
    if day_boundary is not None:
        meta["day_boundary"] = day_boundary
    doc = {
        "metadata": meta,
        "data": {
            "HOU": {"station": "KHOU", "name": "Houston", "lat": 1.0, "lon": 2.0,
                    "days": {"06-08": {"daily_high": {"mean": 90.0, "std": 3.0, "n": 175},
                                       "daily_low": {"mean": 74.0, "std": 3.0, "n": 175}}}}
        },
    }
    p = tmp / "climatology.json"
    p.write_text(json.dumps(doc))
    return p


def _capture_alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "alert", lambda *a, **k: calls.append((a, k)) or True)
    return calls


def test_station_local_artifact_does_not_alert(monkeypatch, capsys):
    calls = _capture_alerts(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        p = _write_climo(Path(d), "station_local")
        Climatology().load(p)
    assert not calls, "station_local artifact should not raise a staleness alert"
    assert "day_boundary" not in capsys.readouterr().err


def test_utc_artifact_alerts(monkeypatch, capsys):
    calls = _capture_alerts(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        p = _write_climo(Path(d), "utc")
        Climatology().load(p)
    assert calls, "UTC-built artifact must raise a staleness alert"
    assert calls[0][1].get("key") == "climatology_stale_tz"


def test_unstamped_artifact_alerts(monkeypatch):
    """A legacy artifact with no day_boundary field (the exact shipped state) must alert."""
    calls = _capture_alerts(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        p = _write_climo(Path(d), None)
        c = Climatology()
        c.load(p)
    assert calls, "unstamped (legacy) artifact must raise a staleness alert"
    # Still loads despite the warning — must never break the pricing path.
    assert c.is_loaded() and c.has_city("HOU")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

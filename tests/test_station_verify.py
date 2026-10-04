#!/usr/bin/env python3
"""
tests/test_station_verify.py — Pin the station-identity verdict logic (audit 2026-07-06).

bin/verify_stations.py found that STATIONS["DAL"] was KDAL/Love Field while Kalshi settles
the DAL series on CLIDFW/Dallas-Fort Worth (KDFW) — two airports ~15mi apart. A loose
name-token match false-passed it (both contain "dallas"); the CLI product code is the
authoritative discriminator (CLIDFW ≠ our KDAL). These tests pin that discriminator and the
now-fixed DAL station.

Run: python3 -m pytest tests/test_station_verify.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import verify_stations as vs
from data.weather_data import STATIONS


def test_cli_code_is_authoritative_over_name():
    # The exact Love-vs-DFW case that slipped through a name match:
    assert vs._station_verdict("KDAL", "Dallas Love Field", "Dallas/Fort Worth, TX", "DFW") == "MISMATCH"
    # Correct station passes:
    assert vs._station_verdict("KDFW", "Dallas-Fort Worth", "Dallas/Fort Worth, TX", "DFW") == "ok"
    # Matching CLI codes pass regardless of cosmetic name differences:
    assert vs._station_verdict("KHOU", "Houston Hobby", "Houston-Hobby, TX", "HOU") == "ok"
    assert vs._station_verdict("KMSY", "New Orleans Louis Armstrong", "New Orleans, LA", "MSY") == "ok"


def test_name_fallback_only_when_no_cli():
    # No CLI in rules → fall back to loose name-token match.
    assert vs._station_verdict("KSEA", "Seattle Tacoma", "Seattle-Tacoma, WA", None) == "ok"
    assert vs._station_verdict("KDFW", "Dallas-Fort Worth", "Chicago, IL", None) == "MISMATCH"


def test_dal_station_now_points_at_dfw():
    # The fix itself: STATIONS must anchor DAL on the settlement airport.
    assert STATIONS["DAL"]["station"] == "KDFW"
    assert abs(STATIONS["DAL"]["lon"] - (-97.0403)) < 1e-6


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

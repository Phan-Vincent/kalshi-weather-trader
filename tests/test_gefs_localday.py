#!/usr/bin/env python3
"""Phase A audit fixes (2026-06-25):
  (1) #1/#3 — GEFS forecast hours must bucket by station-LOCAL day, so a next-local-day hour
      can't leak into today's high/low (was max/min across f24-f48 spanning days = look-ahead).
  (2) #7 — error_tracker EWMA variance must re-center on the shifted mean (Finch).
Runs under plain python3."""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "data"))

from data.gefs_ingester import _valid_local_date  # noqa: E402


def test_gefs_hours_bucket_to_distinct_local_days():
    run = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)  # 00Z run
    # SFO = America/Los_Angeles (UTC-7 in summer):
    #   f24 -> valid 00Z 6/26 -> 17:00 PT 6/25 -> local 2026-06-25
    #   f48 -> valid 00Z 6/27 -> 17:00 PT 6/26 -> local 2026-06-26
    d24 = _valid_local_date(run, 24, "SFO")
    d48 = _valid_local_date(run, 48, "SFO")
    assert d24 == "2026-06-25", d24
    assert d48 == "2026-06-26", d48
    assert d24 != d48, "f24 and f48 must NOT fold into the same local day (the leak)"


def test_gefs_local_date_uses_station_tz_not_utc():
    run = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
    # f30 -> valid 06Z 6/26; UTC date is 6/26, but PT local is 23:00 6/25 -> local 2026-06-25.
    assert _valid_local_date(run, 30, "SFO") == "2026-06-25"


def test_ewma_variance_recenters_finch():
    # Exercise the REAL ErrorTracker.record_error (not a re-implementation), so a regression in the
    # production formula at error_tracker.py:146 is actually caught. Fresh city, error=5 (forecast
    # 10, actual 15): old_var=4.0, alpha=0.3 → new_var=(1-0.3)*(4.0+0.3*25)=8.05 → std=2.84. The
    # buggy drop-the-recenter form would store sqrt(6.475)=2.54.
    from model.error_tracker import ErrorTracker
    t = ErrorTracker()
    t._data = {}  # isolate from on-disk state; record_error mutates memory only (no save)
    t.record_error("QATESTCITY", forecast_f=10.0, actual_f=15.0)
    std = t._data["QATESTCITY"]["error_std_f"]
    assert std == 2.84, f"expected Finch std 2.84, got {std} (2.54 would be the buggy form)"


def test_gefs_aggregate_excludes_other_day_and_thin_samples():
    # Locks the actual anti-leak aggregation (not just the date-key helper): a hot NEXT-local-day
    # reading must never enter today's daily_high, and a thin (1-2 sample) day must be dropped so an
    # evening reading can't masquerade as the daily high.
    from data.gefs_ingester import _aggregate_by_date
    member_day_temps = {"m0": {"2026-06-25": [70.0, 68.0, 71.0], "2026-06-26": [95.0, 94.0, 96.0]}}
    by = _aggregate_by_date(member_day_temps, min_samples=3)
    assert by["2026-06-25"]["daily_high"] == [71.0], by  # the 95-96 of the next day must NOT leak in
    assert by["2026-06-26"]["daily_high"] == [96.0], by
    thin = {"m0": {"2026-06-25": [62.0], "2026-06-26": [70.0, 71.0, 72.0]}}
    by2 = _aggregate_by_date(thin, min_samples=3)
    assert "2026-06-25" not in by2, "1-sample day must be dropped (no real daily extreme)"
    assert by2["2026-06-26"]["daily_high"] == [72.0], by2


if __name__ == "__main__":
    test_gefs_hours_bucket_to_distinct_local_days(); print("[PASS] GEFS hours bucket to distinct local days")
    test_gefs_local_date_uses_station_tz_not_utc();  print("[PASS] GEFS local date uses station tz, not UTC")
    test_gefs_aggregate_excludes_other_day_and_thin_samples(); print("[PASS] GEFS aggregation excludes other-day + thin samples")
    test_ewma_variance_recenters_finch();            print("[PASS] EWMA variance via real ErrorTracker (std=2.84)")
    print("\nAll GEFS-localday + variance tests pass.")

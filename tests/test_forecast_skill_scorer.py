#!/usr/bin/env python3
"""Unit tests for the rewritten bin/validate_forecast_skill.py scorer (weather-model QA 2026-07-03).

Pins the four defects the rewrite fixes: current model only (mode=forecast), one forecast per
EVENT (dedup — no pseudo-replication), a 4σ-from-climatology outlier guard (kills the corrupt
loc=90 daily-low record), and leak-free filtering (pre-window + same-day station-local). Pure
functions, no network / no real climatology. Runs under plain python3 and pytest.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import validate_forecast_skill as vfs  # noqa: E402


class _FakeClimo:
    """daily_high mean 90, daily_low mean 75, interdecile spread 6 (σ≈2.34) for every city/day."""
    def get_climo_mean(self, city, mmdd, mtype):
        return 90.0 if mtype == "daily_high" else 75.0

    def get_climo_spread(self, city, mmdd, mtype):
        return 6.0


CUTOFF = datetime.fromisoformat("2026-07-01T00:00:00+00:00")
TODAY = "2026-07-10"  # target days 07-02..07-04 are all settled


def _row(ticker, asof, loc, mode="forecast"):
    return {"ticker": ticker, "asof_utc": asof, "loc": loc, "mode": mode, "bin_kind": "above"}


def _collect(rows, actuals):
    def actual_of(city, mtype, date_iso):
        return actuals.get((city, mtype, date_iso))
    return vfs.collect(rows, actual_of, _FakeClimo(), CUTOFF, TODAY)


def test_dedup_and_leak_and_outlier_filters():
    rows = [
        # Event A (HOU daily_high 07-04): two leak-free snapshots → keep the LAST (loc 92).
        _row("KXHIGHHOU-26JUL04-T90", "2026-07-03T12:00:00+00:00", 91.0),
        _row("KXHIGHHOU-26JUL04-T90", "2026-07-03T20:00:00+00:00", 92.0),
        # Corrupt: HOU daily_low loc=90 is ~15°F (>4σ) above the climo low mean → outlier drop.
        _row("KXLOWTHOU-26JUL03-B75.5", "2026-07-02T20:00:00+00:00", 90.0),
        # Same-day leak: asof 07-02 12:00Z = 07:00 CDT, local date == target 07-02 → dropped.
        _row("KXHIGHHOU-26JUL02-T88", "2026-07-02T12:00:00+00:00", 89.0),
        # Pre-window: asof before the cutoff → dropped.
        _row("KXHIGHHOU-26JUL03-T90", "2026-06-30T00:00:00+00:00", 90.0),
        # Legacy mode: not the current model → skipped entirely.
        _row("KXHIGHHOU-26JUL04-T90", "2026-07-03T21:00:00+00:00", 99.0, mode="legacy"),
    ]
    actuals = {
        ("HOU", "daily_high", "2026-07-04"): 93.0,
        ("HOU", "daily_low", "2026-07-03"): 76.0,
        ("HOU", "daily_high", "2026-07-02"): 90.0,
    }
    events, by_bucket, dropped = _collect(rows, actuals)

    assert len(events) == 1, events                     # only Event A survives all filters
    city, mtype, date_iso, loc, actual, cmean, lead_h, bucket = events[0]
    assert (city, mtype, date_iso) == ("HOU", "daily_high", "2026-07-04")
    assert loc == 92.0, "dedup must keep the LAST leak-free forecast, not the first/legacy"
    assert actual == 93.0 and cmean == 90.0
    assert dropped.get("outlier_loc") == 1
    assert dropped.get("same_day_leak") == 1
    assert dropped.get("pre_window") == 1
    # Event A's eve forecast (9h before the Chicago-local target-day start) buckets as 0-1d.
    assert bucket == "0-1d", (lead_h, bucket)


def test_awaiting_actual_is_not_scored():
    rows = [_row("KXHIGHHOU-26JUL04-T90", "2026-07-03T20:00:00+00:00", 92.0)]
    events, _, dropped = _collect(rows, {})  # no actual available
    assert events == []
    assert dropped.get("awaiting_actual") == 1


def test_stats_skill_math():
    # forecast error 1°F everywhere, climo error 4°F everywhere → skill = 1 - 1/4 = 0.75.
    S = [("X", "daily_high", "2026-07-04", 90.0, 91.0, 87.0, 10.0, "0-1d"),
         ("Y", "daily_high", "2026-07-04", 80.0, 81.0, 77.0, 10.0, "0-1d")]
    st = vfs._stats(S)
    assert abs(st["f_rmse"] - 1.0) < 1e-9 and abs(st["c_rmse"] - 4.0) < 1e-9
    assert abs(st["skill"] - 0.75) < 1e-9
    assert vfs._stats([]) is None


def test_bucket_boundaries():
    assert vfs._bucket(9.0) == "0-1d"
    assert vfs._bucket(30.0) == "1-2d"
    assert vfs._bucket(60.0) == "2-3d"
    assert vfs._bucket(100.0) == "3d+"
    assert vfs._bucket(-1.0) == "unknown"
    assert vfs._bucket(None) == "unknown"


def test_bootstrap_ci_detects_real_gain():
    # forecast strictly better on every event → CI lower bound must exclude 0.
    S = [("c%d" % i, "daily_high", "2026-07-04", 90.0, 91.0, 96.0, 10.0, "0-1d") for i in range(20)]
    pt, lo, hi = vfs._bootstrap_mse_gain_ci(S, iters=2000)
    assert pt > 0 and lo > 0, (pt, lo, hi)


if __name__ == "__main__":
    test_dedup_and_leak_and_outlier_filters()
    test_awaiting_actual_is_not_scored()
    test_stats_skill_math()
    test_bucket_boundaries()
    test_bootstrap_ci_detects_real_gain()
    print("OK — forecast-skill scorer: dedup, outlier guard, leak filters, skill math, CI")

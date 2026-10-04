#!/usr/bin/env python3
"""
tests/test_reconcile_actuals.py — Unit tests for bin/reconcile_actuals.py (audit
2026-07-06). The tool replays settled markets, buckets OUR grid actual with the model's
outcome() convention, and flags where it disagrees with Kalshi's settlement — quantifying
the grid-reanalysis vs station-sensor divergence that model scoring silently ignores.

We pre-seed the fetch cache so these are offline/deterministic.

Run: python3 -m pytest tests/test_reconcile_actuals.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import reconcile_actuals as ra

# HOU exists in STATIONS. Use an "above" T-market: parse gives threshold_f = strike+0.5.
CITY = "HOU"


def _row(strike_ticker, settlement, grid, bin_kind="above", thr=None, lo=None, hi=None):
    mp = {"city_code": CITY, "date_iso": "2026-07-04", "market_type": "daily_high",
          "bin_kind": bin_kind, "threshold_f": thr, "bin_low": lo, "bin_high": hi}
    return {"ticker": strike_ticker, "settlement_result": settlement, "market_parsed": mp}, grid


def test_match_when_grid_agrees_with_kalshi():
    row, grid = _row("KXHIGHTHOU-26JUL04-T93", "yes", grid=95.0, bin_kind="above", thr=93.5)
    cache = {(CITY, "2026-07-04", "daily_high"): grid}
    r = ra.reconcile_row(row, cache)
    assert r and r["match"] is True and r["our_yes"] == 1 and r["kalshi_yes"] == 1


def test_mismatch_and_bounds_grid_error():
    # Kalshi settled YES (actual > 93.5) but our grid says 91 → we'd score NO. Mismatch,
    # and the grid is at least |91 - 93.5| = 2.5°F too cold vs the station.
    row, grid = _row("KXHIGHTHOU-26JUL04-T93", "yes", grid=91.0, bin_kind="above", thr=93.5)
    cache = {(CITY, "2026-07-04", "daily_high"): grid}
    r = ra.reconcile_row(row, cache)
    assert r and r["match"] is False
    assert r["min_grid_vs_station_err"] == 2.5


def test_hourly_and_missing_grid_are_skipped():
    row, _ = _row("KXTEMPNYCH-26JUL0416-T82", "yes", grid=80.0)
    row["market_parsed"]["market_type"] = "hourly_temp"
    assert ra.reconcile_row(row, {}) is None
    # missing grid actual → skip (not a crash)
    row2, _ = _row("KXHIGHTHOU-26JUL04-T93", "yes", grid=None, thr=93.5)
    assert ra.reconcile_row(row2, {(CITY, "2026-07-04", "daily_high"): None}) is None


def test_analyze_aggregates_by_city(monkeypatch, tmp_path):
    # Two HOU mismatches + one match → 2/3 mismatch rate.
    import json
    rows = [
        {"ticker": "KXHIGHTHOU-26JUL04-T93", "settlement_result": "yes",
         "market_parsed": {"city_code": "HOU", "date_iso": "2026-07-04", "market_type": "daily_high",
                           "bin_kind": "above", "threshold_f": 93.5, "bin_low": None, "bin_high": None}},
    ]
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(ra, "fetch_actual_temp", lambda *a, **k: 90.0)  # cold grid → NO, mismatch
    a = ra.analyze(sd)
    assert a["n_evaluated"] == 1 and a["n_mismatch"] == 1
    assert a["by_city"][0]["city"] == "HOU"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

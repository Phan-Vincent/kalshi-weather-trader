#!/usr/bin/env python3
"""Tests for bin/rain_firewall_backtest.py — leak-free firewall backtest pure logic (no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import rain_firewall_backtest as fb


def test_prior_month():
    assert fb.prior_month(2026, 1) == (2025, 12)
    assert fb.prior_month(2026, 7) == (2026, 6)


def test_prob_above_and_shift():
    totals = [1.0, 2.0, 3.0, 4.0]
    assert fb.prob_above(2.5, totals) == 0.5           # {3,4} exceed
    assert fb.prob_above(2.5, totals, shift=1.0) == 0.75   # {2,3,4}+1 exceed 2.5
    assert fb.prob_above(2.5, []) == 0.5               # empty → base rate


def test_loo_predict_recovers_linear_signal():
    # anom = 2*prior_anom exactly → LOO regression should predict ~2*prior_anom for the held-out row
    rows = [(2.0 * p, p, 0.0) for p in [-2, -1, -0.5, 0.5, 1, 1.5, 2, -1.5]]
    pred = fb.loo_predict_anomaly(rows, 0)   # held out prior_anom=-2 → expect ~-4
    assert abs(pred - (-4.0)) < 0.3, pred


def test_loo_predict_degenerate_returns_zero():
    assert fb.loo_predict_anomaly([(1.0, 0.0, 0.0)], 0) == 0.0   # too few rows → climatology (0)


def test_verdict_branches():
    # firewall CI clearly positive → predictability found
    pos = [(f"E{i}", 0.05) for i in range(40)]
    assert fb._verdict(pos, 0.2).startswith("PREDICTABILITY FOUND")
    # firewall ~0 + R²≤0 → kill-supporting
    null = [(f"E{i}", 0.0001 * ((i % 3) - 1)) for i in range(40)]
    assert fb._verdict(null, -0.02).startswith("KILL-SUPPORTING")


def test_seas_last_map_covers_all_seasons():
    assert len(fb._SEAS_LAST) == 12 and fb._SEAS_LAST["JJA"] == 8 and fb._SEAS_LAST["NDJ"] == 13


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    sys.exit(0 if not failed else 1)

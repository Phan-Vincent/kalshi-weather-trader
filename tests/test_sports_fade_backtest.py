#!/usr/bin/env python3
"""Tests for bin/sports_fade_backtest.py — retail longshot-fade backtest pure logic (no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import sports_fade_backtest as f


def test_fees():
    assert f.taker_fee_cents(0.50) == 2.0
    assert f.taker_fee_cents(0.10) == 1.0            # ceil(0.63)
    assert f.maker_fee_cents(0.50) == 0.5            # 25% of taker
    assert f.maker_fee_cents(0.10) == 0.25


def test_fade_pnl():
    # sell YES at 10c; longshot MISSES (outcome 0) → keep ~10c minus maker fee
    assert abs(f.fade_pnl_cents(0.10, 0) - (10.0 - 0.25)) < 1e-9
    # longshot HITS (outcome 1) → pay $1, lose ~90c (the adverse-selection tail)
    assert abs(f.fade_pnl_cents(0.10, 1) - (-90.0 - 0.25)) < 1e-9


def test_fade_ev_sign_matches_calibration():
    # if a 20c longshot realizes YES 10% of the time (overpriced), fading is +EV
    ev = 0.90 * f.fade_pnl_cents(0.20, 0) + 0.10 * f.fade_pnl_cents(0.20, 1)
    assert ev > 0, ev
    # if it realizes at its price (fair), fading is ≤0 after fees
    ev_fair = 0.80 * f.fade_pnl_cents(0.20, 0) + 0.20 * f.fade_pnl_cents(0.20, 1)
    assert ev_fair < 0, ev_fair


def test_bucket():
    assert f._bucket(0.07) == "5-10c"
    assert f._bucket(0.00) == "0-5c" and f._bucket(0.29) == "25-30c"
    assert f._bucket(0.30) is None and f._bucket(0.5) is None


def test_verdict_branches():
    pos = [(f"M{i}", 3.0) for i in range(40)]
    assert f._verdict(pos, 3.0).startswith("EDGE FOUND")
    neg = [(f"M{i}", -5.0) for i in range(40)]
    assert f._verdict(neg, -5.0).startswith("DEAD (worse")
    null = [(f"M{i}", 0.01 * ((i % 3) - 1)) for i in range(40)]
    assert f._verdict(null, 0.0).startswith("DEAD (within noise")


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

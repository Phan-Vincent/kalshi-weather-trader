#!/usr/bin/env python3
"""Tests for the gated live size ramp (risk.live_size_rung).

Promotes only on PROOF (>= RUNG_MIN_FILLS fills at the rung AND positive P&L since
rung start); demotes one rung on a drawdown worse than RUNG_STOP_DOLLARS. State
persists in risk-state.json. P&L is measured from a weather-only balance-equivalent
supplied by the caller (see test_weather_total_signal.py); this module tests the
diff-based promote/demote logic with arbitrary totals, so it is agnostic to the source.

Runs under plain `python3 tests/test_size_ramp.py` (no pytest). Never sends alerts
(KALSHI_WEATHER_ALERTS=0).
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

os.environ["KALSHI_WEATHER_ALERTS"] = "0"
os.environ["KALSHI_WEATHER_SIZE_LADDER"] = "5,10,25"
os.environ["KALSHI_WEATHER_RUNG_MIN_FILLS"] = "10"
os.environ["KALSHI_WEATHER_RUNG_STOP_DOLLARS"] = "5"  # $5 demote threshold

from trader.risk import RiskGate  # noqa: E402


def _rg(d):
    return RiskGate(_state_dir=Path(d))


def test_seed_then_no_promote_without_fills():
    d = tempfile.mkdtemp()
    rg = _rg(d)
    # First call seeds the baseline (balance 100_00c, 0 fills) → bottom rung $5, no jump.
    assert rg.live_size_rung(0, 10000) == 5
    # A few fills + profit but < min_fills → stays at $5.
    assert rg.live_size_rung(5, 10500) == 5
    assert rg._rung_idx == 0


def test_promote_only_on_fills_and_positive_pnl():
    d = tempfile.mkdtemp()
    rg = _rg(d)
    rg.live_size_rung(0, 10000)  # seed
    # enough fills but P&L not positive → NO promote
    assert rg.live_size_rung(10, 10000) == 5
    # enough fills AND positive P&L → promote to $10
    assert rg.live_size_rung(10, 10500) == 10
    assert rg._rung_idx == 1


def test_demote_on_drawdown():
    d = tempfile.mkdtemp()
    rg = _rg(d)
    rg.live_size_rung(0, 10000)            # seed, rung 0
    rg.live_size_rung(10, 10500)           # promote to rung 1 ($10), baseline now bal=10500
    # drawdown worse than -$5 since rung start → demote back to $5
    assert rg.live_size_rung(12, 9900) == 5   # pnl_since = 9900-10500 = -600 <= -500
    assert rg._rung_idx == 0


def test_persists_across_reload_and_caps_at_top():
    d = tempfile.mkdtemp()
    rg = _rg(d)
    rg.live_size_rung(0, 10000)
    rg.live_size_rung(10, 10500)           # → $10 (rung 1)
    # reload: rung persisted
    rg2 = _rg(d)
    assert rg2._rung_idx == 1
    # promote again to top ($25, rung 2)
    assert rg2.live_size_rung(20, 11000) == 25
    # further proof cannot exceed the top of the ladder
    assert rg2.live_size_rung(40, 12000) == 25
    assert rg2._rung_idx == 2


if __name__ == "__main__":
    test_seed_then_no_promote_without_fills()
    print("[PASS] seed → $5; no promote without enough fills")
    test_promote_only_on_fills_and_positive_pnl()
    print("[PASS] promote only on fills AND positive P&L")
    test_demote_on_drawdown()
    print("[PASS] demote one rung on drawdown")
    test_persists_across_reload_and_caps_at_top()
    print("[PASS] rung persists across reload; caps at ladder top")
    print("\nAll size-ramp tests pass.")

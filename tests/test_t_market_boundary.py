#!/usr/bin/env python3
"""Regression: T-market strike maps to the ±0.5 continuous SETTLEMENT boundary (QA fix #5).

Kalshi weather T-markets settle the INTEGER extreme one step past the strike (confirmed against the
live API for high AND low series):
  ">S°"  pays at S+1  (subtitle "(S+1)° or above")  → YES iff continuous >= S+0.5
  "<S°"  pays at S-1  (subtitle "(S-1)° or below")  → YES iff continuous <  S-0.5
So the continuous decision boundary is strike ± 0.5, not the raw integer. Both pricing (exceedance/
KDE/gaussian read threshold_f) and Brier scoring (shadow_score.outcome `actual > thr` on the continuous
Open-Meteo actual) read threshold_f, so the raw integer over-counted ~0.5°F of mass on the priced side
of every T-market (~5-7¢). B-markets already carry ±1.0 half-integer edges and are untouched.

Runs under plain python3 and pytest.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import parse_market_ticker  # noqa: E402
from shadow_score import outcome  # noqa: E402


def _p(ticker, title):
    return parse_market_ticker(ticker, {"title": title})


def test_t_above_uses_settlement_boundary():
    m = _p("KXHIGHNY-26JUL04-T105", "Will the high temp in NYC be >105° on Jul 4, 2026?")
    assert m["bin_kind"] == "above"
    assert m["threshold_f"] == 105.5          # >105 pays at 106 => continuous >= 105.5
    assert m["bin_low"] == 105.5 and m["bin_high"] == float("inf")


def test_t_below_uses_settlement_boundary():
    m = _p("KXHIGHNY-26JUL04-T98", "Will the high temp in NYC be <98° on Jul 4, 2026?")
    assert m["bin_kind"] == "below"
    assert m["threshold_f"] == 97.5           # <98 pays at 97 => continuous < 97.5
    assert m["bin_high"] == 97.5 and m["bin_low"] == float("-inf")


def test_low_t_market_same_convention():
    m = _p("KXLOWTNYC-26JUL04-T82", "Will the minimum temperature be >82° on Jul 4, 2026?")
    assert m["bin_kind"] == "above" and m["threshold_f"] == 82.5


def test_direction_inferred_from_raw_strike_not_shifted():
    # The "<98°" title must still infer BELOW — proving direction inference ran on the raw strike (98)
    # BEFORE the shift. If it ran on the shifted 97.5 it would search "<97.5°", miss, and misclassify.
    m = _p("KXHIGHNY-26JUL04-T98", "Will the high temp in NYC be <98° on Jul 4, 2026?")
    assert m["bin_kind"] == "below"


def test_b_market_unchanged():
    m = _p("KXHIGHNY-26JUL04-B95.5", "high temp between")
    assert m["bin_kind"] == "between"
    assert m["threshold_f"] == 95.5
    assert m["bin_low"] == 94.5 and m["bin_high"] == 96.5


def test_scoring_agrees_with_settlement_at_boundary():
    # T-above 105 (boundary 105.5): a continuous actual of 105.3 rounds to 105 → settles NO.
    assert outcome("above", 105.5, None, None, 105.3) == 0
    assert outcome("above", 105.5, None, None, 105.7) == 1
    # T-below 98 (boundary 97.5): actual 97.7 rounds to 98 → high is NOT < 98 → NO.
    assert outcome("below", 97.5, None, None, 97.7) == 0
    assert outcome("below", 97.5, None, None, 97.3) == 1
    # The pre-fix raw-integer threshold would have mis-scored the near-boundary case:
    assert outcome("above", 105.0, None, None, 105.3) == 1  # wrong-YES that the fix removes


if __name__ == "__main__":
    for fn in [test_t_above_uses_settlement_boundary, test_t_below_uses_settlement_boundary,
               test_low_t_market_same_convention, test_direction_inferred_from_raw_strike_not_shifted,
               test_b_market_unchanged, test_scoring_agrees_with_settlement_at_boundary]:
        fn()
    print("OK — T-market strike maps to the ±0.5 settlement boundary (pricing + scoring); B unchanged")

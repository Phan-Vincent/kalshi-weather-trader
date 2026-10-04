#!/usr/bin/env python3
"""Tests for bin/rain_screen_snapshot.py — ROADMAP monthly-rain kill-screen logger.

Pure logic only (no network): bin parsing, month-boundary math, the threshold-crossing probability,
and the FULL = ensemble-within + climatology-beyond combination that must stay deterministic so a frozen
snapshot is reproducible.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import rain_screen_snapshot as rs


def test_parse_rain_bin():
    assert rs.parse_rain_bin({"strike_type": "greater", "floor_strike": 5, "ticker": "T"}) == (5.0, "T")
    assert rs.parse_rain_bin({"strike_type": "between", "floor_strike": 5, "ticker": "T"}) is None
    assert rs.parse_rain_bin({"strike_type": "greater", "ticker": "T"}) is None


def test_month_bounds():
    el, rem, dates = rs.month_bounds(date(2026, 7, 14))   # July has 31 days
    assert el == 14 and rem == 17
    assert dates[0] == date(2026, 7, 15) and dates[-1] == date(2026, 7, 31)
    # last day of month → nothing remaining
    el, rem, dates = rs.month_bounds(date(2026, 2, 28))   # 2026 not leap
    assert rem == 0 and dates == []


def test_prob_above():
    # remaining distribution [1,2,3,4]; banked 2.0; P(total > 4) = fraction with banked+s>4 = {s>2}=2/4
    assert rs.prob_above(4.0, 2.0, [1.0, 2.0, 3.0, 4.0]) == 0.5
    assert rs.prob_above(0.0, 5.0, [0.0, 0.0]) == 1.0    # already banked past threshold
    assert rs.prob_above(10.0, 0.0, [1.0, 2.0]) == 0.0
    assert rs.prob_above(3.0, 0.0, []) is None


def test_full_remaining_samples():
    # ensemble covers whole remaining → members ARE the distribution
    assert rs.full_remaining_samples([1.0, 2.0], []) == [1.0, 2.0]
    # no ensemble → climatology only
    assert rs.full_remaining_samples([], [3.0, 4.0]) == [3.0, 4.0]
    # combine: deterministic index-pairing, length = max(len)
    out = rs.full_remaining_samples([1.0, 2.0], [10.0, 20.0, 30.0])
    assert out == [11.0, 22.0, 31.0], out   # i%2 paired with i%3 → (1+10),(2+20),(1+30)


def test_bias_ratio():
    assert rs._bias_ratio([3.0, 3.0], [2.0, 2.0]) == 1.5    # gauge runs 1.5× the grid
    assert rs._bias_ratio([], [2.0]) == 1.0                 # undefined → identity
    assert rs._bias_ratio([3.0], [0.0]) == 1.0              # grid zero → identity


def test_sum_over_handles_trace_and_year():
    daily = {"2024-07-15": 0.0, "2024-07-16": 1.2, "2025-07-15": 0.5, "2025-07-16": 0.3}
    assert rs._sum_over(daily, {(7, 15), (7, 16)}, 2024) == 1.2
    assert rs._per_year_sums(daily, {(7, 15)}, [2024, 2025]) == [0.0, 0.5]


def test_full_prob_beats_or_differs_from_banked():
    # firewall sanity: a drier forecast than climatology lowers FULL below BANKED-ONLY
    banked = 3.0
    ens = [0.5, 0.6, 0.4]           # forecast: little more rain coming
    clim = [3.0, 4.0, 5.0]          # climatology: a lot more rain typically
    full = rs.prob_above(6.0, banked, rs.full_remaining_samples(ens, []))
    bankedonly = rs.prob_above(6.0, banked, clim)
    assert full < bankedonly        # 0/3 vs 2/3


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

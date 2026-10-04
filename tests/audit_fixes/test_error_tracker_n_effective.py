#!/usr/bin/env python3
"""Tests for Fix #1: Error Model n_effective formula.

The bug: n_effective used `n_new = n_old + α × (1 - n_old)` which converges
to 1.0 instead of accumulating. Both build_fair_values.py (threshold ≥3)
and the function's own has_good_data() check (threshold ≥2) were unreachable.

After fix: n_effective is a simple count (capped at 100).
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.error_tracker import ErrorTracker


def test_n_effective_increments_per_observation():
    """Each record_error() call should increase n_effective by ~1."""
    t = ErrorTracker()
    t._data = {}
    assert t.get_effective_n("PHX") == 0.0

    t.record_error("PHX", 100.0, 102.0)
    n1 = t.get_effective_n("PHX")
    assert 0.5 <= n1 <= 1.5, f"after 1 obs n should be ~1, got {n1}"

    t.record_error("PHX", 100.0, 99.0)
    n2 = t.get_effective_n("PHX")
    assert 1.5 <= n2 <= 2.5, f"after 2 obs n should be ~2, got {n2}"

    t.record_error("PHX", 100.0, 101.0)
    n3 = t.get_effective_n("PHX")
    assert 2.5 <= n3 <= 3.5, f"after 3 obs n should be ~3, got {n3}"


def test_n_effective_reaches_overflow_threshold():
    """The threshold (n >= 3) used by build_fair_values.py MUST be reachable
    within a reasonable number of observations. This is the regression test
    for the original bug."""
    t = ErrorTracker()
    t._data = {}
    for i in range(5):
        t.record_error("SEA", 70.0, 70.0 + (i % 3) * 0.5)
    n = t.get_effective_n("SEA")
    assert n >= 3.0, f"after 5 obs n should reach 3, got {n} (bug regression!)"


def test_has_good_data_works():
    """has_good_data() should return True after ≥2 observations."""
    t = ErrorTracker()
    t._data = {}
    assert not t.has_good_data("LAX")
    t.record_error("LAX", 75.0, 75.0)
    t.record_error("LAX", 76.0, 76.0)
    assert t.has_good_data("LAX"), "should have good data after 2 observations"


def test_n_effective_capped_at_100():
    """n_effective should not grow unbounded."""
    t = ErrorTracker()
    t._data = {}
    for i in range(200):
        t.record_error("NYC", 70.0, 70.0)
    n = t.get_effective_n("NYC")
    assert n <= 100.0, f"n should be capped at 100, got {n}"


def test_cities_independent():
    """Recording for one city shouldn't affect another."""
    t = ErrorTracker()
    t._data = {}
    t.record_error("HOU", 95.0, 96.0)
    t.record_error("HOU", 95.0, 95.5)
    t.record_error("HOU", 95.0, 95.8)
    assert t.get_effective_n("HOU") >= 2.5
    assert t.get_effective_n("SEA") == 0.0, "unrelated city should still be empty"


def test_bias_updates_with_each_observation():
    """Bias should update with each new observation, not get stuck at 0."""
    t = ErrorTracker()
    t._data = {}
    # 3 observations all showing +2°F bias
    t.record_error("PHX", 100.0, 102.0)
    t.record_error("PHX", 100.0, 102.0)
    t.record_error("PHX", 100.0, 102.0)
    bias = t.get_bias("PHX")
    assert bias > 0.5, f"bias should reflect systematic 2F overshoot, got {bias}"


def test_saturated_cities_flags_only_well_sampled_over_cap():
    """saturated_cities() surfaces cities the ±BIAS_CAP_F get_bias clamp is UNDER-correcting — but only
    well-sampled ones, so a few wild errors can't false-flag (2026-07-13 audit; HOU/PHX run too warm)."""
    from model.error_tracker import BIAS_CAP_F
    t = ErrorTracker()
    t._data = {
        "HOU": {"bias_f": -2.31, "n_effective": 41.0},   # clamped + well-sampled → flagged
        "PHX": {"bias_f": -2.24, "n_effective": 24.0},   # clamped + well-sampled → flagged
        "MIA": {"bias_f": -1.86, "n_effective": 45.0},   # under the cap → NOT flagged
        "XYZ": {"bias_f": -3.50, "n_effective": 4.0},    # over cap but too few obs → NOT flagged (noise)
    }
    sat = t.saturated_cities(min_n=10.0)
    assert [s["city"] for s in sat] == ["HOU", "PHX"], sat   # worst under-correction first, well-sampled
    assert sat[0]["applied_bias_f"] == -BIAS_CAP_F           # what get_bias actually returns
    assert sat[0]["under_correction_f"] == 0.31
    assert t.get_bias("HOU") == -BIAS_CAP_F                  # get_bias clamps the saturated city
    assert t.get_bias("MIA") == -1.86                        # under cap → unchanged


if __name__ == "__main__":
    import subprocess
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{7 - failed}/7 passed" if failed == 0 else f"\n❌ {failed}/7 FAILED")
    sys.exit(0 if failed == 0 else 1)

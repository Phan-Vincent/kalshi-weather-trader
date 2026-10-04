#!/usr/bin/env python3
"""Anytime-valid confidence sequence tests (compare_variants.asymp_confseq).

Pins the properties that make it safe for continuous (every-cron-cycle) monitoring: it is WIDER than
a fixed-n CI (the price of anytime-validity), shrinks with n, excludes 0 only on a real signal,
reduces to one value per EVENT (the independent unit), and degrades gracefully at n<2 / zero-variance.
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import asymp_confseq  # noqa: E402


def _items(vals):
    return [(f"e{i}", float(v)) for i, v in enumerate(vals)]


def _hw(ci):
    return (ci[2] - ci[1]) / 2.0


def test_wider_than_fixed_n():
    vals = [3, 4, 5, 6, 7] * 12                      # 60 events, mean 5
    ci = asymp_confseq(_items(vals), alpha=0.05, t_opt=100)
    n = len(vals)
    mu = sum(vals) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / (n - 1))
    fixed_hw = 1.96 * sd / math.sqrt(n)
    assert _hw(ci) > fixed_hw                         # anytime-valid must be strictly wider


def test_shrinks_with_n():
    pattern = [3, 4, 5, 6, 7]
    small = asymp_confseq(_items(pattern * 8), t_opt=100)     # n=40
    big = asymp_confseq(_items(pattern * 60), t_opt=100)      # n=300
    assert _hw(big) < _hw(small)


def test_excludes_zero_on_real_signal():
    ci = asymp_confseq(_items([9, 10, 11] * 40), t_opt=100)   # mean 10, tight, n=120
    assert ci[1] > 0                                          # lo > 0 → decisive


def test_spans_zero_on_noise():
    ci = asymp_confseq(_items([-3, -2, -1, 0, 1, 2, 3] * 12), t_opt=100)  # mean 0
    assert ci[1] < 0 < ci[2]


def test_degenerate_below_two_events():
    assert asymp_confseq([("e0", 5.0)]) == (5.0, None, None)
    assert asymp_confseq([]) == (None, None, None)


def test_zero_variance_is_not_spuriously_decisive():
    # all events identical → degenerate (lo==hi); callers require lo!=hi to rule decisive
    pt, lo, hi = asymp_confseq(_items([4.0] * 30))
    assert pt == 4.0 and lo == hi


def test_event_clustering_reduces_bins_to_one_per_event():
    # two bins of the same event must count as ONE observation, not two
    items = [("KXHIGHHOU-26JUL01", 10.0), ("KXHIGHHOU-26JUL01", 20.0)]  # one event, mean 15
    pt, lo, hi = asymp_confseq(items)
    assert pt == 15.0 and lo is None and hi is None   # n_events==1 → degenerate

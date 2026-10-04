#!/usr/bin/env python3
"""
tests/test_estimand_alignment.py — Regression for the 2026-07-06 audit finding that the
KILL tool's DISPLAYED CI and its KILL TRIGGER estimated different things: asymp_confseq
reduced to per-event means (event = independent unit) while cluster_bootstrap_ci's point
was the pooled per-trade mean (events with more contracts weighted more). On a concentrated
book they diverged in sign. markout_kill_test now feeds BOTH the per-event-mean series
(_event_mean_items), so the CI, the CS, and the verdict share one estimand.

Run: python3 -m pytest tests/test_estimand_alignment.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import markout_kill_test as mkt
from compare_variants import cluster_bootstrap_ci, asymp_confseq


def test_event_mean_items_reduces_to_one_per_event():
    items = [("E1", 10.0), ("E1", 20.0), ("E2", -3.0)]
    got = dict(mkt._event_mean_items(items))
    assert got == {"E1": 15.0, "E2": -3.0}


def test_bootstrap_and_cs_share_the_per_event_estimand():
    # One event has MANY losing trades, another has ONE winning trade. The pooled per-trade
    # mean is dragged negative; the per-event mean is positive. After alignment, the bootstrap
    # POINT must equal the mean of per-event means (matching the CS), not the per-trade pool.
    items = [("BIG", -4.0)] * 10 + [("SMALL", +30.0)]
    per_event = mkt._event_mean_items(items)
    pooled_per_trade = sum(v for _, v in items) / len(items)     # ≈ -0.9
    mean_of_event_means = sum(v for _, v in per_event) / len(per_event)  # (-4 + 30)/2 = 13
    assert abs(pooled_per_trade - mean_of_event_means) > 1.0, "fixture must expose the weighting gap"

    boot_pt = cluster_bootstrap_ci(per_event)[0]
    cs_pt = asymp_confseq(per_event)[0]
    assert abs(boot_pt - mean_of_event_means) < 1e-9, "bootstrap point must be the per-event mean now"
    assert abs(cs_pt - mean_of_event_means) < 1e-9, "CS point must match (same estimand)"
    assert abs(boot_pt - cs_pt) < 1e-9, "displayed CI and KILL trigger must share the estimand"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

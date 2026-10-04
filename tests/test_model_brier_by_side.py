#!/usr/bin/env python3
"""
tests/test_model_brier_by_side.py — Drawdown fix #2 (2026-07-13).

The fair_prob calibration monitor reported a single global reliability diagram, so a
NO-side calibration bias (the model runs systematically too LOW on P(yes) exactly where it
selects NO — predicted ~0.61 win, realized ~0.27) was averaged away. It also scored the
forecast-DECOUPLED premium arm's logged fair_prob as if it drove trades, which reads as a
model defect when it does not. This is a MONITORING-only change (no artifact the scanner
loads, no gating), so it cannot touch live selection.

Run: python3 -m pytest tests/test_model_brier_by_side.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bin.model_brier import compute_model_brier


def _trade(side, our_prob, outcome, mode="maker", decoupled=False):
    return {
        "ticker": "KXHIGHTATL-26JUL11-B90", "side": side, "our_prob": our_prob,
        "market_prob": 0.5, "outcome": outcome, "mode": mode,
        "fair_prob_decoupled": decoupled,
    }


def test_by_side_exposes_no_side_overconfidence():
    # NO-selected trades: model says P(yes)=0.39 → predicted NO-win 0.61, but YES wins most of
    # the time (NO loses) → realized NO-win low → large positive calibration_gap.
    trades = [_trade("no", 0.39, "yes") for _ in range(8)]     # NO lost 8x
    trades += [_trade("no", 0.39, "no") for _ in range(2)]     # NO won 2x  → realized 0.20
    trades += [_trade("yes", 0.55, "yes") for _ in range(6)]   # YES fine
    res = compute_model_brier(trades)
    assert res["by_side"]["no"]["n"] == 10
    assert abs(res["by_side"]["no"]["pred_win_prob"] - 0.61) < 1e-6
    assert abs(res["by_side"]["no"]["realized_win_rate"] - 0.20) < 1e-6
    assert res["by_side"]["no"]["calibration_gap"] > 0.30      # overconfident on NO
    # YES side is well-behaved and reported separately.
    assert res["by_side"]["yes"]["n"] == 6


def test_decoupled_records_excluded():
    good = [_trade("no", 0.39, "yes") for _ in range(4)]
    decoupled = [_trade("no", 0.39, "yes", decoupled=True) for _ in range(3)]
    premium = [_trade("no", 0.39, "yes", mode="premium") for _ in range(2)]
    res = compute_model_brier(good + decoupled + premium)
    assert res["n_trades"] == 4                 # only the coupled forecast fills scored
    assert res["n_decoupled_excluded"] == 5


def test_all_decoupled_returns_empty_not_crash():
    res = compute_model_brier([_trade("no", 0.39, "yes", mode="premium")])
    assert res["n_trades"] == 0
    assert res["n_decoupled_excluded"] == 1


def test_side_split_does_not_change_global_metrics():
    # Adding the by_side block must not alter the pre-existing aggregate fields.
    trades = [_trade("yes", 0.6, "yes"), _trade("no", 0.4, "no"), _trade("yes", 0.7, "no")]
    res = compute_model_brier(trades)
    assert res["n_trades"] == 3
    assert res["model_brier"] is not None
    assert "reliability" in res and "per_city" in res


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

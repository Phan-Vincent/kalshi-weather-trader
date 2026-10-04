#!/usr/bin/env python3
"""Regression: post-mortems classify BELOW ("<S°") T-markets correctly.

Pre-existing diagnostic bug (found during the 2026-07-03 weather-model QA of fix #5):
bin/postmortem.py re-parsed the bare ticker (parse_market_ticker WITHOUT the raw
market), and parse_market_ticker infers T-market direction from the market TITLE — a
missing title defaults to "above" (data/weather_data._infer_direction_from_title). Live
T-market titles are overwhelmingly "<S°" (below), so nearly every below-market loss was
mis-classified as "above": wrong bin_kind, wrong observed_minus_threshold_f, a wrong-signed
forecast_too_warm/cool lesson, and (after fix #5) a threshold shifted +0.5 instead of -0.5.

Fix: settle_paper.py — which holds the raw market (and thus the title) at settlement time —
persists a `market_parsed` snapshot into the loss-queue record; postmortem reads that instead
of re-parsing the title-less ticker. This is diagnostics-only (no pricing/settlement/money
effect) but the post-mortem reports and classification JSON are now direction-correct.

Runs under plain python3 and pytest.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import parse_market_ticker  # noqa: E402
import postmortem  # noqa: E402
import settle_paper  # noqa: E402

TK = "KXHIGHTHOU-26JUL04-T99"


def _below_raw(strike=99):
    # Shape of `kalshi-cli markets get`: the title carries the "<S°" direction.
    return {"title": f"Will the high temp in Houston be <{strike}° on Jul 4, 2026?", "result": "no"}


def _above_raw(strike=99):
    return {"title": f"Will the high temp in Houston be >{strike}° on Jul 4, 2026?", "result": "no"}


def _loss(side, snap, pnl=-40):
    return {
        "ticker": snap["ticker"],
        "side": side,
        "pnl_cents": pnl,
        "entry_cents": 60,
        "fair_prob_at_open": 0.75,
        "settlement_result": "no" if side == "yes" else "yes",
        "market_parsed": snap,
    }


def test_snapshot_parses_below_and_is_json_safe():
    snap = settle_paper._parsed_snapshot(TK, _below_raw(99))
    assert snap is not None
    assert snap["bin_kind"] == "below"
    assert snap["threshold_f"] == 98.5           # <99 pays at 98 => continuous < 98.5
    # ±inf edges flattened to None so the on-disk JSONL stays standard JSON
    assert snap["bin_low"] is None
    assert snap["bin_high"] == 98.5


def test_snapshot_never_raises_on_bad_ticker():
    assert settle_paper._parsed_snapshot("NOT-A-WEATHER-TICKER", {"title": "x"}) is None


def test_postmortem_prefers_stored_snapshot_over_bare_ticker():
    snap = settle_paper._parsed_snapshot(TK, _below_raw(99))
    # The bare-ticker parse (no title) WOULD mis-default to "above" — this is the bug.
    assert parse_market_ticker(TK)["bin_kind"] == "above"
    # With the snapshot present, postmortem reads the correct direction.
    assert postmortem._parsed_for_record({"ticker": TK, "market_parsed": snap})["bin_kind"] == "below"
    # Legacy record (no snapshot) still falls back to the mis-default — documents the known limit.
    assert postmortem._parsed_for_record({"ticker": TK})["bin_kind"] == "above"


def test_below_lost_yes_is_forecast_too_cool():
    snap = settle_paper._parsed_snapshot(TK, _below_raw(99))   # boundary 98.5
    observed = {"high_f": 101.2, "source": "test"}             # warm → YES(below) loses
    c = postmortem._classify_failure(_loss("yes", snap), observed)
    assert c["bin_kind"] == "below"
    assert c["threshold_f"] == 98.5
    assert c["failure_mode"].startswith("forecast_too_cool")
    assert c["observed_minus_threshold_f"] == round(101.2 - 98.5, 2)   # +2.7
    assert c["direction"] == "warmer_than_threshold"
    assert "undershot" in c["lesson"] and "HOU" in c["lesson"]


def test_below_lost_no_is_forecast_too_warm():
    snap = settle_paper._parsed_snapshot(TK, _below_raw(99))
    observed = {"high_f": 95.0, "source": "test"}             # cold → below wins → NO loses
    c = postmortem._classify_failure(_loss("no", snap), observed)
    assert c["bin_kind"] == "below"
    assert c["failure_mode"].startswith("forecast_too_warm")
    assert c["observed_minus_threshold_f"] == round(95.0 - 98.5, 2)    # -3.5


def test_snapshot_fixes_mis_sign_vs_legacy_bare_ticker():
    """The core regression: same below-market loss, classified with vs without the snapshot."""
    snap = settle_paper._parsed_snapshot(TK, _below_raw(99))
    observed = {"high_f": 101.2, "source": "test"}
    fixed = postmortem._classify_failure(_loss("yes", snap), observed)
    legacy_rec = {k: v for k, v in _loss("yes", snap).items() if k != "market_parsed"}
    legacy = postmortem._classify_failure(legacy_rec, observed)
    # Fixed: correct below-direction, forecast_too_cool, boundary 98.5.
    assert fixed["bin_kind"] == "below" and fixed["failure_mode"].startswith("forecast_too_cool")
    assert fixed["threshold_f"] == 98.5
    # Pre-fix: mis-reads direction as "above", flips the diagnosis sign, and shifts the
    # boundary the wrong way (+0.5 → 99.5 instead of -0.5 → 98.5).
    assert legacy["bin_kind"] == "above" and legacy["failure_mode"].startswith("forecast_too_warm")
    assert legacy["threshold_f"] == 99.5


def test_above_market_classification_unchanged():
    snap = settle_paper._parsed_snapshot(TK, _above_raw(99))
    assert snap["bin_kind"] == "above" and snap["threshold_f"] == 99.5
    observed = {"high_f": 97.0, "source": "test"}             # cool → YES(above) loses
    c = postmortem._classify_failure(_loss("yes", snap), observed)
    assert c["failure_mode"].startswith("forecast_too_warm")
    assert c["observed_minus_threshold_f"] == round(97.0 - 99.5, 2)


if __name__ == "__main__":
    for fn in [
        test_snapshot_parses_below_and_is_json_safe,
        test_snapshot_never_raises_on_bad_ticker,
        test_postmortem_prefers_stored_snapshot_over_bare_ticker,
        test_below_lost_yes_is_forecast_too_cool,
        test_below_lost_no_is_forecast_too_warm,
        test_snapshot_fixes_mis_sign_vs_legacy_bare_ticker,
        test_above_market_classification_unchanged,
    ]:
        fn()
    print("OK — below-market post-mortems classify direction/boundary correctly")

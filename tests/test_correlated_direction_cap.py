#!/usr/bin/env python3
"""
tests/test_correlated_direction_cap.py — Drawdown fix #1 (2026-07-13).

The 2026-07-10..12 live drawdown was 12 SAME-direction (NO / cold-lean) positions across
DIFFERENT cities all busting together in one warm-forecast regime. The per-event cap, per-city
circuit breaker, and global open-position count all passed them (distinct events, distinct
cities, 12 << 40). This adds a cross-city correlated-direction cap.

Guarantees under test:
  * DEFAULT OFF (both knobs 0): every check is a no-op → landing this code cannot change live
    selection or perturb the futility-checkpoint stream.
  * When armed, the cap counts DISTINCT same-thermal-direction EVENTS across cities and blocks
    the (N+1)th; another strike of an already-counted event does NOT consume budget; the
    opposite direction and band (KXTEMP) markets are unaffected; the $ notional cap works.

Run: python3 -m pytest tests/test_correlated_direction_cap.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.risk import RiskGate, _market_family, _correlated_side


def _gate(events=0, dollars=0):
    d = tempfile.mkdtemp()
    r = RiskGate(_state_dir=Path(d))
    r.max_correlated_direction_events = events
    r.max_correlated_direction_dollars = dollars
    return r


# ── helpers ──────────────────────────────────────────────────────────

def test_market_family_and_correlated_side():
    assert _market_family("KXHIGHTATL-26JUL11-B76.5") == "high"
    assert _market_family("KXLOWTATL-26JUL11-B74.5") == "low"
    assert _market_family("KXHIGHNY-26JUL11-T90") == "high"
    assert _market_family("KXTEMPNYCH-26MAY2816-T82") == "temp"
    assert _market_family("KXNBAFINALS-26-XYZ") == ""
    # Correlation axis is the BET SIDE on a daily high/low market — NOT an inferred warm/cold
    # direction, which would misclassify the two-tailed '-B' band markets that actually busted.
    assert _correlated_side("KXHIGHTATL-26JUL11-B76.5", "no") == "no"   # band NO → bucketed
    assert _correlated_side("KXLOWTATL-26JUL11-B74.5", "no") == "no"    # band NO → bucketed
    assert _correlated_side("KXHIGHTATL-26JUL11-B76.5", "yes") == "yes"
    assert _correlated_side("KXHIGHNY-26JUL11-T90", "no") == "no"       # threshold NO → bucketed
    # KXTEMP hourly and non-weather tickers are excluded from the cap.
    assert _correlated_side("KXTEMPNYCH-26MAY2816-T82", "no") is None
    assert _correlated_side("KXNBAFINALS-26-XYZ", "no") is None


def test_band_market_no_is_the_actual_drawdown_case():
    # Regression for the review finding: the 12 losers were NO bets on '-B' BAND markets
    # (B76.5, B109.5, …). A warm/cold 'thermal' mapping treats a band NO as two-tailed and
    # would EXCLUDE exactly these — the cap MUST bucket them (by side) so it can catch the
    # real failure mode.
    r = _gate(events=3)
    band_no_book = [
        {"ticker": t, "side": "no", "count": 14, "avg_cost_cents": 24}
        for t in ("KXLOWTATL-26JUL11-B76.5", "KXHIGHTLV-26JUL10-B109.5", "KXHIGHTSEA-26JUL10-B77.5")
    ]
    r.seed_direction_exposure(band_no_book)          # 3 distinct band-NO events at the cap
    ok, reason = r.check_correlated_direction("KXHIGHTOKC-26JUL11-B97.5", "no", 6, 32)
    assert ok is False and "corr_side:no" in reason


# ── default OFF = no-op ──────────────────────────────────────────────

def test_disabled_is_noop():
    r = _gate(events=0, dollars=0)
    assert r.correlated_cap_enabled is False
    # Seeding a huge same-direction book and then checking must never block when disabled.
    r.seed_direction_exposure([
        {"ticker": f"KXHIGHTC{i}-26JUL11-B90", "side": "no", "count": 5, "avg_cost_cents": 33}
        for i in range(50)
    ])
    ok, reason = r.check_correlated_direction("KXHIGHTZZ-26JUL11-B90", "no", 5, 33)
    assert ok is True and reason is None


# ── event-count cap ──────────────────────────────────────────────────

def _cold_book(n):
    # n distinct cold (NO) events across distinct cities.
    return [
        {"ticker": f"KXHIGHTC{i}-26JUL11-B90", "side": "no", "count": 5, "avg_cost_cents": 33}
        for i in range(n)
    ]


def test_blocks_the_n_plus_first_correlated_event():
    r = _gate(events=5)
    r.seed_direction_exposure(_cold_book(5))          # already at the cap of 5 cold events
    ok, reason = r.check_correlated_direction("KXLOWTNEW-26JUL11-B70", "no", 5, 33)
    assert ok is False and "corr_side:no" in reason


def test_additional_strike_of_existing_event_is_allowed():
    r = _gate(events=5)
    r.seed_direction_exposure(_cold_book(5))
    # Another STRIKE of an already-counted event does not raise the distinct-event count.
    ok, reason = r.check_correlated_direction("KXHIGHTC0-26JUL11-B92", "no", 5, 33)
    assert ok is True, f"same-event strike should pass, got {reason}"


def test_opposite_side_unaffected():
    r = _gate(events=5)
    r.seed_direction_exposure(_cold_book(5))
    # 5 NO events open, but a YES event is a different side bucket → allowed.
    ok, _ = r.check_correlated_direction("KXHIGHTX-26JUL11-B90", "yes", 5, 25)
    assert ok is True


def test_hourly_temp_and_nonweather_never_blocked():
    # KXTEMP hourly markets and non-weather tickers are OUT of scope (the cap targets the daily
    # high/low book that busted). NB: daily '-B' band markets ARE in scope — see the band test.
    r = _gate(events=1)
    r.seed_direction_exposure(_cold_book(1))
    ok_hourly, _ = r.check_correlated_direction("KXTEMPNYCH-26JUL11-T82", "no", 5, 33)
    ok_other, _ = r.check_correlated_direction("KXNBAFINALS-26-XYZ", "no", 5, 33)
    assert ok_hourly is True and ok_other is True


def test_add_direction_exposure_accumulates_in_run():
    r = _gate(events=2)
    r.seed_direction_exposure([])                     # empty book
    assert r.check_correlated_direction("KXHIGHTA-26JUL11-B90", "no", 5, 33)[0] is True
    r.add_direction_exposure("KXHIGHTA-26JUL11-B90", "no", 5, 33)
    assert r.check_correlated_direction("KXLOWTB-26JUL11-B70", "no", 5, 33)[0] is True
    r.add_direction_exposure("KXLOWTB-26JUL11-B70", "no", 5, 33)
    # Two distinct NO events now open this run; a third must be blocked.
    ok, reason = r.check_correlated_direction("KXHIGHTC-26JUL11-B90", "no", 5, 33)
    assert ok is False and "corr_side:no events 3>2" in reason


# ── dollar notional cap ──────────────────────────────────────────────

def test_dollar_notional_cap():
    r = _gate(dollars=1)          # $1.00 worst-case per direction bucket
    r.seed_direction_exposure([
        {"ticker": "KXHIGHTA-26JUL11-B90", "side": "no", "count": 2, "avg_cost_cents": 33},  # 66c
    ])
    # +40c = $1.06 > $1.00 → blocked purely on notional even though it's only the 2nd event.
    ok, reason = r.check_correlated_direction("KXLOWTB-26JUL11-B70", "no", 1, 40)
    assert ok is False and "corr_side:no $" in reason
    # A small add that stays under $1.00 is allowed.
    ok2, _ = r.check_correlated_direction("KXLOWTB-26JUL11-B70", "no", 1, 20)  # 66+20=86c
    assert ok2 is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

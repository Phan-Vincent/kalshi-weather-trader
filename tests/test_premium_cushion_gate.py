#!/usr/bin/env python3
"""Adverse-markout cushion gate tests (proposal #4: fewer, less-adverse fills).

premium_quote_price gains a MIN-spread floor: resting at the join-bid, the book cushion below mid is
~half the spread, so a too-tight spread can't cover the confirmed ~-5c post-fill adverse markout —
the fill is a near-certain net loss. The gate skips those. CRITICAL: it defaults OFF
(KALSHI_WEATHER_PREMIUM_MIN_SPREAD unset/0) so live behavior is unchanged until activated.
tests/conftest.py autouse-restores KALSHI_WEATHER_* env, so setenv here is safe/order-independent.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.scanner import premium_quote_price  # noqa: E402


def _mkt(yes_bid_c, yes_ask_c):
    return {"yes_bid_dollars": yes_bid_c / 100.0, "yes_ask_dollars": yes_ask_c / 100.0}


def test_default_off_preserves_tight_spread(monkeypatch):
    monkeypatch.delenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", raising=False)
    # 1c spread, yes_bid 30 in band → still quotes exactly as before (no behavior change)
    assert premium_quote_price(_mkt(30, 31)) == ("yes", 30)


def test_min_spread_rejects_cushionless_fill(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "6")
    assert premium_quote_price(_mkt(30, 32)) is None          # spread 2 < 6 → skipped


def test_min_spread_boundary_accepts_equal(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "6")
    assert premium_quote_price(_mkt(30, 36)) == ("yes", 30)   # spread 6 == 6 → passes


def test_max_spread_still_rejects_wide(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "4")
    assert premium_quote_price(_mkt(30, 45)) is None          # spread 15 > max 12 → skipped (unchanged)


def test_no_side_respects_cushion_gate(monkeypatch):
    # yes_bid 70 (out of band); no_bid = 100 - yes_ask = 100 - 76 = 24 (in band); spread 6
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "6")
    assert premium_quote_price(_mkt(70, 76)) == ("no", 24)    # spread 6 >= 6 → NO-side quote
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "8")
    assert premium_quote_price(_mkt(70, 76)) is None          # spread 6 < 8 → skipped


def test_gate_reduces_fill_count(monkeypatch):
    markets = [_mkt(30, 31), _mkt(30, 33), _mkt(30, 36), _mkt(30, 40)]  # spreads 1,3,6,10
    monkeypatch.delenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", raising=False)
    n_off = sum(premium_quote_price(m) is not None for m in markets)
    monkeypatch.setenv("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "6")
    n_on = sum(premium_quote_price(m) is not None for m in markets)
    assert n_off == 4 and n_on == 2                           # fewer fills once the gate is on

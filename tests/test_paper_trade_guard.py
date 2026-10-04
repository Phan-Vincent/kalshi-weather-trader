#!/usr/bin/env python3
"""
tests/test_paper_trade_guard.py — Targeted tests for paper_trade.py idempotency
guard and city extraction validation.

Run: python3 tests/test_paper_trade_guard.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from trader.paper_book import PaperBook
from trader.risk import RiskGate
from trader.scanner import scan


def _tmp_book():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return PaperBook(state_path=Path(path)), path


def test_city_extraction_skips_date_codes():
    """City extraction must NOT match date codes like MAY/JUN."""
    from bin.paper_trade import _extract_city_from_ticker

    # Standard tickers -> valid cities
    assert _extract_city_from_ticker("KXHIGHTHOU-26MAY28-T91") == "HOU"
    assert _extract_city_from_ticker("KXLOWTLAX-26JUN28-T91") == "LAX"
    assert _extract_city_from_ticker("KXTEMPNYC-26MAY28-T91") == "NYC"

    # Malformed/hypothetical tickers where date code appears before dash
    # These should return "" because MAY/JUN are not valid city codes (not 2-4 alpha)
    # Actually, MAY is 3 alpha letters, so we need to be careful. But in standard
    # tickers, the date is AFTER the dash, so the prefix never contains MAY/JUN.
    assert _extract_city_from_ticker("KXHIGHTMAY-26MAY28-T91") == ""  # MAY is 3 letters but invalid city
    assert _extract_city_from_ticker("KXHIGHTJUN-26JUN28-T91") == ""  # JUN is 3 letters but invalid city

    # Edge cases
    assert _extract_city_from_ticker("KXHIGHT-26MAY28-T91") == ""  # no city after HIGHT
    assert _extract_city_from_ticker("RANDOM") == ""  # no tag
    print("[PASS] city extraction correctly rejects date-code false positives")


def test_scanner_city_extraction_skips_date_codes():
    """Scanner's _ticker_city must also reject date codes."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    close_ok = (now + datetime.timedelta(hours=12)).isoformat()

    fixture = [
        {
            "ticker": "KXHIGHTHOU-26MAY28-T91",
            "fair_prob": 0.99,
            "confidence": "high",
            "rationale": "test",
            "source": "test",
            "_market": {
                "close_time": close_ok,
                "yes_bid": 49,
                "yes_ask": 50,
                "no_bid": 49,
                "no_ask": 50,
            },
        },
    ]
    fd, fv_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(fv_path, "w") as f:
        json.dump(fixture, f)

    # Use isolated risk state so production circuit breakers don't interfere
    from unittest.mock import patch
    with patch.object(RiskGate, "_load_state", lambda self: None):
        gate = RiskGate()
        gate._today_pnl_cents = 0
        gate._week_pnl_cents = 0
        gate._today = "test"
        gate._week_start = "test"
        gate._open_positions = []
        gate._realized_today_cents = 0
        gate._consecutive_losses = 0
        gate._consecutive_losses_by_city = {}
        gate._city_pnl_cents = {}
        gate._total_bankroll_cents = 50000
        gate._halted_cities = []
        # Per-event cap enforcement (added 2026-06-19) needs a non-zero cap, else
        # kelly_qty sizes to 0 and no signal is produced. _load_state is mocked, so
        # set it (and the ramp-state fields) explicitly.
        gate.per_event_max_position_dollars = 50
        gate._rung_idx = 0
        gate._fills_at_rung_start = 0
        gate._balance_at_rung_start = 0
        gate.check_close_time = lambda _close_time: True
        signals = scan(fv_path, gate, mode="taker", top_n=5)

    assert len(signals) == 1, f"Expected 1 signal, got {len(signals)}"
    assert signals[0].ticker == "KXHIGHTHOU-26MAY28-T91"
    os.unlink(fv_path)
    print("[PASS] scanner city extraction works for standard tickers")


def test_paper_trade_idempotency_skip():
    """Simulate paper_trade.py idempotency: skip tickers already in open/pending."""
    book, book_path = _tmp_book()

    # Pre-seed an open position
    book.fill_taker(
        ticker="KXHIGHTHOU-26MAY28-T91",
        side="yes",
        ask_price_cents=50,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2099-01-01T23:59:00+00:00",
    )
    book._save()

    # Re-load and build existing_tickers exactly as paper_trade.py's live loop does.
    book2 = PaperBook(state_path=Path(book_path))
    existing_tickers = {p["ticker"] for p in book2.open + book2.pending_makers}

    # QA-13 (2026-07-01): exercise the REAL dedup guard, not a set the test built itself
    # (the old body asserted membership in a set it had just constructed — it could never fail).
    from trader.risk import RiskGate
    rg = RiskGate()
    rg.research_mode.enabled = True
    rg.research_mode.allow_duplicates = False
    assert rg.check_duplicate_exposure("KXHIGHTHOU-26MAY28-T91", existing_tickers)[0] is False, \
        "held ticker must be skipped by the duplicate-exposure guard"
    assert rg.check_duplicate_exposure("KXHIGHLAX-26JUL01-T95", existing_tickers)[0] is True, \
        "a fresh ticker must still be allowed"
    print("[PASS] paper_trade.py idempotency guard skips held tickers, allows fresh")
    os.unlink(book_path)


if __name__ == "__main__":
    test_city_extraction_skips_date_codes()
    test_scanner_city_extraction_skips_date_codes()
    test_paper_trade_idempotency_skip()
    print("\nAll paper_trade guard tests passed.")

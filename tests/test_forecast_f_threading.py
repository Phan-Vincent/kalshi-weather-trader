#!/usr/bin/env python3
"""
tests/test_forecast_f_threading.py — Regression for the 2026-07-06 audit finding:
the per-city error-model EWMA silently froze Jun 18–Jul 6 2026 because the MAKER
fill path dropped forecast_f. post_maker() accepted no forecast_f, PendingMaker did
not carry it, and _convert_maker_to_fill() built the Position without it — so every
maker fill (the live/premium book is maker-only) landed with forecast_f=None and
settle_paper's error-model update gate (`forecast_f is not None`) never fired.

These tests pin the full maker plumbing so the freeze cannot silently recur.

Run: python3 -m pytest tests/test_forecast_f_threading.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.paper_book import PaperBook


def _book():
    tmp = tempfile.mkdtemp()
    return PaperBook(state_path=Path(tmp) / "paper-book.json")


def _future_close(hours: int = 24) -> str:
    """A close_time safely in the future so sweep_pending_makers won't cancel the maker
    as market-closed. Must be dynamic: a hardcoded '2026-07-07T06:00:00Z' rotted this
    suite the following day (the maker got cancelled_market_closed once that instant passed)."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_post_maker_records_forecast_f_on_pending():
    book = _book()
    book.post_maker(
        ticker="KXHIGHTHOU-26JUL06-B95", side="yes",
        limit_price_cents=30, qty=5, fair_prob=0.4,
        edge_cents_post_fee=5.0, confidence="high",
        market_close_time=_future_close(),
        forecast_f=94.3,
    )
    assert book.pending_makers, "maker was not queued"
    assert book.pending_makers[0]["forecast_f"] == 94.3


def test_maker_fill_carries_forecast_f_to_open_position():
    """The core regression: a maker fill must land an open Position with forecast_f
    populated so settle_paper's error-model update actually fires."""
    book = _book()
    book.post_maker(
        ticker="KXHIGHTHOU-26JUL06-B95", side="yes",
        limit_price_cents=30, qty=5, fair_prob=0.4,
        edge_cents_post_fee=5.0, confidence="high",
        market_close_time=_future_close(),
        forecast_f=94.3,
    )
    # A live snapshot whose YES bid crosses the 30c limit with ample depth.
    snapshot = {
        "KXHIGHTHOU-26JUL06-B95": {
            "yes_bid": 45, "yes_bid_qty": 100,
            "yes_ask": 47, "no_bid": 53, "no_bid_qty": 100, "no_ask": 55,
        }
    }
    fills = book.sweep_pending_makers(snapshot)
    filled = [f for f in fills if f.get("status") == "filled_maker"]
    assert filled, f"maker did not fill: {fills}"
    assert book.open, "no open position after maker fill"
    pos = book.open[0]
    assert pos["forecast_f"] == 94.3, (
        f"forecast_f dropped in maker fill path: {pos.get('forecast_f')!r} "
        "(this is exactly the bug that froze the error model)"
    )


def test_taker_fill_still_carries_forecast_f():
    """Guard the taker path too (it was already correct — keep it that way)."""
    book = _book()
    book.fill_taker(
        ticker="KXHIGHTHOU-26JUL06-B95", side="yes",
        ask_price_cents=40, qty=5, fair_prob=0.5,
        edge_cents_post_fee=5.0, confidence="high",
        market_close_time=_future_close(),
        forecast_f=91.0,
    )
    assert book.open and book.open[0]["forecast_f"] == 91.0


if __name__ == "__main__":
    test_post_maker_records_forecast_f_on_pending()
    test_maker_fill_carries_forecast_f_to_open_position()
    test_taker_fill_still_carries_forecast_f()
    print("ok")

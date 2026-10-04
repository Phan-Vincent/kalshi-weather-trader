#!/usr/bin/env python3
"""Tests for Fix #2: Paper Book double-exposure on same-ticker maker fill.

The bug: if a taker position for ticker X already existed in book.open
AND a pending maker for ticker X was queued, sweep_pending_makers would
fill the maker and add a SECOND position for the same ticker (2x exposure).

After fix: _convert_maker_to_fill rejects if a position already exists
for the same ticker.
"""
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.paper_book import PaperBook


# Close times must be computed relative to *now*, never hardcoded calendar dates:
# sweep_pending_makers auto-cancels any maker whose market close_time <= now(), so fixed
# past literals (the original 26JUN15 strings) silently turn every fill/reject/requeue
# assertion into a cancelled_market_closed once the wall clock passes them.
def _future_close(hours: int = 6) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past_close(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_maker_rejected_when_taker_already_open():
    """If book.open has ticker X, a maker sweep on ticker X must reject."""
    with tempfile.TemporaryDirectory() as td:
        # Use a fresh state file
        state_path = Path(td) / "paper-book.json"
        book = PaperBook(state_path=state_path)
        close = _future_close()

        # Manually inject an open taker position for KXHIGHTPHX
        book.open.append({
            "ticker": "KXHIGHTPHX-26JUN15-B104.5",
            "side": "yes", "qty": 5, "avg_entry_cents": 50,
            "fee_cents_total": 5, "cost_cents_total": 255,
            "fair_prob_at_open": 0.65, "opened_utc": "2026-06-15T18:00:00+00:00",
            "market_close_time": close,
            "paper_order_id": "test-taker-1",
            "rationale": "test", "brier_record_id": None, "brier_logged": True,
            "forecast_f": 103.0,
        })
        book.cash_cents = 20000
        book._save()

        # Post a maker quote for the SAME ticker
        post_result = book.post_maker(
            ticker="KXHIGHTPHX-26JUN15-B104.5",
            side="yes",
            limit_price_cents=48,
            qty=3,
            fair_prob=0.65,
            edge_cents_post_fee=12.0,
            confidence="med",
            market_close_time=close,
            rationale="test maker",
        )
        assert post_result["status"] == "posted"

        # Now sweep: live bid crosses the limit with real depth (bid_qty >= fill-depth gate)
        snap = {
            "KXHIGHTPHX-26JUN15-B104.5": {
                "yes_bid": 50,
                "yes_ask": 52,
                "yes_bid_qty": 10,
                "no_bid": 48,
                "no_ask": 50,
                "close_time": close,
            }
        }
        sweep = book.sweep_pending_makers(snap)

        # The maker must NOT fill into a second position
        assert len([p for p in book.open if p["ticker"] == "KXHIGHTPHX-26JUN15-B104.5"]) == 1, \
            f"double exposure: open has {len([p for p in book.open if p['ticker'] == 'KXHIGHTPHX-26JUN15-B104.5'])} positions for same ticker"

        # Sweep should have recorded a rejection
        rejects = [r for r in sweep if r.get("status") == "rejected_double_exposure"]
        assert len(rejects) == 1, f"expected 1 double-exposure rejection, got {sweep}"


def test_cash_shortage_requeues_maker():
    """A maker rejected due to cash shortage should be requeued, not dropped."""
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "paper-book.json"
        book = PaperBook(state_path=state_path)

        # Set cash to a very small amount
        book.cash_cents = 50  # not enough for any meaningful fill

        # Post a maker
        close = _future_close()
        post_result = book.post_maker(
            ticker="KXHIGHTLAX-26JUN15-B72.5",
            side="yes",
            limit_price_cents=70,
            qty=2,
            fair_prob=0.60,
            edge_cents_post_fee=5.0,
            confidence="med",
            market_close_time=close,
        )
        assert post_result["status"] == "posted"
        paper_id = post_result["paper_order_id"]

        # Sweep: bid crosses limit with depth, but cash is too low → requeue
        snap = {
            "KXHIGHTLAX-26JUN15-B72.5": {
                "yes_bid": 75, "yes_ask": 80, "yes_bid_qty": 10, "no_bid": 20, "no_ask": 25,
                "close_time": close,
            }
        }
        sweep = book.sweep_pending_makers(snap)

        # The maker must be requeued
        assert len(book.pending_makers) == 1, f"maker should be requeued, pending_makers = {len(book.pending_makers)}"
        assert book.pending_makers[0]["paper_order_id"] == paper_id

        # The sweep result should reflect requeue
        requeues = [r for r in sweep if r.get("status") == "rejected_no_cash_requeued"]
        assert len(requeues) == 1, f"expected 1 requeue, got {sweep}"


def test_no_double_exposure_different_tickers():
    """Sanity: different tickers SHOULD both fill."""
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "paper-book.json"
        book = PaperBook(state_path=state_path)
        book.cash_cents = 30000
        close = _future_close()

        for tk in ("KXHIGHTSEA-26JUN15-B65.5", "KXHIGHTBOS-26JUN15-B85.5"):
            book.post_maker(
                ticker=tk, side="yes", limit_price_cents=45, qty=2,
                fair_prob=0.60, edge_cents_post_fee=8.0, confidence="med",
                market_close_time=close,
            )

        snap = {
            "KXHIGHTSEA-26JUN15-B65.5": {"yes_bid": 50, "yes_ask": 55, "yes_bid_qty": 10,
                                          "no_bid": 45, "no_ask": 50, "close_time": close},
            "KXHIGHTBOS-26JUN15-B85.5": {"yes_bid": 50, "yes_ask": 55, "yes_bid_qty": 10,
                                          "no_bid": 45, "no_ask": 50, "close_time": close},
        }
        sweep = book.sweep_pending_makers(snap)
        fills = [r for r in sweep if r.get("status") == "filled_maker"]
        assert len(fills) == 2, f"both should fill, got {len(fills)}"


def test_market_closed_auto_cancels_maker():
    """Makers in markets that have already closed should be cancelled, not filled."""
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "paper-book.json"
        book = PaperBook(state_path=state_path)

        closed = _past_close()  # market already closed relative to now
        book.post_maker(
            ticker="KXLOWTNYC-26JUN10-B55.5",
            side="no", limit_price_cents=50, qty=1,
            fair_prob=0.45, edge_cents_post_fee=8.0, confidence="med",
            market_close_time=closed,
        )
        snap = {
            "KXLOWTNYC-26JUN10-B55.5": {
                "yes_bid": 50, "yes_ask": 55, "no_bid": 45, "no_ask": 50,
                "close_time": closed,
            }
        }
        sweep = book.sweep_pending_makers(snap)
        cancels = [r for r in sweep if "cancelled" in r.get("status", "")]
        assert len(cancels) == 1, f"closed market maker should be cancelled: {sweep}"
        assert len(book.pending_makers) == 0, "cancelled maker should be removed from queue"


if __name__ == "__main__":
    import subprocess
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            print(f"  ❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed" if failed == 0 else f"\n❌ {failed}/{len(tests)} FAILED")
    sys.exit(0 if failed == 0 else 1)

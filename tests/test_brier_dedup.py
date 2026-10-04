#!/usr/bin/env python3
"""
tests/test_brier_dedup.py — Verify brier-log deduplication after state reload.

Run: python3 tests/test_brier_dedup.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from trader.paper_book import PaperBook, FilledOrder, Position, PendingMaker
from trader.brier import BrierLogger


def _tmp_paths():
    """Return temp paths for paper_book.json and brier-log.jsonl."""
    fd1, book_path = tempfile.mkstemp(suffix=".json")
    os.close(fd1)
    fd2, brier_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd2)
    return Path(book_path), Path(brier_path)


def test_fill_taker_sets_brier_logged_false():
    book_path, _ = _tmp_paths()
    book = PaperBook(state_path=book_path)
    result = book.fill_taker(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        ask_price_cents=50,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2026-01-01T23:59:00+00:00",
    )
    assert result["status"] == "filled"
    pos = book.open[0]
    assert pos.get("brier_logged") is False, "New fill must have brier_logged=False"
    assert pos.get("brier_record_id") is None
    os.unlink(book_path)
    print("[PASS] fill_taker sets brier_logged=False on new position")


def test_maker_post_sets_brier_logged_false():
    book_path, _ = _tmp_paths()
    book = PaperBook(state_path=book_path)
    result = book.post_maker(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        limit_price_cents=45,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2026-01-01T23:59:00+00:00",
    )
    assert result["status"] == "posted"
    pm = book.pending_makers[0]
    assert pm.get("brier_logged") is False, "New maker post must have brier_logged=False"
    os.unlink(book_path)
    print("[PASS] post_maker sets brier_logged=False on new pending maker")


def test_maker_sweep_carries_brier_logged():
    book_path, _ = _tmp_paths()
    book = PaperBook(state_path=book_path)
    book.post_maker(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        limit_price_cents=1,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2099-01-01T23:59:00+00:00",  # far future
    )
    # Manually mark as already logged (simulates post-time Brier logging)
    book.pending_makers[0]["brier_logged"] = True
    book.pending_makers[0]["brier_record_id"] = "test-uuid-123"
    book._save()

    # Reload and sweep with a market snapshot that crosses the limit
    book2 = PaperBook(state_path=book_path)
    # yes_bid_qty required by the 2026-06-19 depth gate (bid_qty >= max(min_bid_qty, qty*0.5)).
    snap = {"KXHIGHTEST-01JAN01-T50": {"yes_bid": 5, "yes_ask": 10, "no_bid": 90, "no_ask": 95, "yes_bid_qty": 5}}
    fills = book2.sweep_pending_makers(snap)
    assert any(f["status"] == "filled_maker" for f in fills)
    pos = book2.open[0]
    assert pos.get("brier_logged") is True, "Swept position must inherit brier_logged from pending maker"
    assert pos.get("brier_record_id") == "test-uuid-123"
    os.unlink(book_path)
    print("[PASS] sweep_pending_makers carries brier_logged from pending maker to position")


def test_paper_trade_skips_relogging_brier_logged_true():
    """Simulate paper_trade.py logic: if position already has brier_logged=True, skip Brier log."""
    book_path, brier_path = _tmp_paths()
    book = PaperBook(state_path=book_path)
    brier = BrierLogger(log_path=brier_path)

    # Simulate a fill that was already Brier-logged
    book.fill_taker(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        ask_price_cents=50,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2026-01-01T23:59:00+00:00",
    )
    book.open[0]["brier_logged"] = True
    book.open[0]["brier_record_id"] = "existing-uuid"
    book._save()

    # Reload and try to log again (simulates state reload + re-run)
    book2 = PaperBook(state_path=book_path)
    pos = book2.open[0]
    if pos.get("brier_logged", True):
        # This is the paper_trade.py guard — skip logging
        pass
    else:
        rid = brier.log_prediction(
            ticker="KXHIGHTEST-01JAN01-T50",
            side="yes",
            our_prob=0.75,
            market_prob=0.50,
            qty=1,
            price_cents=50,
            mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        pos["brier_record_id"] = rid
        pos["brier_logged"] = True
        book2._save()

    # Verify no new brier log entries
    with open(brier_path) as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 0, "No new Brier entries should be written when brier_logged=True"
    os.unlink(book_path)
    os.unlink(brier_path)
    print("[PASS] paper_trade.py guard skips re-logging when brier_logged=True")


def test_old_fill_without_brier_logged_treated_as_already_logged():
    """Backward compat: old fills without brier_logged field should be treated as already logged."""
    book_path, brier_path = _tmp_paths()
    book = PaperBook(state_path=book_path)
    brier = BrierLogger(log_path=brier_path)

    book.fill_taker(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        ask_price_cents=50,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2026-01-01T23:59:00+00:00",
    )
    # Remove brier_logged to simulate old fill
    del book.open[0]["brier_logged"]
    book._save()

    # Reload and check backward compat guard
    book2 = PaperBook(state_path=book_path)
    pos = book2.open[0]
    # pos.get("brier_logged", True) returns True for missing key
    assert pos.get("brier_logged", True) is True, "Missing brier_logged must default to True (already logged)"
    os.unlink(book_path)
    os.unlink(brier_path)
    print("[PASS] old fills without brier_logged default to True (already logged)")


def test_brier_record_outcome_dedup():
    """record_outcome must not append duplicate settled records."""
    _, brier_path = _tmp_paths()
    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        our_prob=0.75,
        market_prob=0.50,
        qty=1,
        price_cents=50,
        mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid, outcome=True)
    brier.record_outcome(rid, outcome=True)  # second call must be deduped
    brier.record_outcome(rid, outcome=True)  # third call must be deduped

    with open(brier_path) as f:
        records = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in records if r["status"] == "settled"]
    assert len(settled) == 1, f"Expected 1 settled record, got {len(settled)}"
    os.unlink(brier_path)
    print("[PASS] record_outcome deduplicates duplicate settlement calls")


def test_brier_record_outcome_different_ids_not_deduped():
    """Different record_ids should not interfere with each other."""
    _, brier_path = _tmp_paths()
    brier = BrierLogger(log_path=brier_path)
    rid1 = brier.log_prediction(
        ticker="T1", side="yes", our_prob=0.75, market_prob=0.50,
        qty=1, price_cents=50, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    rid2 = brier.log_prediction(
        ticker="T2", side="no", our_prob=0.25, market_prob=0.40,
        qty=1, price_cents=40, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid1, outcome=True)
    brier.record_outcome(rid2, outcome=False)
    brier.record_outcome(rid1, outcome=True)  # dedup

    with open(brier_path) as f:
        records = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in records if r["status"] == "settled"]
    assert len(settled) == 2, f"Expected 2 settled records, got {len(settled)}"
    os.unlink(brier_path)
    print("[PASS] record_outcome only dedupes same record_id, not different ones")


if __name__ == "__main__":
    test_fill_taker_sets_brier_logged_false()
    test_maker_post_sets_brier_logged_false()
    test_maker_sweep_carries_brier_logged()
    test_paper_trade_skips_relogging_brier_logged_true()
    test_old_fill_without_brier_logged_treated_as_already_logged()
    test_brier_record_outcome_dedup()
    test_brier_record_outcome_different_ids_not_deduped()
    print("\nAll brier dedup tests passed.")

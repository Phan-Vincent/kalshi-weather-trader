#!/usr/bin/env python3
"""
tests/test_settle_alias.py — Verify that two positions with the same ticker+side
each get their own distinct settlement records with correct paper_order_id.

Run: python3 tests/test_settle_alias.py
"""
import json
import sys
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path

# Add project paths so we can import trader modules
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.paper_book import PaperBook, STARTING_BANK_CENTS


def test_two_same_ticker_side_positions_get_distinct_settlements():
    """Simulate two YES positions in the same ticker and settle them.
    Assert each settlement record carries its own unique paper_order_id."""

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "paper-book.json"
        book = PaperBook(state_path=state_path)

        # Sanity: start with default bank
        assert book.cash_cents == STARTING_BANK_CENTS

        # Fill two YES positions on the same ticker at different prices
        fill1 = book.fill_taker(
            ticker="KXSTEAKES-25-APR-DENHITHIGH-80",
            side="yes",
            ask_price_cents=45,
            qty=5,
            fair_prob=0.65,
            edge_cents_post_fee=5.0,
            confidence="high",
            market_close_time="2025-04-25T20:00:00+00:00",
            rationale="test pos 1",
        )
        fill2 = book.fill_taker(
            ticker="KXSTEAKES-25-APR-DENHITHIGH-80",
            side="yes",
            ask_price_cents=50,
            qty=3,
            fair_prob=0.60,
            edge_cents_post_fee=3.0,
            confidence="medium",
            market_close_time="2025-04-25T20:00:00+00:00",
            rationale="test pos 2",
        )

        assert fill1["status"] == "filled"
        assert fill2["status"] == "filled"

        id1 = fill1["paper_order_id"]
        id2 = fill2["paper_order_id"]
        assert id1 != id2, "paper_order_ids must be unique"

        # Verify two open positions exist
        assert len(book.open) == 2
        open_ids = [p["paper_order_id"] for p in book.open]
        assert id1 in open_ids and id2 in open_ids

        # Settle the market as YES
        settlements = book.settle_position("KXSTEAKES-25-APR-DENHITHIGH-80", "yes")

        # Must return exactly 2 settlement records
        assert len(settlements) == 2, f"Expected 2 settlements, got {len(settlements)}"

        # Each settlement must carry its own paper_order_id
        settle_ids = [s["paper_order_id"] for s in settlements]
        assert set(settle_ids) == {id1, id2}, (
            f"Settlement IDs {settle_ids} don't match position IDs {{{id1}, {id2}}}"
        )

        # Map settlement to position to ensure 1:1 attribution
        for s in settlements:
            matching = [p for p in book.open + book.closed if p["paper_order_id"] == s["paper_order_id"]]
            assert len(matching) == 1, f"Ambiguous match for {s['paper_order_id']}"

        # Verify closed trades also have distinct IDs
        assert len(book.closed) == 2
        closed_ids = [c["paper_order_id"] for c in book.closed]
        assert set(closed_ids) == {id1, id2}

        # Verify open is empty
        assert len(book.open) == 0

        print("✅ PASS: two positions with same ticker+side each get distinct settlement records")
        return True


def test_settle_paper_matches_by_paper_order_id():
    """Simulate the settle_paper.py matching logic using the fixed code.
    Verify that pre_ctx matching by paper_order_id yields the correct context
    for each settlement, avoiding the old side-only aliasing."""

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "paper-book.json"
        book = PaperBook(state_path=state_path)

        fill1 = book.fill_taker(
            ticker="KXSTEAKES-25-APR-DENHITHIGH-80",
            side="yes",
            ask_price_cents=45,
            qty=5,
            fair_prob=0.65,
            edge_cents_post_fee=5.0,
            confidence="high",
            market_close_time="2025-04-25T20:00:00+00:00",
            rationale="pos 1",
        )
        fill2 = book.fill_taker(
            ticker="KXSTEAKES-25-APR-DENHITHIGH-80",
            side="yes",
            ask_price_cents=50,
            qty=3,
            fair_prob=0.60,
            edge_cents_post_fee=3.0,
            confidence="medium",
            market_close_time="2025-04-25T20:00:00+00:00",
            rationale="pos 2",
        )

        id1 = fill1["paper_order_id"]
        id2 = fill2["paper_order_id"]

        pre_ctx = [dict(p) for p in book.open if p.get("ticker") == "KXSTEAKES-25-APR-DENHITHIGH-80"]
        settlements = book.settle_position("KXSTEAKES-25-APR-DENHITHIGH-80", "yes")

        for s in settlements:
            # This is the fixed matching logic (was side-only before)
            ctx = next((c for c in pre_ctx if c.get("paper_order_id") == s.get("paper_order_id")), {})
            assert ctx.get("paper_order_id") == s.get("paper_order_id")
            assert ctx.get("rationale") in ("pos 1", "pos 2")
            # Ensure no aliasing: the ctx rationale must match the original position
            if s["paper_order_id"] == id1:
                assert ctx["rationale"] == "pos 1"
                assert ctx["avg_entry_cents"] == 45
            elif s["paper_order_id"] == id2:
                assert ctx["rationale"] == "pos 2"
                assert ctx["avg_entry_cents"] == 50

        print("✅ PASS: settle_paper ctx matching by paper_order_id yields correct attribution")
        return True


if __name__ == "__main__":
    ok = test_two_same_ticker_side_positions_get_distinct_settlements()
    ok = test_settle_paper_matches_by_paper_order_id() and ok
    sys.exit(0 if ok else 1)

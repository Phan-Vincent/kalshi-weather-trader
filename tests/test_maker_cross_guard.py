#!/usr/bin/env python3
"""
tests/test_maker_cross_guard.py — Regression for the 2026-07-06 audit finding that a
live maker posts sig.limit_price_cents from the (stale) cycle-start snapshot; if the
book dropped since, that GTC bid can be at/above the fresh ask, cross, and fill as a
TAKER — paying the taker fee the premium model books as 0 and donating the spread.
The live path now skips a post when _maker_would_cross() against the fresh book.

Run: python3 -m pytest tests/test_maker_cross_guard.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import paper_trade as pt


def test_yes_bid_below_ask_is_maker():
    live = {"yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60}
    assert pt._maker_would_cross("yes", 44, live) is False  # 44 < 45 → rests as maker


def test_yes_bid_at_or_above_ask_crosses():
    live = {"yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60}
    assert pt._maker_would_cross("yes", 45, live) is True   # touches the ask → taker
    assert pt._maker_would_cross("yes", 50, live) is True   # through the ask → taker


def test_no_side_uses_no_ask():
    live = {"yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60}
    assert pt._maker_would_cross("no", 59, live) is False
    assert pt._maker_would_cross("no", 60, live) is True


def test_zero_ask_no_liquidity_cannot_cross():
    live = {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0}
    assert pt._maker_would_cross("yes", 90, live) is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

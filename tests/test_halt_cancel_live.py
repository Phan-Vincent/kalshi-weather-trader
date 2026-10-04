#!/usr/bin/env python3
"""Halt-guard kill-chain gap fix (QA 2026-06-25, user-authorized default-on): when a city's circuit
breaker trips, its LIVE Kalshi resting orders must be cancelled — previously only paper makers were
dropped, so a flagged-losing city kept filling adverse. Fail-closed on a list error. Plain python3."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))


class _Risk:
    """Stand-in RiskGate: OKC is halted (mult 0.0), every other city is healthy."""
    def __init__(self, halted="OKC"):
        self.halted = halted
    def get_city_risk_multiplier(self, city):
        return (0.0, "circuit breaker open") if city == self.halted else (1.0, "ok")


def _run(resting, err, halted="OKC"):
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        import trader.orders as orders_mod
        import paper_trade as pt
        from trader.paper_book import PaperBook
        book = PaperBook()
        cancels = []
        o_list, o_cancel = orders_mod.list_resting_orders, orders_mod.cancel_order
        orders_mod.list_resting_orders = lambda prod=False: (resting, err)
        orders_mod.cancel_order = lambda oid, prod=False: cancels.append(oid) or {"_ok": True}
        try:
            n = pt._cancel_halted_city_live_orders(book, _Risk(halted), prod=True)
        finally:
            orders_mod.list_resting_orders, orders_mod.cancel_order = o_list, o_cancel
        return n, cancels
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_halt_cancel_cancels_only_halted_city():
    resting = [
        {"ticker": "KXHIGHTOKC-26JUN25-B95.5", "order_id": "oid-okc", "remaining_count": 7},   # halted
        {"ticker": "KXHIGHTHOU-26JUN25-B99.5", "order_id": "oid-hou", "remaining_count": 5},   # healthy
    ]
    n, cancels = _run(resting, None)
    assert n == 1, n
    assert cancels == ["oid-okc"], f"only the halted city's live order should cancel, got {cancels}"


def test_halt_cancel_fail_closed_on_list_error():
    # a list_resting_orders failure must cancel NOTHING (don't act on an unknown order book).
    resting = [{"ticker": "KXHIGHTOKC-26JUN25-B95.5", "order_id": "oid-okc", "remaining_count": 7}]
    n, cancels = _run(resting, "HTTP 401: simulated list failure")
    assert n == 0 and cancels == [], "list error must cancel nothing (fail-closed)"


def test_halt_cancel_covers_bare_high_series():
    # KXHIGHNY (bare daily-high, no 'T') must be recognized so a halted NYC's LIVE orders are cancelled
    # (the extractor previously returned "" for bare HIGH/LOW series -> silently skipped them).
    from paper_trade import _extract_city_from_ticker
    assert _extract_city_from_ticker("KXHIGHNY-26JUN25-B90.5") == "NY"
    assert _extract_city_from_ticker("KXHIGHMIA-26JUN25-B90.5") == "MIA"
    resting = [{"ticker": "KXHIGHNY-26JUN25-B90.5", "order_id": "oid-ny", "remaining_count": 4}]
    n, cancels = _run(resting, None, halted="NY")
    assert n == 1 and cancels == ["oid-ny"], f"halted NYC bare-high order must be cancelled (n={n}, {cancels})"


if __name__ == "__main__":
    test_halt_cancel_cancels_only_halted_city(); print("[PASS] halt-cancel cancels only the halted city's live order")
    test_halt_cancel_fail_closed_on_list_error(); print("[PASS] halt-cancel fail-closed on list error")
    test_halt_cancel_covers_bare_high_series(); print("[PASS] halt-cancel covers bare HIGH series (KXHIGHNY -> NY)")
    print("\nHalt-cancel tests pass.")

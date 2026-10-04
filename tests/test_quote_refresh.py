#!/usr/bin/env python3
"""Band-keyed quote-staleness refresh (Stream 3, 2026-06-25): cancel resting premium quotes the
ORDER BOOK has moved against — keyed on scanner.premium_quote_price (the same book-derived pricing
the quotes are posted at), NOT the model (which is anti-predictive for the premium edge). Cancel-only,
flag-gated, fail-closed. Plain python3."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from paper_trade import _band_stale, _refresh_stale_paper_makers, _refresh_stale_live_quotes  # noqa: E402

MIA = "KXHIGHTMIA-26JUN25-B90.5"
SFO = "KXHIGHTSFO-26JUN25-B69.5"
BOOK_IN_BAND = {"yes_bid": 30, "yes_ask": 34}   # premium_quote_price -> ("yes", 30); spread 4, in-band


def test_band_stale_decisions():
    assert _band_stale("yes", 30, BOOK_IN_BAND)[0] is False                     # resting == fresh -> hold
    assert _band_stale("yes", 35, BOOK_IN_BAND)[0] is True                      # 5c richer than book -> stale
    assert _band_stale("no", 30, BOOK_IN_BAND)[0] is True                       # strategy wants YES now -> stale
    assert _band_stale("yes", 30, {"yes_bid": 10, "yes_ask": 14})[0] is True    # two-sided, not in band -> stale
    assert _band_stale("yes", 30, {"yes_bid": 30, "yes_ask": 0})[0] is False    # one-sided snapshot -> hold (don't act)


def _book_in(tmpd):
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    from trader.paper_book import PaperBook
    return PaperBook()


def _restore(prev_env, prev_flag, tmpd):
    for k, v in (("KALSHI_WEATHER_STATE_DIR", prev_env), ("KALSHI_WEATHER_QUOTE_REFRESH", prev_flag)):
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    shutil.rmtree(tmpd, ignore_errors=True)


def test_paper_refresh_drops_book_stale_holds_fresh():
    prev_env, prev_flag = os.environ.get("KALSHI_WEATHER_STATE_DIR"), os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_QUOTE_REFRESH"] = "1"
    try:
        import paper_trade as pt
        book = _book_in(tmpd)
        book.pending_makers = [
            {"ticker": MIA, "side": "yes", "limit_price_cents": 35, "qty": 5},   # 5c above book -> stale
            {"ticker": SFO, "side": "yes", "limit_price_cents": 30, "qty": 5},   # at fresh price  -> held
        ]
        orig = pt.fetch_live_markets_for_tickers
        pt.fetch_live_markets_for_tickers = lambda tks: {MIA: dict(BOOK_IN_BAND), SFO: dict(BOOK_IN_BAND)}
        try:
            n = pt._refresh_stale_paper_makers(book)
        finally:
            pt.fetch_live_markets_for_tickers = orig
        tickers = [pm["ticker"] for pm in book.pending_makers]
        assert n == 1, n
        assert MIA not in tickers, "book-stale maker must be dropped"
        assert SFO in tickers, "fresh maker must be held"
    finally:
        _restore(prev_env, prev_flag, tmpd)


def test_refresh_noop_when_flag_off():
    prev_env, prev_flag = os.environ.get("KALSHI_WEATHER_STATE_DIR"), os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_QUOTE_REFRESH"] = "0"   # flag OFF -> no fetch, no drop
    try:
        import paper_trade as pt
        book = _book_in(tmpd)
        book.pending_makers = [{"ticker": MIA, "side": "yes", "limit_price_cents": 99, "qty": 5}]
        called = {"n": 0}
        orig = pt.fetch_live_markets_for_tickers
        pt.fetch_live_markets_for_tickers = lambda tks: called.__setitem__("n", called["n"] + 1) or {}
        try:
            assert pt._refresh_stale_paper_makers(book) == 0
        finally:
            pt.fetch_live_markets_for_tickers = orig
        assert len(book.pending_makers) == 1 and called["n"] == 0, "flag off -> maker untouched, no fetch"
    finally:
        _restore(prev_env, prev_flag, tmpd)


def test_live_refresh_fail_closed_on_list_error():
    prev_env, prev_flag = os.environ.get("KALSHI_WEATHER_STATE_DIR"), os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_QUOTE_REFRESH"] = "1"
    try:
        import trader.orders as orders_mod
        from trader.risk import RiskGate
        import paper_trade as pt
        book = _book_in(tmpd)
        o_list, o_cancel = orders_mod.list_resting_orders, orders_mod.cancel_order
        cancels = []
        orders_mod.list_resting_orders = lambda prod=False: ([], "HTTP 401: simulated list failure")
        orders_mod.cancel_order = lambda oid, prod=False: cancels.append(oid) or {"_ok": True}
        try:
            n = pt._refresh_stale_live_quotes(book, RiskGate(), prod=True)
        finally:
            orders_mod.list_resting_orders, orders_mod.cancel_order = o_list, o_cancel
        assert n == 0 and cancels == [], "list failure must cancel nothing (fail-closed)"
    finally:
        _restore(prev_env, prev_flag, tmpd)


def test_live_refresh_cancels_real_shaped_stale_order():
    # End-to-end: a real-shaped Kalshi NO order (no_price=40) whose book now prices a fresh NO at 32c
    # (8c richer -> stale) must be cancelled. Exercises _resting_price (side-aware no_price) + cancel.
    prev_env, prev_flag = os.environ.get("KALSHI_WEATHER_STATE_DIR"), os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_QUOTE_REFRESH"] = "1"
    try:
        import trader.orders as orders_mod
        import paper_trade as pt
        book = _book_in(tmpd)

        class _Risk:
            def get_city_risk_multiplier(self, city):
                return (1.0, "ok")

        tk = "KXHIGHTMIA-26JUN25-B90.5"
        resting = [{"order_id": "o1", "ticker": tk, "side": "no", "no_price": 40, "remaining_count": 5}]
        cancels = []
        o_list, o_cancel, o_fetch = orders_mod.list_resting_orders, orders_mod.cancel_order, pt.fetch_live_markets_for_tickers
        orders_mod.list_resting_orders = lambda prod=False: (resting, None)
        orders_mod.cancel_order = lambda oid, prod=False: cancels.append(oid) or {"_ok": True}
        pt.fetch_live_markets_for_tickers = lambda tks: {tk: {"yes_bid": 65, "yes_ask": 68}}   # no_bid 32 in-band
        try:
            n = pt._refresh_stale_live_quotes(book, _Risk(), prod=True)
        finally:
            orders_mod.list_resting_orders, orders_mod.cancel_order, pt.fetch_live_markets_for_tickers = o_list, o_cancel, o_fetch
        assert n == 1 and cancels == ["o1"], f"real-shaped stale NO order must be cancelled (n={n}, cancels={cancels})"
    finally:
        _restore(prev_env, prev_flag, tmpd)


if __name__ == "__main__":
    test_band_stale_decisions();              print("[PASS] band-stale: hold / too-rich / side-flip / not-quotable / one-sided")
    test_live_refresh_cancels_real_shaped_stale_order(); print("[PASS] live refresh cancels a real-shaped (no_price) book-stale order")
    test_paper_refresh_drops_book_stale_holds_fresh(); print("[PASS] paper refresh drops book-stale maker, holds fresh")
    test_refresh_noop_when_flag_off();        print("[PASS] flag off -> no-op (no fetch)")
    test_live_refresh_fail_closed_on_list_error(); print("[PASS] live refresh fail-closed on list error")
    print("\nBand-keyed quote-refresh tests pass.")

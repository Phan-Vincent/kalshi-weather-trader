#!/usr/bin/env python3
"""The live book must contain ONLY weather positions. The Kalshi position feed is account-wide, so it
sweeps in non-weather contracts (e.g. the legacy KXUSAIRANAGREEMENT bet). Those must not enter the
weather arm's book — they pad open[]/fills_total and surface as the strategy's unrealized P&L on the
dashboard (positions-live.json + the markout log derive from book.open). The daily/weekly $ stop already
excludes them (test_weather_budget_filter.py); this pins the BOOK-side exclusion in sync_live_book.

Escape hatch KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS=1 restores the old all-positions book. (2026-06-30)"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import sync_live_positions as slp  # noqa: E402
from sync_live_positions import sync_live_book  # noqa: E402

WEATHER = "KXHIGHTNOLA-26JUN30-B92.5"
IRAN = "KXUSAIRANAGREEMENT-27-26SEP"


def _pos(ticker, qty, side="yes", cost_cents=3600, rpnl=0, exposure=5000):
    return {
        "ticker": ticker, "side": side, "qty": qty, "position_fp": qty if side == "yes" else -qty,
        "total_cost_cents": cost_cents, "realized_pnl_cents": rpnl,
        "market_exposure_cents": exposure, "fees_paid_cents": 0,
        "resting_orders": 0, "last_updated": "2026-06-30T00:00:00Z",
    }


def _state(positions, available=45658, portfolio=15087):
    return {
        "available_cents": available, "portfolio_cents": portfolio,
        "total_cents": available + portfolio, "positions": positions,
        "num_positions": len(positions), "total_deployed_cents": 0,
        "timestamp_utc": "2026-06-30T22:00:00+00:00",
    }


def _run(state, env=None, old_book=None):
    """Run sync_live_book against a temp state dir; return the written book. Guards that the
    settlements CLI is never reached (network) by stubbing it to raise."""
    orig_settlements = slp.get_kalshi_settlements
    slp.get_kalshi_settlements = lambda *a, **k: (_ for _ in ()).throw(AssertionError("CLI not allowed"))
    saved_env = {k: os.environ.get(k) for k in ("KALSHI_WEATHER_STATE_DIR", "KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS")}
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = d
            os.environ.pop("KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS", None)
            for k, v in (env or {}).items():
                os.environ[k] = v
            if old_book is not None:
                (Path(d) / "paper-book.json").write_text(json.dumps(old_book))
            sync_live_book(state)
            book = json.loads((Path(d) / "paper-book.json").read_text())
            log = Path(d) / "settlement-log.jsonl"
            book["_settlement_log"] = log.read_text() if log.exists() else ""
            return book
    finally:
        slp.get_kalshi_settlements = orig_settlements
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_iran_excluded_from_open_book():
    book = _run(_state([_pos(WEATHER, 6), _pos(IRAN, 173, side="no", cost_cents=11734)]))
    tickers = {p["ticker"] for p in book["open"]}
    assert WEATHER in tickers, "weather position must stay in the book"
    assert IRAN not in tickers, "non-weather position must be excluded from open[]"
    assert book["fills_total"] == 1, "fills_total must count weather opens only"


def test_escape_hatch_keeps_all_tickers():
    book = _run(_state([_pos(WEATHER, 6), _pos(IRAN, 173, side="no", cost_cents=11734)]),
                env={"KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS": "1"})
    tickers = {p["ticker"] for p in book["open"]}
    assert IRAN in tickers and WEATHER in tickers, "escape hatch must restore all-positions book"
    assert book["fills_total"] == 2


def test_legacy_iran_in_old_book_not_settled():
    # Iran sat in the prior book's open[]; it's still open on Kalshi. The filter must drop it from
    # BOTH prior and current state so it's neither carried as open nor misread as a disappeared
    # settlement (which would fabricate a settlement-log row + feed the kill-switch).
    old = {"version": 2, "open": [_pos(IRAN, 173, side="no", cost_cents=11734),
                                  _pos(WEATHER, 6)], "closed": [], "pending_settlement": []}
    book = _run(_state([_pos(WEATHER, 6), _pos(IRAN, 173, side="no", cost_cents=11734)]), old_book=old)
    assert {p["ticker"] for p in book["open"]} == {WEATHER}
    assert IRAN not in book["_settlement_log"], "legacy non-weather position must never be logged as settled"
    assert book["trades_closed"] == 0 and book["realized_pnl_cents"] == 0


if __name__ == "__main__":
    test_iran_excluded_from_open_book();        print("[PASS] Iran excluded from open book")
    test_escape_hatch_keeps_all_tickers();      print("[PASS] escape hatch keeps all tickers")
    test_legacy_iran_in_old_book_not_settled(); print("[PASS] legacy Iran in old book not mis-settled")
    print("\nAll live-book-weather-only tests pass.")

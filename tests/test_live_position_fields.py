#!/usr/bin/env python3
"""Live position-field accuracy (2026-06-25 QA audit). Two pre-existing bugs in
bin/sync_live_positions.py corrupted the per-contract entry/qty that flow into the live
paper-book, settlement-log.jsonl, and the dashboards:

  1. entry_price_cents impossible (>100¢). The per-contract entry was derived from the
     EVENT-level total_cost_dollars (shared across every strike of an event) divided by ONE
     market's qty → multi-strike events produced entries like 194¢. Fix: derive from the
     PER-MARKET total_traded_dollars, clamped [0,100].
  2. Fractional qty truncation. qty = abs(int(position_fp)) truncated a genuine 0<|fp|<1
     holding to 0, dropping it from the open book. Fix: round(), floored to 1 for |fp|>=0.01;
     fp=0 (a settled position) must STAY qty=0 so settlement detection is preserved.

Pure-python3, no pytest. Run: python3 tests/test_live_position_fields.py"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from bin.sync_live_positions import _build_live_positions, sync_live_book

EVENT = "KXHIGHTNOLA-26JUN25"


def _ks(positions, ts="2026-06-25T00:00:00+00:00"):
    return {"available_cents": 100000, "portfolio_cents": 0, "total_cents": 100000,
            "positions": positions, "num_positions": len(positions), "timestamp_utc": ts}


def test_per_market_entry_not_event_shared():
    # A 2-strike event: event total_cost = $1.94 (the SUM of both strikes); each strike traded
    # $0.97 for 1 contract. The old code stamped 194¢ on EACH strike (event cost / 1 qty).
    events = [{"event_ticker": EVENT, "total_cost_dollars": 1.94}]
    positions = [
        {"ticker": f"{EVENT}-B91.5", "position_fp": 1.0, "total_traded_dollars": 0.97,
         "market_exposure_dollars": 0.50, "realized_pnl_dollars": 0.0, "last_updated_ts": "t"},
        {"ticker": f"{EVENT}-B88.5", "position_fp": 1.0, "total_traded_dollars": 0.97,
         "market_exposure_dollars": 0.50, "realized_pnl_dollars": 0.0, "last_updated_ts": "t"},
    ]
    built, _ = _build_live_positions(positions, events)
    # Per-MARKET cost (97¢), not the event-shared 194¢.
    assert [p["total_cost_cents"] for p in built] == [97, 97], built

    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        sync_live_book(_ks(built))
        book = json.loads((Path(tmpd) / "paper-book.json").read_text())
        entries = {p["ticker"]: p["entry_price_cents"] for p in book["open"]}
        assert len(entries) == 2, book["open"]
        for tk, e in entries.items():
            assert 0 <= e <= 100, f"{tk} entry {e}¢ must be a real per-contract price"
            assert e == 97, f"{tk} entry should be the per-market 97¢, got {e}"
    finally:
        _restore_env(prev_env)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_entry_clamped_to_100_in_sync():
    # Even if a malformed cost slips through, sync must never emit a >100¢ entry.
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        bad = {"ticker": f"{EVENT}-B91.5", "side": "yes", "qty": 1, "total_cost_cents": 194,
               "realized_pnl_cents": 0, "market_exposure_cents": 50, "last_updated": "t"}
        sync_live_book(_ks([bad]))
        book = json.loads((Path(tmpd) / "paper-book.json").read_text())
        assert len(book["open"]) == 1, book["open"]
        op = book["open"][0]
        assert op["entry_price_cents"] == 100 and op["avg_entry_cents"] == 100, op
    finally:
        _restore_env(prev_env)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_fractional_qty_not_truncated():
    # 0<|fp|<1 is a genuine holding — round()/floor-1 keeps it, int() would have dropped it.
    events = [{"event_ticker": EVENT, "total_cost_dollars": 0.40}]
    positions = [{"ticker": f"{EVENT}-B91.5", "position_fp": 0.4, "total_traded_dollars": 0.40,
                  "market_exposure_dollars": 0.20, "realized_pnl_dollars": 0.0, "last_updated_ts": "t"}]
    built, _ = _build_live_positions(positions, events)
    assert len(built) == 1 and built[0]["qty"] == 1, built  # NOT abs(int(0.4)) == 0

    # And it survives end-to-end into the open book.
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        sync_live_book(_ks(built))
        book = json.loads((Path(tmpd) / "paper-book.json").read_text())
        assert len(book["open"]) == 1, "sub-1 holding must not be dropped from the open book"
    finally:
        _restore_env(prev_env)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_settled_position_stays_qty_zero():
    # fp=0 with residual traded $ is a SETTLED position Kalshi still reports for ~one cycle. It must
    # NOT be floored to qty=1 — sync_live_book relies on qty<=0 to record the settlement.
    events = [{"event_ticker": EVENT, "total_cost_dollars": 0.97}]
    positions = [{"ticker": f"{EVENT}-B91.5", "position_fp": 0.0, "total_traded_dollars": 0.97,
                  "market_exposure_dollars": 0.0, "realized_pnl_dollars": -2.60, "last_updated_ts": "t"}]
    built, _ = _build_live_positions(positions, events)
    assert len(built) == 1 and built[0]["qty"] == 0, built


def _restore_env(prev_env):
    if prev_env is None:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
    else:
        os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env


if __name__ == "__main__":
    test_per_market_entry_not_event_shared()
    print("[PASS] per-market entry (≤100¢) instead of event-shared cost — multi-strike event")
    test_entry_clamped_to_100_in_sync()
    print("[PASS] sync clamps a malformed >100¢ entry to 100¢")
    test_fractional_qty_not_truncated()
    print("[PASS] fractional 0<|fp|<1 holding kept (qty=1), survives into the open book")
    test_settled_position_stays_qty_zero()
    print("[PASS] settled fp=0 position stays qty=0 (settlement detection preserved)")
    print("\nLive position-field accuracy tests pass.")

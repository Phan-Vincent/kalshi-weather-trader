#!/usr/bin/env python3
"""Live settlement reconciliation (fill-quality deep-dive, 2026-06-26): a position Kalshi PURGES
after settlement (gone from the portfolio before a sync sees qty=0) must still have its realized P&L
captured — settlement-feed revenue − our cost basis — written to closed[] + settlement-log and fed to
the $ kill-switch, not silently dropped. And a position that merely vanished without a settlement
(e.g. a manual exit) must NOT be fabricated. Plain python3."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

TK = "KXHIGHNY-26JUN25-B81.5"


def _prev_book(tmpd, opens):
    Path(tmpd, "paper-book.json").write_text(json.dumps({
        "version": 2, "cash_cents": 50000, "open": opens, "closed": [], "pending_makers": [],
        "filled_orders": [], "fills_total": len(opens),
    }))


def _kstate(positions):
    return {"available_cents": 50000, "portfolio_cents": 0, "positions": positions,
            "timestamp_utc": "2026-06-26T13:00:00+00:00"}


def _run(kstate, settlements):
    import sync_live_positions as sp
    orig = sp.get_kalshi_settlements
    sp.get_kalshi_settlements = lambda limit=200: settlements
    try:
        sp.sync_live_book(kstate)
    finally:
        sp.get_kalshi_settlements = orig


def test_disappeared_settled_position_is_reconciled():
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        # last cycle we held 4 YES @ 43c (cost 172c); Kalshi has since PURGED it (not in positions).
        _prev_book(tmpd, [{"ticker": TK, "side": "yes", "qty": 4, "entry_price_cents": 43,
                           "avg_entry_cents": 43, "cost_cents": 172, "opened_utc": "2026-06-24T21:00:00Z"}])
        sett = {TK: {"market_result": "yes", "revenue_cents": 400, "settled_time": "2026-06-26T12:01:00Z"}}
        _run(_kstate([]), sett)                            # positions empty → the ticker disappeared
        book = json.load(open(Path(tmpd, "paper-book.json")))
        closed = book["closed"]
        assert len(closed) == 1, closed
        assert closed[0]["ticker"] == TK
        assert closed[0]["pnl_cents"] == 400 - 172, closed[0]["pnl_cents"]      # 228 = revenue − cost
        assert closed[0]["source"] == "live_settlement_reconcile"
        log = Path(tmpd, "settlement-log.jsonl")
        assert log.is_file() and TK in log.read_text(), "must be written to settlement-log"
        # the $ stop must have been fed (risk-state reflects the realized loss/gain)
        assert Path(tmpd, "risk-state.json").is_file(), "RiskGate must have been fed"
        # re-run: must NOT double-count
        _run(_kstate([]), sett)
        book2 = json.load(open(Path(tmpd, "paper-book.json")))
        assert len(book2["closed"]) == 1, "re-run double-counted the settlement"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_disappeared_but_unsettled_is_not_fabricated():
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        _prev_book(tmpd, [{"ticker": TK, "side": "yes", "qty": 4, "entry_price_cents": 43,
                           "avg_entry_cents": 43, "cost_cents": 172, "opened_utc": ""}])
        _run(_kstate([]), {})                              # empty/failed settlement feed → can't confirm
        book = json.load(open(Path(tmpd, "paper-book.json")))
        assert book["closed"] == [], "disappeared-but-unsettled must not be fabricated"
        assert any(p["ticker"] == TK for p in book["pending_settlement"]), "must carry forward for retry, not drop"
        assert not any(p["ticker"] == TK for p in book["open"]), "pending must NOT pollute open[] (equity/cap)"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_feed_failure_carries_reconciled_forward_then_retry_succeeds():
    # If the RiskGate feed THROWS, a disappeared+settled position must NOT be lost — carry it forward
    # so the next sync retries (QA HIGH: previously it was dropped from BOTH closed[] and open[] ->
    # permanently lost, and the $ stop never saw the loss). Retry with a working feed then reconciles it.
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        _prev_book(tmpd, [{"ticker": TK, "side": "yes", "qty": 4, "entry_price_cents": 43,
                           "avg_entry_cents": 43, "cost_cents": 172, "opened_utc": ""}])
        sett = {TK: {"market_result": "yes", "revenue_cents": 400, "settled_time": "2026-06-26T12:01:00Z"}}

        import trader.risk as risk_mod

        class _Boom:                                   # RiskGate whose feed throws
            def __init__(self, *a, **k): pass
            def record_settlements_batch(self, *a, **k): raise RuntimeError("feed boom")
            def check_budget(self, *a, **k): return (True, None)

        orig = risk_mod.RiskGate
        risk_mod.RiskGate = _Boom
        try:
            _run(_kstate([]), sett)                     # feed fails this cycle
        finally:
            risk_mod.RiskGate = orig
        book = json.load(open(Path(tmpd, "paper-book.json")))
        assert book["closed"] == [], "feed failed -> must NOT commit to closed[]"
        assert any(p["ticker"] == TK for p in book["pending_settlement"]), "must carry reconciled forward for retry"
        # next cycle, with the real (working) feed, reconciles it exactly once
        _run(_kstate([]), sett)
        book2 = json.load(open(Path(tmpd, "paper-book.json")))
        assert sum(1 for c in book2["closed"] if c["ticker"] == TK) == 1, "retry must reconcile exactly once"
        assert not any(p["ticker"] == TK for p in book2["open"]), "settled -> removed from open[]"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_qty0_rpnl0_ghost_carried_forward():
    # Kalshi reports a just-settled position as qty=0 + realized_pnl_cents=0 for >=1 cycle before the
    # real number. It must be carried forward (not dropped) so it stays detectable until it settles.
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        _prev_book(tmpd, [{"ticker": TK, "side": "yes", "qty": 4, "entry_price_cents": 43,
                           "avg_entry_cents": 43, "cost_cents": 172, "opened_utc": ""}])
        ghost = [{"ticker": TK, "qty": 0, "realized_pnl_cents": 0}]   # qty=0 + rpnl=0 ghost (still reported)
        _run(_kstate(ghost), {})
        book = json.load(open(Path(tmpd, "paper-book.json")))
        assert book["closed"] == [], "rpnl=0 ghost is not a settlement yet"
        assert any(p["ticker"] == TK for p in book["pending_settlement"]), "qty=0+rpnl=0 ghost must be carried forward"
        assert not any(p["ticker"] == TK for p in book["open"]), "pending must NOT pollute open[]"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_feed_failure_qty0_ghost_carried_then_purged_is_reconciled():
    # qty=0 ghost WITH realized P&L (source=live_sync): if the feed FAILS and Kalshi then PURGES it, it
    # must be carried (pending_settlement) and reconciled via the settlements feed next cycle (QA HIGH:
    # previously only the disappeared path was carried on feed-fail, so this ghost was permanently lost).
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        _prev_book(tmpd, [{"ticker": TK, "side": "yes", "qty": 4, "entry_price_cents": 43,
                           "avg_entry_cents": 43, "cost_cents": 172, "opened_utc": ""}])
        import trader.risk as risk_mod

        class _Boom:
            def __init__(self, *a, **k): pass
            def record_settlements_batch(self, *a, **k): raise RuntimeError("feed boom")
            def check_budget(self, *a, **k): return (True, None)

        orig = risk_mod.RiskGate
        risk_mod.RiskGate = _Boom
        try:
            _run(_kstate([{"ticker": TK, "qty": 0, "realized_pnl_cents": -172}]), {})   # ghost, feed fails
        finally:
            risk_mod.RiskGate = orig
        book = json.load(open(Path(tmpd, "paper-book.json")))
        assert book["closed"] == [], "feed failed -> not committed"
        assert any(p["ticker"] == TK for p in book["pending_settlement"]), "qty=0 ghost must be carried on feed-fail"
        # cycle 2: Kalshi PURGES it (positions=[]); settlement feed confirms; feed works -> reconciled once
        _run(_kstate([]), {TK: {"market_result": "no", "revenue_cents": 0, "settled_time": "2026-06-26T12:01:00Z"}})
        book2 = json.load(open(Path(tmpd, "paper-book.json")))
        assert sum(1 for c in book2["closed"] if c["ticker"] == TK) == 1, "purged ghost reconciled exactly once"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_disappeared_settled_position_is_reconciled(); print("[PASS] disappeared settled position reconciled (revenue − cost − fees), logged, fed, no double-count")
    test_feed_failure_qty0_ghost_carried_then_purged_is_reconciled(); print("[PASS] feed-fail qty=0 ghost carried -> purge -> reconciled once (QA HIGH)")
    test_disappeared_but_unsettled_is_not_fabricated(); print("[PASS] disappeared-but-unsettled is not fabricated, carried forward for retry")
    test_feed_failure_carries_reconciled_forward_then_retry_succeeds(); print("[PASS] feed-failure carries reconciled forward; retry reconciles exactly once")
    test_qty0_rpnl0_ghost_carried_forward(); print("[PASS] qty=0+rpnl=0 ghost carried forward, not dropped")
    print("\nLive settlement-reconcile tests pass.")

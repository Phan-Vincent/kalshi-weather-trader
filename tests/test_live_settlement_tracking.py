#!/usr/bin/env python3
"""Live realized-P&L tracking (2026-06-25). sync_live_book must record each SETTLED live position
into settlement-log.jsonl + closed[], and preserve closed[] across cycles. Kalshi keeps a settled
position for ~one cycle reporting qty=0 + realized_pnl_cents (its net settled P&L) before clearing
it; sync captures that (deduped by ticker) using the previous cycle's open snapshot for the
original side/qty. Before this fix, sync hard-reset closed=[] every cycle and settle_paper crashed
on the synced schema, so the live arm's realized P&L was never tracked (the fill-quality watchdog
and the Stage-2 forecast→live gate had no realized signal). Driven by Kalshi's own realized number,
so no settle_paper involvement and no risk double-count. Runs under plain python3."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

TK = "KXHIGHTSFO-26JUN24-B67.5"


def _ks(positions, ts):
    return {"available_cents": 100000, "portfolio_cents": 0, "total_cents": 100000,
            "positions": positions, "num_positions": len(positions), "timestamp_utc": ts}


def _settled_pos(ts_tag):
    # Kalshi reports a settled position with qty=0 and its net realized P&L; side flips to the
    # cleared default — sync must recover the ORIGINAL side/qty from the previous open snapshot.
    return {"ticker": TK, "side": "no", "qty": 0, "total_cost_cents": 260,
            "realized_pnl_cents": -260, "market_exposure_cents": 0, "last_updated": ts_tag}


def test_live_settlement_captured_and_deduped():
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        from bin.sync_live_positions import sync_live_book
        bookp, logp = Path(tmpd) / "paper-book.json", Path(tmpd) / "settlement-log.jsonl"

        # Cycle 1: one OPEN live YES position (5 @ 52c), nothing settled.
        bookp.write_text(json.dumps({"open": [{
            "ticker": TK, "side": "yes", "qty": 5,
            "entry_price_cents": 52, "avg_entry_cents": 52, "synced_from_kalshi": True}],
            "closed": []}))
        sync_live_book(_ks([{"ticker": TK, "side": "yes", "qty": 5, "total_cost_cents": 260,
                             "realized_pnl_cents": 0, "market_exposure_cents": 260,
                             "last_updated": "t1"}], "2026-06-24T18:00:00+00:00"))
        b1 = json.loads(bookp.read_text())
        assert len(b1["closed"]) == 0 and len(b1["open"]) == 1, b1
        assert not logp.exists(), "nothing settled yet → no settlement-log"

        # Cycle 2: the position SETTLED at a -260c loss (Kalshi: qty=0, realized=-260).
        sync_live_book(_ks([_settled_pos("t2")], "2026-06-25T00:00:00+00:00"))
        b2 = json.loads(bookp.read_text())
        assert len(b2["open"]) == 0, b2["open"]                         # settled → not open (no qty=0 ghost)
        assert len(b2["closed"]) == 1, b2["closed"]
        c = b2["closed"][0]
        assert c["ticker"] == TK and c["pnl_cents"] == -260, c
        assert c["side"] == "yes" and c["qty"] == 5, c                  # ORIGINAL side/qty, not the cleared qty=0
        assert b2["realized_pnl_cents"] == -260, b2["realized_pnl_cents"]
        recs = [json.loads(x) for x in logp.read_text().splitlines() if x.strip()]
        assert len(recs) == 1 and recs[0]["pnl_cents"] == -260 and recs[0]["side"] == "yes", recs

        # Cycle 3: Kalshi still reports it one more cycle — must NOT re-log or duplicate (dedup).
        sync_live_book(_ks([_settled_pos("t3")], "2026-06-25T06:00:00+00:00"))
        recs2 = [json.loads(x) for x in logp.read_text().splitlines() if x.strip()]
        assert len(recs2) == 1, f"settlement must log exactly once, got {len(recs2)}"
        assert len(json.loads(bookp.read_text())["closed"]) == 1, "no duplicate in closed[]"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


def test_gap_settlement_logged_flagged():
    # A position settled with NO prior open snapshot (fill+settle within one sync gap, or Kalshi's
    # qty→0-before-realized lag) is now WRITTEN to the settlement-log as a CLEARLY-FLAGGED record
    # (gap_settled:true, qty=0/entry=None, source live_gap) — so total-P&L/win-rate is COMPLETE while the
    # band + per-contract analyses defensively skip the attribution-less row (validation 2026-06-28).
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        from bin.sync_live_positions import sync_live_book
        bookp, logp = Path(tmpd) / "paper-book.json", Path(tmpd) / "settlement-log.jsonl"
        bookp.write_text(json.dumps({"open": [], "closed": []}))      # no prior snapshot for TK
        sync_live_book(_ks([{"ticker": TK, "side": "no", "qty": 0, "total_cost_cents": 260,
                             "realized_pnl_cents": 340, "market_exposure_cents": 0,
                             "last_updated": "t"}], "2026-06-25T00:00:00+00:00"))
        rows = [json.loads(l) for l in logp.read_text().splitlines() if l.strip()] if logp.exists() else []
        gap = [r for r in rows if r.get("ticker") == TK]
        assert gap and gap[0].get("gap_settled") is True and gap[0].get("entry_cents") is None, gap
        assert gap[0].get("source") == "live_gap" and gap[0].get("pnl_cents") == 340, gap
        assert all(r.get("gap_settled") for r in json.loads(bookp.read_text())["closed"]), \
            "closed[] holds gap markers only"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


def test_settle_paper_skips_synced_live_positions():
    # settle_paper must SKIP synced_from_kalshi (live) positions — the synced schema would KeyError
    # in settle_position and double-count the kill-switch. It must never even fetch their markets.
    import importlib
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        Path(tmpd, "paper-book.json").write_text(json.dumps({
            "open": [{"ticker": TK, "side": "yes", "qty": 5, "entry_price_cents": 52,
                      "avg_entry_cents": 52, "cost_cents": 260, "pnl_cents": 0, "fair_prob": 0.5,
                      "mode": "live", "order_id": "x", "opened_utc": "t", "synced_from_kalshi": True}],
            "closed": [], "cash_cents": 100000, "starting_bank_cents": 100000}))
        import bin.settle_paper as sp
        importlib.reload(sp)                                          # re-read STATE_DIR from env
        called = []
        sp.fetch_market = lambda tk: (called.append(tk), {
            "status": "finalized", "result": "no",
            "close_time": "2026-06-24T20:00:00Z", "expiration_time": "2026-06-24T20:00:00Z"})[1]
        rc = sp.main([])                                              # isolate from pytest's argv
        assert rc == 0, rc
        assert called == [], f"settle_paper must SKIP synced live positions; it fetched {called}"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


def test_live_stop_fed_realized_only():
    # Option B: the live daily/weekly $ kill-switch is fed by REALIZED settlements only — an OPEN
    # position's mark swing must NOT move the budget counter; a SETTLED loss MUST.
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        from bin.sync_live_positions import sync_live_book
        from trader.risk import RiskGate
        bookp = Path(tmpd) / "paper-book.json"
        # Cycle 1: OPEN position marked DOWN (exposure 100 vs cost 260), realized still 0.
        bookp.write_text(json.dumps({"open": [{"ticker": TK, "side": "yes", "qty": 5,
            "entry_price_cents": 52, "avg_entry_cents": 52, "synced_from_kalshi": True}], "closed": []}))
        sync_live_book(_ks([{"ticker": TK, "side": "yes", "qty": 5, "total_cost_cents": 260,
            "realized_pnl_cents": 0, "market_exposure_cents": 100, "last_updated": "t1"}],
            "2026-06-25T00:00:00+00:00"))
        assert RiskGate(_state_dir=Path(tmpd))._today_pnl_cents == 0, "open mark must NOT feed the $ stop"
        # Cycle 2: the position SETTLES at -260c realized → the $ stop must now see -260.
        sync_live_book(_ks([_settled_pos("t2")], "2026-06-25T01:00:00+00:00"))
        assert RiskGate(_state_dir=Path(tmpd))._today_pnl_cents == -260, "realized loss must feed the $ stop"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


def test_gap_settled_feeds_stop_and_logged_once():
    # A gap-settled position (qty<=0 + realized, NO prior open snapshot) feeds the $ kill-switch AND is
    # now written to the settlement-log as a flagged record; deduped so it both feeds AND logs once.
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        from bin.sync_live_positions import sync_live_book
        from trader.risk import RiskGate
        bookp, logp = Path(tmpd) / "paper-book.json", Path(tmpd) / "settlement-log.jsonl"
        bookp.write_text(json.dumps({"open": [], "closed": []}))      # no prior snapshot for TK
        gap = [{"ticker": TK, "side": "no", "qty": 0, "total_cost_cents": 260,
                "realized_pnl_cents": -180, "market_exposure_cents": 0, "last_updated": "t"}]
        sync_live_book(_ks(gap, "2026-06-25T00:00:00+00:00"))
        assert RiskGate(_state_dir=Path(tmpd))._today_pnl_cents == -180, "gap-settled loss must feed the $ stop"
        rows = [json.loads(l) for l in logp.read_text().splitlines() if l.strip()] if logp.exists() else []
        assert len(rows) == 1 and rows[0].get("gap_settled") and rows[0].get("source") == "live_gap", rows
        c = json.loads(bookp.read_text())["closed"]
        assert len(c) == 1 and c[0].get("gap_settled") and c[0]["pnl_cents"] == -180, c  # dedup marker
        sync_live_book(_ks(gap, "2026-06-25T01:00:00+00:00"))        # still reported next cycle
        assert RiskGate(_state_dir=Path(tmpd))._today_pnl_cents == -180, "gap-settled fed exactly once (dedup)"
        rows2 = [json.loads(l) for l in logp.read_text().splitlines() if l.strip()]
        assert len(rows2) == 1, "gap-settled logged exactly once (dedup), not re-logged next cycle"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


def test_feed_failure_leaves_dedup_uncommitted():
    # If the RiskGate feed raises, closed[]/settlement-log must NOT be committed — so the settlement is
    # re-captured + re-fed next cycle and its loss is never masked from the stop (audit HIGH).
    prev_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    try:
        import bin.sync_live_positions as slp
        import trader.risk as risk_mod
        bookp, logp = Path(tmpd) / "paper-book.json", Path(tmpd) / "settlement-log.jsonl"
        bookp.write_text(json.dumps({"open": [{"ticker": TK, "side": "yes", "qty": 5,
            "entry_price_cents": 52, "avg_entry_cents": 52, "synced_from_kalshi": True}], "closed": []}))
        orig = risk_mod.RiskGate
        class _Boom:                                                  # force the feed to raise
            def __init__(self, *a, **k):
                raise RuntimeError("simulated feed failure")
        risk_mod.RiskGate = _Boom
        try:
            slp.sync_live_book(_ks([_settled_pos("t2")], "2026-06-25T00:00:00+00:00"))
        finally:
            risk_mod.RiskGate = orig
        b = json.loads(bookp.read_text())
        assert b["closed"] == [], f"feed failure must NOT commit closed[], got {b['closed']}"
        assert (not logp.exists()) or logp.read_text().strip() == "", "feed failure must not write the log"
        assert len(b["open"]) == 0, "open book is still refreshed from Kalshi on feed failure"
    finally:
        if prev_env is None:
            os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = prev_env
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_live_settlement_captured_and_deduped()
    print("[PASS] live settlement → settlement-log + closed[], original side/qty recovered, deduped")
    test_gap_settlement_logged_flagged()
    print("[PASS] gap-settled position (no prior snapshot) is logged as a flagged record")
    test_settle_paper_skips_synced_live_positions()
    print("[PASS] settle_paper skips synced live positions (no fetch, no settle)")
    test_live_stop_fed_realized_only()
    print("[PASS] live $ stop fed by realized settlements only (open mark does not trip it)")
    test_gap_settled_feeds_stop_and_logged_once()
    print("[PASS] gap-settled loss feeds the $ stop AND is logged, flagged (deduped, once)")
    test_feed_failure_leaves_dedup_uncommitted()
    print("[PASS] feed failure leaves closed[]/log uncommitted (loss re-fed next cycle, not masked)")
    print("\nLive settlement-tracking tests pass.")

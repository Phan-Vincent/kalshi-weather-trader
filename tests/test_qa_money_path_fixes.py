#!/usr/bin/env python3
"""Regression tests for the 2026-07-01 money-path QA fixes (QA-01..QA-05).

Each test pins a defect the full audit found (reviews/QA-full-audit-2026-07-01.md) so a
regression fails loudly. Plain pytest; no network, no real-state mutation (temp dirs +
monkeypatched CLI). The live placement loop lives in paper_trade.main() and isn't unit-
isolable, so QA-01/QA-02 are guarded at the risk.py contract seam the loop fixes depend on.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from trader.paper_book import PaperBook          # noqa: E402
from trader.risk import RiskGate                 # noqa: E402
import repair_paper_state                        # noqa: E402  (orphan tool; import is side-effect-free)
import sync_live_positions as slp                # noqa: E402


# ── QA-04: equity_cents must not double-flip a NO position on an empty book ──────────────
def _book(tmp_path):
    b = PaperBook(state_path=tmp_path / "paper-book.json")
    b.cash_cents = 100_000
    b.open = []
    return b


def _no_pos(ticker="KXHIGHNY-26JUL01-T90", qty=10, entry=30):
    return {"ticker": ticker, "side": "no", "qty": qty, "avg_entry_cents": entry}


def test_qa04_no_position_empty_book_marks_at_own_side_entry(tmp_path):
    b = _book(tmp_path)
    b.open = [_no_pos(entry=30, qty=10)]
    tk = b.open[0]["ticker"]
    eq_no_snap = b.equity_cents({})                                    # no snapshot at all
    eq_empty_book = b.equity_cents({tk: {"yes_bid": 0, "yes_ask": 0}})  # snapshot present but empty
    # Both fall back to the position's own-side entry (30¢) — NOT the flipped 70¢.
    assert eq_no_snap == eq_empty_book == b.cash_cents + 30 * 10
    # Regression: the old code marked a NO empty-book at 100-30=70 → +40¢/ct phantom gain.
    assert eq_empty_book != b.cash_cents + 70 * 10


def test_qa04_no_position_with_live_yes_book_flips_correctly(tmp_path):
    b = _book(tmp_path)
    b.open = [_no_pos(entry=30, qty=10)]
    tk = b.open[0]["ticker"]
    eq = b.equity_cents({tk: {"yes_bid": 58, "yes_ask": 62}})   # yes mid 60 → NO mark 100-60=40
    assert eq == b.cash_cents + 40 * 10


def test_qa04_yes_position_empty_book_unchanged(tmp_path):
    b = _book(tmp_path)
    b.open = [{"ticker": "KXHIGHLAX-26JUL01-T95", "side": "yes", "qty": 5, "avg_entry_cents": 42}]
    tk = b.open[0]["ticker"]
    assert b.equity_cents({}) == b.equity_cents({tk: {"yes_bid": 0, "yes_ask": 0}}) == b.cash_cents + 42 * 5


# ── QA-05: repair_paper_state must read the true outcome, not infer it from the P&L sign ──
def test_qa05_no_side_winner_keeps_no_outcome():
    # NO position that WON: pnl>0 but the market settled NO. Old heuristic returned "yes".
    assert repair_paper_state.market_result({"side": "no", "pnl_cents": 186, "settlement_result": "no"}) == "no"


def test_qa05_prefers_recorded_field_over_pnl_sign():
    assert repair_paper_state.market_result({"side": "yes", "pnl_cents": 500, "settlement_result": "no"}) == "no"


def test_qa05_fallback_reconstructs_from_side_and_pnl_when_field_missing():
    assert repair_paper_state.market_result({"side": "no", "pnl_cents": 186}) == "no"    # NO winner
    assert repair_paper_state.market_result({"side": "no", "pnl_cents": -50}) == "yes"   # NO loser → YES won
    assert repair_paper_state.market_result({"side": "yes", "pnl_cents": 300}) == "yes"  # YES winner
    assert repair_paper_state.market_result({"side": "yes", "pnl_cents": -20}) == "no"   # YES loser → NO won


# ── QA-03: a balance-feed failure/null must not crash or fabricate zero cash ─────────────
def _fake_run(balance_rc=0, balance_stdout='{"balance": 45000, "portfolio_value": 15000}'):
    def run(cmd, **kw):
        if "balance" in cmd:
            return types.SimpleNamespace(returncode=balance_rc, stdout=balance_stdout, stderr="")
        return types.SimpleNamespace(returncode=0, stdout='{"market_positions": [], "event_positions": []}', stderr="")
    return run


def test_qa03_balance_cli_failure_flags_not_ok(monkeypatch):
    monkeypatch.setattr(slp.subprocess, "run", _fake_run(balance_rc=1, balance_stdout=""))
    st = slp.get_kalshi_positions()
    assert st["balance_ok"] is False
    assert st["available_cents"] == 0            # coerced to 0, not crashed — caller carries forward


def test_qa03_null_balance_field_does_not_crash(monkeypatch):
    # int(None) would have raised TypeError before the fix.
    monkeypatch.setattr(slp.subprocess, "run",
                        _fake_run(balance_rc=0, balance_stdout='{"balance": null, "portfolio_value": null}'))
    st = slp.get_kalshi_positions()
    assert st["balance_ok"] is False
    assert st["available_cents"] == 0 and st["portfolio_cents"] == 0


def test_qa03_healthy_balance_is_ok(monkeypatch):
    monkeypatch.setattr(slp.subprocess, "run", _fake_run())
    st = slp.get_kalshi_positions()
    assert st["balance_ok"] is True
    assert st["available_cents"] == 45000 and st["portfolio_cents"] == 15000


# ── QA-01: folding a resting-order ticker into existing_tickers blocks re-entry ──────────
def test_qa01_resting_ticker_in_dedup_blocks_reentry():
    rg = RiskGate()
    rg.research_mode.enabled = True
    rg.research_mode.allow_duplicates = False
    tk = "KXHIGHNY-26JUL01-T90"
    existing = {tk}                              # simulates a resting live order folded in by the fix
    assert rg.check_duplicate_exposure(tk, existing)[0] is False          # would stack a 2nd live order
    assert rg.check_duplicate_exposure("KXHIGHLAX-26JUL01-T95", existing)[0] is True   # other ticker OK


# ── QA-02: the in-run budget stop must tighten as placed notional accumulates ────────────
def test_qa02_cumulative_notional_trips_budget_stop():
    rg = RiskGate()
    rg.daily_max_loss_dollars = 20              # $20 daily stop → -2000¢ limit
    rg._today_pnl_cents = -1000                 # already -$10 today
    per_order = 5 * 100                         # a $5 order
    placed_notional, allowed = 0, 0
    for _ in range(10):
        ok, _r = rg.check_budget(prospective_cost_cents=-(placed_notional + per_order))
        if not ok:
            break
        allowed += 1
        placed_notional += per_order
    # -$10 already; cumulative $5 orders must trip the -$20 stop quickly.
    assert allowed <= 2
    # Pre-fix behavior: a STATIC per-order check (ignoring accumulation) always passed here.
    assert rg.check_budget(prospective_cost_cents=-per_order)[0] is True

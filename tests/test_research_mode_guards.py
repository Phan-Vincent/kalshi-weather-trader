#!/usr/bin/env python3
"""
tests/test_research_mode_guards.py — Targeted tests for Fix C: research-mode risk gates.

Covers:
  - Default research-mode min price = 10¢ (vs production 3¢)
  - Brier gate RED blocks new entries after meaningful sample
  - Duplicate same-ticker exposure blocked on reruns
  - City circuit breakers enforced in research mode
  - Env overrides for all four gates (allow data collection)

Run: python3 -m pytest tests/test_research_mode_guards.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from trader.risk import RiskGate, ResearchModeConfig
from trader.paper_book import PaperBook


# ── helpers ───────────────────────────────────────────────────────────

def _isolated_risk_gate(**env_overrides):
    """Build a RiskGate with mocked state and env overrides applied."""
    env = {
        "KALSHI_RESEARCH_MODE": "1",
        "KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS": "10",
        "KALSHI_RESEARCH_MODE_ALLOW_RED": "",
        "KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES": "",
        "KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER": "",
        "KALSHI_RESEARCH_MODE_BRIER_MIN_TRADES": "150",
    }
    env.update(env_overrides)
    with patch.dict(os.environ, env, clear=False):
        with patch.object(RiskGate, "_load_state", lambda self: None):
            gate = RiskGate()
            gate._today_pnl_cents = 0
            gate._week_pnl_cents = 0
            gate._today = "test"
            gate._week_start = "test"
            gate._open_positions = []
            gate._realized_today_cents = 0
            gate._consecutive_losses = 0
            gate._consecutive_losses_by_city = {}
            gate._city_pnl_cents = {}
            gate._total_bankroll_cents = 50000  # $500 default for tests
            gate._halted_cities = []
            # _load_state is mocked, so set the per-event cap (added 2026-06-19) and
            # the ramp-state fields explicitly. Without a non-zero cap, kelly_qty
            # sizes to 0 and scan() produces no signals.
            gate.per_event_max_position_dollars = 50
            gate._rung_idx = 0
            gate._fills_at_rung_start = 0
            gate._balance_at_rung_start = 0
            return gate


# ── min price guard ─────────────────────────────────────────────────

def test_research_mode_default_min_price_is_10():
    gate = _isolated_risk_gate()
    assert gate.research_mode.enabled is True
    assert gate.min_price_cents == 10, f"Expected 10¢ in research mode, got {gate.min_price_cents}¢"
    print("[PASS] research mode default min_price_cents = 10")


def test_research_mode_disabled_uses_production_default():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE="0")
    assert gate.research_mode.enabled is False
    # Production default raised 15->20 on 2026-06-19 (<=15c bucket is a net loser).
    assert gate.min_price_cents == 20, f"Expected 20¢ production default when research mode disabled, got {gate.min_price_cents}¢"
    print("[PASS] research mode disabled -> min_price_cents = 20 (production)")


def test_research_mode_min_price_override_env():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS="5")
    assert gate.min_price_cents == 5, f"Expected 5¢ via env override, got {gate.min_price_cents}¢"
    assert gate.research_mode.min_price_cents == 5
    print("[PASS] KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS=5 override works")


def test_research_mode_min_price_blocks_low_price_signals():
    """Scanner uses risk_gate.min_price_cents; in research mode 10¢ blocks 5¢ signals."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    close_ok = (now + datetime.timedelta(hours=12)).isoformat()

    fixture = [
        {
            "ticker": "KXHIGHTHOU-26MAY28-T91",
            "fair_prob": 0.99,
            "confidence": "high",
            "rationale": "test",
            "source": "test",
            "_market": {
                "close_time": close_ok,
                "yes_bid": 4,
                "yes_ask": 5,
                "no_bid": 94,
                "no_ask": 95,
            },
        },
    ]
    fd, fv_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(fv_path, "w") as f:
        json.dump(fixture, f)

    from trader.scanner import scan

    gate = _isolated_risk_gate()  # min_price_cents = 10 (env override)
    gate.check_close_time = lambda _close_time: True
    signals = scan(fv_path, gate, mode="taker", top_n=5)
    assert len(signals) == 0, f"Expected 0 signals (price 5¢ < 10¢ min), got {len(signals)}"
    os.unlink(fv_path)
    print("[PASS] research mode min_price_cents=10 blocks 5¢ signals")


# ── Brier gate ──────────────────────────────────────────────────────

def test_brier_red_gate_blocks():
    gate = _isolated_risk_gate()
    red_summary = {
        "n_trades": 150,
        "gate_status": "RED",
        "brier_skill_score": -1.26,
        "our_brier_mean": 0.308,
        "market_brier_mean": 0.137,
    }
    ok, reason = gate.check_brier_gate(red_summary)
    assert ok is False, f"Expected RED gate to block, got ok={ok}"
    assert "RED" in reason
    assert "150" in reason
    print("[PASS] Brier RED gate blocks after 150 trades")


def test_brier_red_gate_allow_override():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE_ALLOW_RED="1")
    red_summary = {
        "n_trades": 150,
        "gate_status": "RED",
        "brier_skill_score": -1.26,
    }
    ok, reason = gate.check_brier_gate(red_summary)
    assert ok is True, f"Expected override to allow RED gate, got ok={ok} reason={reason}"
    print("[PASS] KALSHI_RESEARCH_MODE_ALLOW_RED=1 overrides Brier RED gate")


def test_brier_green_gate_allows():
    gate = _isolated_risk_gate()
    green_summary = {
        "n_trades": 150,
        "gate_status": "GREEN",
        "brier_skill_score": 0.15,
    }
    ok, reason = gate.check_brier_gate(green_summary)
    assert ok is True, f"Expected GREEN gate to allow, got ok={ok}"
    print("[PASS] Brier GREEN gate allows new entries")


def test_brier_waiting_gate_allows():
    gate = _isolated_risk_gate()
    waiting_summary = {
        "n_trades": 149,
        "gate_status": "WAITING",
        "brier_skill_score": None,
    }
    ok, reason = gate.check_brier_gate(waiting_summary)
    assert ok is True, f"Expected WAITING gate to allow, got ok={ok}"
    print("[PASS] Brier WAITING gate allows new entries (<150 trades)")


def test_brier_red_gate_disabled_when_research_mode_off():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE="0")
    red_summary = {
        "n_trades": 150,
        "gate_status": "RED",
        "brier_skill_score": -1.26,
    }
    ok, reason = gate.check_brier_gate(red_summary)
    assert ok is True, f"Expected research mode off to skip Brier gate, got ok={ok}"
    print("[PASS] Brier gate skipped when research mode disabled")


def test_brier_red_gate_custom_threshold():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE_BRIER_MIN_TRADES="50")
    red_summary = {
        "n_trades": 60,
        "gate_status": "RED",
        "brier_skill_score": -0.5,
    }
    ok, reason = gate.check_brier_gate(red_summary)
    assert ok is False, f"Expected block at 60 trades with threshold 50, got ok={ok}"

    waiting_summary = {
        "n_trades": 49,
        "gate_status": "RED",
        "brier_skill_score": -0.5,
    }
    ok, reason = gate.check_brier_gate(waiting_summary)
    assert ok is True, f"Expected allow at 49 trades with threshold 50, got ok={ok}"
    print("[PASS] KALSHI_RESEARCH_MODE_BRIER_MIN_TRADES custom threshold works")


# ── duplicate exposure guard ────────────────────────────────────────

def test_duplicate_exposure_blocks():
    gate = _isolated_risk_gate()
    existing = {"KXHIGHTHOU-26MAY28-T91"}
    ok, reason = gate.check_duplicate_exposure("KXHIGHTHOU-26MAY28-T91", existing)
    assert ok is False, f"Expected duplicate to block, got ok={ok}"
    assert "duplicate_exposure" in reason
    print("[PASS] duplicate_exposure blocks same-ticker re-entry")


def test_duplicate_exposure_allows_new_tickers():
    gate = _isolated_risk_gate()
    existing = {"KXHIGHTHOU-26MAY28-T91"}
    ok, reason = gate.check_duplicate_exposure("KXLOWTLAX-26MAY28-B55", existing)
    assert ok is True, f"Expected new ticker to allow, got ok={ok}"
    print("[PASS] duplicate_exposure allows new tickers")


def test_duplicate_override_allows():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES="1")
    existing = {"KXHIGHTHOU-26MAY28-T91"}
    ok, reason = gate.check_duplicate_exposure("KXHIGHTHOU-26MAY28-T91", existing)
    assert ok is True, f"Expected duplicate override to allow, got ok={ok}"
    print("[PASS] KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES=1 overrides duplicate block")


def test_duplicate_exposure_disabled_when_research_mode_off():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE="0")
    existing = {"KXHIGHTHOU-26MAY28-T91"}
    ok, reason = gate.check_duplicate_exposure("KXHIGHTHOU-26MAY28-T91", existing)
    assert ok is True, f"Expected research mode off to skip duplicate gate, got ok={ok}"
    print("[PASS] duplicate gate skipped when research mode disabled")


# ── city dynamic multiplier (was: hard circuit breaker) ──────────

def test_city_circuit_breaker_enforced_at_15_losses():
    """Dynamic multiplier: 15+ consecutive losses triggers full halt (0.0)."""
    gate = _isolated_risk_gate()
    gate._consecutive_losses_by_city = {"HOU": 15}
    mult, reason = gate.get_city_risk_multiplier("HOU")
    assert mult == 0.0, f"Expected 0.0 multiplier for 15 consecutive losses, got {mult}"
    assert "consecutive_losses" in (reason or "")
    # Backward-compat check_circuit_breaker also returns False
    ok, _ = gate.check_circuit_breaker("HOU")
    assert ok is False
    print("[PASS] 15 consecutive losses → full halt (0.0 multiplier)")


def test_city_8_losses_no_pnl_returns_full_multiplier():
    """8 consecutive losses without PnL data → still 1.0 (old threshold retired)."""
    gate = _isolated_risk_gate()
    gate._consecutive_losses_by_city = {"HOU": 8}
    mult, reason = gate.get_city_risk_multiplier("HOU")
    assert mult == 1.0, f"Expected 1.0 multiplier for 8 losses (below 15), got {mult}"
    # Backward-compat: check_circuit_breaker now returns True for <15
    ok, _ = gate.check_circuit_breaker("HOU")
    assert ok is True
    print("[PASS] 8 consecutive losses → 1.0 multiplier (below new 15 threshold)")


def test_city_pnl_20_percent_triggers_quarter_size():
    """City at -20% bankroll → 0.25 multiplier."""
    gate = _isolated_risk_gate()
    gate._total_bankroll_cents = 50000  # $500
    gate._city_pnl_cents = {"HOU": -10000}  # -$100 = -20% of $500
    mult, reason = gate.get_city_risk_multiplier("HOU")
    assert mult == 0.25, f"Expected 0.25 multiplier at -20% bankroll, got {mult}"
    assert "pnl" in (reason or "").lower()
    print("[PASS] City at -20% bankroll → 0.25x position size")


def test_city_pnl_10_percent_triggers_half_size():
    """City at -10% bankroll → 0.50 multiplier."""
    gate = _isolated_risk_gate()
    gate._total_bankroll_cents = 50000
    gate._city_pnl_cents = {"SEA": -5000}  # -$50 = -10% of $500
    mult, reason = gate.get_city_risk_multiplier("SEA")
    assert mult == 0.50, f"Expected 0.50 multiplier at -10% bankroll, got {mult}"
    print("[PASS] City at -10% bankroll → 0.50x position size")


def test_city_profitable_returns_full_multiplier():
    """Profitable city → 1.0 multiplier."""
    gate = _isolated_risk_gate()
    gate._total_bankroll_cents = 50000
    gate._city_pnl_cents = {"LAX": 2500}  # +$25 profit
    mult, reason = gate.get_city_risk_multiplier("LAX")
    assert mult == 1.0, f"Expected 1.0 for profitable city, got {mult}"
    assert reason is None
    print("[PASS] Profitable city → 1.0x position size")


def test_city_circuit_breaker_override_disables():
    gate = _isolated_risk_gate(KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER="1")
    gate._consecutive_losses_by_city = {"HOU": 20}
    gate._city_pnl_cents = {"HOU": -20000}
    mult, reason = gate.get_city_risk_multiplier("HOU")
    assert mult == 1.0, f"Expected 1.0 when circuit breaker disabled, got {mult}"
    ok, _ = gate.check_circuit_breaker("HOU")
    assert ok is True
    print("[PASS] KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER=1 overrides all")


# ── integration: paper_book duplicate via RiskGate ─────────────────

def test_paper_book_idempotency_with_risk_gate():
    """Simulate paper_trade.py flow: RiskGate.check_duplicate_exposure against PaperBook."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    book = PaperBook(state_path=Path(path))

    book.fill_taker(
        ticker="KXHIGHTHOU-26MAY28-T91",
        side="yes",
        ask_price_cents=50,
        qty=1,
        fair_prob=0.75,
        edge_cents_post_fee=5.0,
        confidence="high",
        market_close_time="2099-01-01T23:59:00+00:00",
    )
    book._save()

    book2 = PaperBook(state_path=Path(path))
    existing_tickers = {p["ticker"] for p in book2.open + book2.pending_makers}

    gate = _isolated_risk_gate()
    ok, reason = gate.check_duplicate_exposure("KXHIGHTHOU-26MAY28-T91", existing_tickers)
    assert ok is False
    assert "duplicate_exposure" in reason

    ok, reason = gate.check_duplicate_exposure("KXLOWTLAX-26MAY28-B55", existing_tickers)
    assert ok is True

    os.unlink(path)
    print("[PASS] RiskGate.check_duplicate_exposure correctly guards PaperBook reruns")


# ── integration: scanner + research min price ───────────────────────

def test_scanner_respects_research_min_price():
    """End-to-end: scanner uses RiskGate.min_price_cents which is 10 in research mode."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    close_ok = (now + datetime.timedelta(hours=12)).isoformat()

    # Two signals: one at 8¢ (blocked), one at 15¢ (allowed)
    fixture = [
        {
            "ticker": "KXHIGHTHOU-26MAY28-T91",
            "fair_prob": 0.99,
            "confidence": "high",
            "rationale": "test low price",
            "source": "test",
            "_market": {
                "close_time": close_ok,
                "yes_bid": 7,
                "yes_ask": 8,
                "no_bid": 91,
                "no_ask": 92,
            },
        },
        {
            "ticker": "KXLOWTLAX-26MAY28-B55",
            "fair_prob": 0.85,
            "confidence": "high",
            "rationale": "test safe price",
            "source": "test",
            "_market": {
                "close_time": close_ok,
                "yes_bid": 14,
                "yes_ask": 15,
                "no_bid": 84,
                "no_ask": 85,
            },
        },
    ]
    fd, fv_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(fv_path, "w") as f:
        json.dump(fixture, f)

    from trader.scanner import scan

    gate = _isolated_risk_gate()  # min_price_cents = 10 (env override)
    gate.check_close_time = lambda _close_time: True
    signals = scan(fv_path, gate, mode="taker", top_n=5)

    tickers = [s.ticker for s in signals]
    assert "KXHIGHTHOU-26MAY28-T91" not in tickers, "8¢ signal should be blocked by 10¢ min"
    assert "KXLOWTLAX-26MAY28-B55" in tickers, "15¢ signal should be allowed"
    os.unlink(fv_path)
    print("[PASS] scanner respects research mode min_price_cents=10")


# ── ResearchModeConfig.from_env unit ────────────────────────────────

def test_research_mode_config_from_env_defaults():
    with patch.dict(os.environ, {}, clear=False):
        cfg = ResearchModeConfig.from_env()
        assert cfg.enabled is True
        assert cfg.min_price_cents == 20  # default raised 15->20 on 2026-06-19
        assert cfg.allow_red_gate is False
        assert cfg.allow_duplicates is False
        assert cfg.disable_city_circuit_breaker is False
        assert cfg.brier_min_trades == 150
    print("[PASS] ResearchModeConfig.from_env defaults are safe")


def test_research_mode_config_from_env_all_overrides():
    env = {
        "KALSHI_RESEARCH_MODE": "0",
        "KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS": "3",
        "KALSHI_RESEARCH_MODE_ALLOW_RED": "1",
        "KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES": "1",
        "KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER": "1",
        "KALSHI_RESEARCH_MODE_BRIER_MIN_TRADES": "50",
    }
    with patch.dict(os.environ, env, clear=False):
        cfg = ResearchModeConfig.from_env()
        assert cfg.enabled is False
        assert cfg.min_price_cents == 3
        assert cfg.allow_red_gate is True
        assert cfg.allow_duplicates is True
        assert cfg.disable_city_circuit_breaker is True
        assert cfg.brier_min_trades == 50
    print("[PASS] ResearchModeConfig.from_env handles all overrides")


if __name__ == "__main__":
    test_research_mode_default_min_price_is_10()
    test_research_mode_disabled_uses_production_default()
    test_research_mode_min_price_override_env()
    test_research_mode_min_price_blocks_low_price_signals()
    test_brier_red_gate_blocks()
    test_brier_red_gate_allow_override()
    test_brier_green_gate_allows()
    test_brier_waiting_gate_allows()
    test_brier_red_gate_disabled_when_research_mode_off()
    test_brier_red_gate_custom_threshold()
    test_duplicate_exposure_blocks()
    test_duplicate_exposure_allows_new_tickers()
    test_duplicate_override_allows()
    test_duplicate_exposure_disabled_when_research_mode_off()
    test_city_circuit_breaker_enforced_at_15_losses()
    test_city_8_losses_no_pnl_returns_full_multiplier()
    test_city_pnl_20_percent_triggers_quarter_size()
    test_city_pnl_10_percent_triggers_half_size()
    test_city_profitable_returns_full_multiplier()
    test_city_circuit_breaker_override_disables()
    test_paper_book_idempotency_with_risk_gate()
    test_scanner_respects_research_min_price()
    test_research_mode_config_from_env_defaults()
    test_research_mode_config_from_env_all_overrides()
    print("\nAll research-mode guard tests passed.")

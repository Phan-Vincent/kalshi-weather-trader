#!/usr/bin/env python3
"""
Verification tests for the Kalshi weather trading engine.
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.risk import RiskGate
from trader.orders import compute_post_fee_edge, _kalshi_taker_fee_cents
from trader.scanner import scan


def test_fee_math():
    """Assert fee math for taker at 50/50 gives edge in 18-19 range."""
    result = compute_post_fee_edge(fair_prob_yes=0.70, yes_ask_cents=50, no_ask_cents=50, qty=10)
    assert result["side"] == "yes", f"Expected YES side, got {result['side']}"
    edge = result["edge_cents_per_contract"]
    assert 18 <= edge <= 19, f"Edge {edge} not in 18-19 range"
    print(f"[PASS] fee_math: side={result['side']} edge={edge:.2f} fee_per_contract={result['fee_per_contract_cents']:.2f}")


def test_risk_rejects_low_confidence():
    """Verify RiskGate rejects confidence='low' entries."""
    gate = RiskGate(min_confidence="med")
    assert gate.check_confidence("high") is True
    assert gate.check_confidence("med") is True
    assert gate.check_confidence("low") is False
    print("[PASS] risk_rejects_low_confidence")


@contextmanager
def _pristine_weather_env():
    """Run the body with every KALSHI_WEATHER_* env var cleared, then restored.

    The scanner and RiskGate branch on ~20 KALSHI_WEATHER_* flags (CALIBRATED_EDGE,
    LEADTIME_FILTER, LIVE_MODE, PREMIUM_MODE, SIDE_BIAS, REQUIRE_TWO_SIDED, …). Run
    standalone (`python3 bin/verify.py`) the env is clean, but wired into the shared
    pytest process this test now inherits flags that sibling tests set and never
    restore (e.g. test_calibrated_edge_gate leaves CALIBRATED_EDGE=1 + LEADTIME=off,
    which drops CHI's calibrated NO edge below the 20¢ floor). Clearing only the
    KALSHI_WEATHER_* keys (never HOME/PATH) makes this test order-independent and
    identical to a clean run, without touching the sibling tests' own isolation.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("KALSHI_WEATHER_")}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.startswith("KALSHI_WEATHER_")]:
            del os.environ[k]
        os.environ.update(saved)


def _future_close(hours: float = 20.0) -> str:
    """A close_time `hours` from *now*, ISO-formatted.

    Fixture close_times must be relative to now(), never hardcoded calendar dates.
    A stale past literal (the fixture's original 2026-05-29 strings) fails
    RiskGate.check_close_time (the [90min, 36h] window) — the operative blocker
    once the close-time mock is removed — and also drifts into scanner.py's
    `hours < 0` → tightest "0-6h" lead-time bucket (harmless for THIS fixture,
    whose ~28c edges clear even that 25¢ floor, but wrong in principle). 20h keeps
    every fixture market in the 18-24h bucket, comfortably inside the trade window.
    Mirrors tests/audit_fixes/test_paper_book_double_exposure.py::_future_close.
    """
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_scanner_filtering():
    """Run scanner against fake fair-values.json and check results."""
    # Use an isolated risk state so production city circuit breakers do not
    # make deterministic fixture tickers disappear from this unit test.
    from unittest.mock import patch
    with _pristine_weather_env(), patch.object(RiskGate, "_load_state", lambda self: None):
        gate = RiskGate()
        gate._today_pnl_cents = 0
        gate._week_pnl_cents = 0
        gate._today = "test"
        gate._week_id = "test"
        gate._open_positions = []
        gate._realized_today_cents = 0
        gate._consecutive_losses = 0
        gate._consecutive_losses_by_city = {}
        gate._halted_cities = []
        # An isolated gate is never bankrolled via set_bankroll() (which
        # production's paper_trade.py calls before scanning), so
        # per_event_max_position_dollars stays at its 0 auto-scale sentinel →
        # per_event_cap=0 → kelly_qty()=0 → every signal is dropped at `qty < 1`.
        # Give it the $5 per-event cap a ~$500 bankroll auto-scales to, without
        # the set_bankroll() disk write. This — not the min-edge — is what made
        # the test return 0 signals; the fixture's ~28c edges clear the operative
        # gate (the lead-time bucket floor, 10¢ at 18-24h) and, a fortiori, the
        # 20¢ RiskGate.min_edge_cents_after_fees default.
        gate.per_event_max_position_dollars = 5
        # Rewrite the fixture's placeholder close_times to a live-relative window
        # so lead-time bucketing and RiskGate.check_close_time both see a valid
        # future market, then scan the rewritten copy (the real gate does the
        # close-time check — no mock needed now that the dates are current).
        fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "test-fair-values.json"
        entries = json.loads(fixture_path.read_text())
        for entry in entries:
            entry["_market"]["close_time"] = _future_close()
        with tempfile.TemporaryDirectory() as td:
            fair_path = Path(td) / "fair-values.json"
            fair_path.write_text(json.dumps(entries))
            signals = scan(str(fair_path), gate, mode="taker", top_n=5)

    tickers = {s.ticker for s in signals}
    # The low-confidence entry should be dropped
    low_conf_ticker = "KXHIGHTPHX-26MAY28-T105"
    assert low_conf_ticker not in tickers, f"Low-confidence {low_conf_ticker} should be dropped"

    # The coin-flip entry (edge too small) should also be dropped
    coin_flip = "KXTEMPATLH-26MAY28-T82"
    assert coin_flip not in tickers, f"Low-edge {coin_flip} should be dropped"

    # We should get the strong YES and strong NO
    assert len(signals) == 2, f"Expected 2 signals, got {len(signals)}: {[s.ticker for s in signals]}"

    hou = next((s for s in signals if s.ticker == "KXHIGHTHOU-26MAY28-T91"), None)
    chi = next((s for s in signals if s.ticker == "KXLOWTCHI-26MAY28-T55"), None)
    assert hou is not None, "HOU signal missing"
    assert chi is not None, "CHI signal missing"
    assert hou.side == "yes", f"HOU side should be yes, got {hou.side}"
    assert chi.side == "no", f"CHI side should be no, got {chi.side}"

    print(f"[PASS] scanner_filtering: {len(signals)} signals, tickers={[s.ticker for s in signals]}")


def test_dry_run_no_api():
    """Confirm dry-run mode does not call the live API."""
    from trader.orders import place_limit_order
    result = place_limit_order("KXHIGHTEST-26MAY28-T99", "yes", 50, 5, prod=False, dry_run=True)
    assert result.dry_run is True
    assert result.order_id.startswith("dry-")
    assert result.success is True
    print("[PASS] dry_run_no_api: synthetic order id generated, no network call")


def test_kalshi_fee_formula_50_50():
    """Check taker fee at 50/50 for qty=1, 10, 100."""
    # At 50 cents, qty=1: ceil(0.07 * 1 * 0.5 * 0.5 * 100) = ceil(1.75) = 2 cents
    assert _kalshi_taker_fee_cents(1, 50) == 2, f"qty=1 fee={_kalshi_taker_fee_cents(1,50)}"
    # qty=10: ceil(0.07 * 10 * 0.5 * 0.5 * 100) = ceil(17.5) = 18 cents
    assert _kalshi_taker_fee_cents(10, 50) == 18, f"qty=10 fee={_kalshi_taker_fee_cents(10,50)}"
    # qty=100: ceil(0.07 * 100 * 0.5 * 0.5 * 100) = ceil(175) = 175 cents
    assert _kalshi_taker_fee_cents(100, 50) == 175, f"qty=100 fee={_kalshi_taker_fee_cents(100,50)}"
    print("[PASS] kalshi_fee_formula_50_50")


if __name__ == "__main__":
    test_fee_math()
    test_risk_rejects_low_confidence()
    test_scanner_filtering()
    test_dry_run_no_api()
    test_kalshi_fee_formula_50_50()
    print("\nAll verification tests passed.")

#!/usr/bin/env python3
"""Tests for the calibrated-edge gate (KALSHI_WEATHER_CALIBRATED_EDGE).

The fair-value model is ~2x overconfident; sizing already shrinks via
_sizing_prob = min(raw, calibrated). This gate brings the *entry edge* decision
into line, DOWN-ONLY, behind an env flag default OFF (live unchanged).

Properties asserted:
  - OFF (default): _edge_fair_cents is byte-identical to today's raw fair cents.
  - ON, unfitted curve: fail-safe → returns the raw value (never raises).
  - ON, fitted curve: per-side fair is DOWN-ONLY (<= raw) → can only tighten.
  - scan() ON returns a SUBSET of OFF: an overconfident mid market is dropped,
    a strong market survives; Signal.fair_prob stays RAW (for Brier).

Runs under plain `python3 tests/test_calibrated_edge_gate.py` (no pytest needed).
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import trader.scanner as scanner  # noqa: E402
from trader.risk import RiskGate  # noqa: E402


# These tests monkeypatch the module-global scanner._calibrated_prob (lines below)
# to a deterministic curve and never restored it — under pytest that leaked into
# every later test that sizes via _calibrated_prob, an order-dependent global
# mutation the KALSHI_WEATHER_* env conftest can't catch. This autouse fixture
# snapshots and restores it (and _CALIBRATOR) around each test. Guarded by an
# optional pytest import so the file still runs standalone with `python3` (its
# __main__ path is a one-shot process, so a leak there is moot).
try:  # pragma: no cover - trivial import guard
    import pytest  # noqa: E402

    @pytest.fixture(autouse=True)
    def _restore_calibrator():
        _orig_fn = scanner._calibrated_prob
        _orig_cal = scanner._CALIBRATOR
        try:
            yield
        finally:
            scanner._calibrated_prob = _orig_fn
            scanner._CALIBRATOR = _orig_cal
except ImportError:  # standalone `python3 tests/test_calibrated_edge_gate.py`
    pass


# Deterministic calibration curve: shrink toward 0.5 by 40% (mimics the real
# isotonic pull-down for an overconfident model). Identity = "unfitted".
def _shrink(p: float) -> float:
    return 0.5 + 0.6 * (p - 0.5)


def _identity(p: float) -> float:
    return p


def _set_flag(on: bool) -> None:
    os.environ["KALSHI_WEATHER_CALIBRATED_EDGE"] = "1" if on else "0"


def _close(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixture() -> str:
    recs = [
        # strong YES (raw 0.80, ask 45): survives BOTH (edge >> threshold even calibrated)
        {"ticker": "KXHIGHTLAX-26JUN22-T75", "confidence": "high", "fair_prob": 0.80,
         "rationale": "fx", "source": "fx", "model_temp_forecast": 78.0,
         "_market": {"close_time": _close(15), "yes_bid": 41, "yes_ask": 45, "no_bid": 51, "no_ask": 55}},
        # overconfident mid YES (raw 0.62, ask 55): passes raw, dropped down-only
        {"ticker": "KXHIGHTPHX-26JUN22-T100", "confidence": "high", "fair_prob": 0.62,
         "rationale": "fx", "source": "fx", "model_temp_forecast": 101.0,
         "_market": {"close_time": _close(15), "yes_bid": 50, "yes_ask": 55, "no_bid": 44, "no_ask": 47}},
        # fairly priced (raw 0.50, 50/50): dropped both ways
        {"ticker": "KXHIGHTDAL-26JUN22-T95", "confidence": "high", "fair_prob": 0.50,
         "rationale": "fx", "source": "fx", "model_temp_forecast": 95.0,
         "_market": {"close_time": _close(15), "yes_bid": 48, "yes_ask": 50, "no_bid": 48, "no_ask": 50}},
    ]
    payload = {"generated_at_utc": _close(0), "records": recs, "lessons_applied": {}}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(payload, f)
    f.close()
    return f.name


def _scan(flag_on: bool, calibrator=_shrink):
    """Run scan() in taker mode, temp state dir, deterministic calibrator."""
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tempfile.mkdtemp(prefix="caledge_t_")
    os.environ["KALSHI_WEATHER_LEADTIME_FILTER"] = "off"
    os.environ.pop("KALSHI_WEATHER_SIDE_BIAS", None)
    os.environ.pop("KALSHI_WEATHER_LIVE_MODE", None)
    os.environ.pop("KALSHI_WEATHER_PREMIUM_MODE", None)
    _set_flag(flag_on)
    scanner._calibrated_prob = calibrator  # type: ignore  # deterministic
    rg = RiskGate(min_edge_cents_after_fees=5)
    rg.set_bankroll(500000)
    return scanner.scan(_fixture(), rg, mode="taker", top_n=10, bankroll_cents=500000)


# ── _edge_fair_cents unit properties ────────────────────────────────────────

def test_edge_fair_cents_off_is_raw():
    """OFF: identical to today's fair_yes_cents / 100-fair_yes_cents."""
    _set_flag(False)
    for p in (0.05, 0.30, 0.50, 0.62, 0.85, 0.97):
        yes_c = max(1, min(99, round(p * 100)))
        assert scanner._edge_fair_cents(p, "yes") == yes_c, p
        assert scanner._edge_fair_cents(p, "no") == 100 - yes_c, p


def test_edge_fair_cents_failsafe_when_unfitted():
    """ON but unfitted curve (identity calibrate) → returns the raw value, no raise."""
    _set_flag(True)
    scanner._calibrated_prob = _identity  # type: ignore
    for p in (0.30, 0.62, 0.85):
        yes_c = max(1, min(99, round(p * 100)))
        assert scanner._edge_fair_cents(p, "yes") == yes_c, p
        assert scanner._edge_fair_cents(p, "no") == 100 - yes_c, p


def test_edge_fair_cents_on_is_down_only():
    """ON with a real (shrinking) curve → never larger than the raw per-side cents."""
    _set_flag(True)
    scanner._calibrated_prob = _shrink  # type: ignore
    for p in (0.20, 0.40, 0.55, 0.62, 0.78, 0.90):
        raw_yes = max(1, min(99, round(p * 100)))
        raw_no = 100 - raw_yes
        assert scanner._edge_fair_cents(p, "yes") <= raw_yes, p
        assert scanner._edge_fair_cents(p, "no") <= raw_no, p


# ── scan() integration ──────────────────────────────────────────────────────

def test_scan_on_is_subset_and_drops_overconfident():
    off = {(s.ticker, s.side) for s in _scan(False)}
    on = {(s.ticker, s.side) for s in _scan(True)}
    assert on <= off, f"ON must be a subset of OFF; on={on} off={off}"
    # overconfident mid is dropped by the calibrated gate...
    assert ("KXHIGHTPHX-26JUN22-T100", "yes") in off
    assert ("KXHIGHTPHX-26JUN22-T100", "yes") not in on
    # ...but the strong market still survives (gate tightens, doesn't nuke all)
    assert ("KXHIGHTLAX-26JUN22-T75", "yes") in on


def test_signal_fair_prob_is_raw_regardless_of_flag():
    """Brier scores the model's RAW prediction — Signal.fair_prob must stay raw."""
    raw_by_ticker = {"KXHIGHTLAX-26JUN22-T75": 0.80, "KXHIGHTPHX-26JUN22-T100": 0.62}
    for flag in (False, True):
        for s in _scan(flag):
            if s.ticker in raw_by_ticker:
                assert s.fair_prob == raw_by_ticker[s.ticker], (flag, s.ticker, s.fair_prob)


if __name__ == "__main__":
    test_edge_fair_cents_off_is_raw()
    print("[PASS] OFF: _edge_fair_cents == raw fair cents")
    test_edge_fair_cents_failsafe_when_unfitted()
    print("[PASS] ON + unfitted curve: fail-safe → raw")
    test_edge_fair_cents_on_is_down_only()
    print("[PASS] ON + fitted curve: down-only (<= raw)")
    test_scan_on_is_subset_and_drops_overconfident()
    print("[PASS] scan ON ⊆ OFF; overconfident dropped, strong survives")
    test_signal_fair_prob_is_raw_regardless_of_flag()
    print("[PASS] Signal.fair_prob stays raw (Brier integrity)")
    print("\nAll calibrated-edge-gate tests pass.")

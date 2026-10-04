#!/usr/bin/env python3
"""Audit #7: the live daily/weekly-stop budget total must EXCLUDE non-weather positions so an
unrelated contract (e.g. KXUSAIRANAGREEMENT) can't contaminate or MASK the weather stop.
Audit #1: a RiskGate whose _load_state is mocked must not crash get_city_risk_multiplier."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from sync_live_positions import weather_budget_total_cents, _is_weather_ticker  # noqa: E402
from trader.risk import RiskGate  # noqa: E402


def test_is_weather_ticker():
    assert _is_weather_ticker("KXHIGHTNOLA-26JUN24-B92.5")
    assert _is_weather_ticker("KXLOWTNYC-26JUN24-B66.5")
    assert _is_weather_ticker("KXTEMPNYC-26JUN24-1200")
    assert not _is_weather_ticker("KXUSAIRANAGREEMENT-27-26SEP")
    assert not _is_weather_ticker("")


def test_budget_total_excludes_non_weather_value():
    state = {
        "available_cents": 100000, "portfolio_cents": 50000,
        "positions": [
            {"ticker": "KXHIGHTNOLA-26JUN24-B92.5", "market_exposure_cents": 30000},
            {"ticker": "KXUSAIRANAGREEMENT-27-26SEP", "market_exposure_cents": 20000},
        ],
    }
    assert weather_budget_total_cents(state) == 130000  # 100000 + 50000 - 20000 (Iran)


def test_all_weather_total_unchanged():
    state = {"available_cents": 100000, "portfolio_cents": 50000,
             "positions": [{"ticker": "KXLOWTNYC-26JUN24-B66.5", "market_exposure_cents": 50000}]}
    assert weather_budget_total_cents(state) == 150000


def test_non_weather_gain_cannot_mask_weather_loss():
    # Whole-account delta would be 0 (Iran +5000 masks weather -5000); weather-only must show -5000.
    t0 = weather_budget_total_cents({"available_cents": 100000, "portfolio_cents": 50000, "positions": [
        {"ticker": "KXHIGHTNOLA-26JUN24-B92.5", "market_exposure_cents": 30000},
        {"ticker": "KXUSAIRANAGREEMENT-27-26SEP", "market_exposure_cents": 20000}]})
    t1 = weather_budget_total_cents({"available_cents": 100000, "portfolio_cents": 50000, "positions": [
        {"ticker": "KXHIGHTNOLA-26JUN24-B92.5", "market_exposure_cents": 25000},
        {"ticker": "KXUSAIRANAGREEMENT-27-26SEP", "market_exposure_cents": 25000}]})
    assert t1 - t0 == -5000, "non-weather gain masked a weather loss"


def test_riskgate_city_multiplier_no_crash_without_loadstate():
    orig = RiskGate._load_state
    RiskGate._load_state = lambda self: None  # mock: rely on __post_init__ defensive defaults
    try:
        with tempfile.TemporaryDirectory() as d:
            g = RiskGate(_state_dir=Path(d))
            mult, _reason = g.get_city_risk_multiplier("NOLA")  # must NOT raise AttributeError (#1)
    finally:
        RiskGate._load_state = orig
    assert isinstance(mult, (int, float)) and mult == 1.0  # clean city → full multiplier


if __name__ == "__main__":
    test_is_weather_ticker();                              print("[PASS] weather ticker classification")
    test_budget_total_excludes_non_weather_value();        print("[PASS] budget total excludes non-weather value")
    test_all_weather_total_unchanged();                    print("[PASS] all-weather budget unchanged")
    test_non_weather_gain_cannot_mask_weather_loss();      print("[PASS] non-weather gain can't mask a weather loss")
    test_riskgate_city_multiplier_no_crash_without_loadstate(); print("[PASS] city multiplier no-crash w/o _load_state (#1)")
    print("\nAll weather-budget-filter + city-init tests pass.")

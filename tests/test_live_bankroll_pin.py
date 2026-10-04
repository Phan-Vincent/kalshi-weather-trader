"""Allocated-capital pin contract, using synthetic account values."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import os

import paper_trade  # noqa: E402


def test_unset_returns_default(monkeypatch):
    monkeypatch.delenv("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", raising=False)
    assert paper_trade._pinned_live_bankroll(150000) == 150000


def test_pin_overrides_account_cash(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", "100000")
    assert paper_trade._pinned_live_bankroll(150000) == 100000


def test_malformed_pin_falls_back(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", "not-a-number")
    assert paper_trade._pinned_live_bankroll(150000) == 150000


def test_non_positive_pin_falls_back(monkeypatch):
    monkeypatch.setenv("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", "0")
    assert paper_trade._pinned_live_bankroll(150000) == 150000
    monkeypatch.setenv("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", "-500")
    assert paper_trade._pinned_live_bankroll(150000) == 150000


def test_premium_live_arm_carries_the_pin():
    """The live arm's variants.json env actually sets the pin (wiring, not just code)."""
    import json
    v = json.loads((ROOT / "variants.json").read_text())
    arms = v.get("arms", v.get("variants")) if isinstance(v, dict) else v
    arm = next(a for a in arms if a.get("name") == "premium-live")
    assert int(arm["env"]["KALSHI_WEATHER_LIVE_BANKROLL_CENTS"]) > 0

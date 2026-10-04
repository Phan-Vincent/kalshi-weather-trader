#!/usr/bin/env python3
"""Per-city premium skip (edge decomposition, 2026-06-28): KALSHI_WEATHER_PREMIUM_SKIP_CITIES drops
premium quotes for the listed cities (those whose realized premium edge bleeds, e.g. SFO -8.6c/ct).
Default off → no change. Plain python3."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

_ENVKEYS = ("KALSHI_WEATHER_PREMIUM_MODE", "KALSHI_WEATHER_SPREAD_FILTER_OFF",
            "KALSHI_WEATHER_PREMIUM_SKIP_CITIES")


def test_premium_city_skip_drops_only_skipped_city():
    # two in-band premium markets (SFO + LAX); with SKIP_CITIES=SFO, SFO must vanish, LAX must remain.
    recs = {"records": [
        {"ticker": "KXHIGHTSFO-26JUN28-B68.5", "confidence": "high", "fair_prob": 0.5, "_market": {"yes_bid": 30, "yes_ask": 34}},
        {"ticker": "KXHIGHTLAX-26JUN28-B72.5", "confidence": "high", "fair_prob": 0.5, "_market": {"yes_bid": 30, "yes_ask": 34}},
    ]}
    saved = {k: os.environ.get(k) for k in _ENVKEYS}
    tmpd = tempfile.mkdtemp()
    fv = str(Path(tmpd, "fv.json"))
    Path(fv).write_text(json.dumps(recs))
    os.environ["KALSHI_WEATHER_PREMIUM_MODE"] = "1"
    os.environ["KALSHI_WEATHER_SPREAD_FILTER_OFF"] = "1"
    try:
        from trader.scanner import scan
        from trader.risk import RiskGate
        risk = RiskGate()
        os.environ.pop("KALSHI_WEATHER_PREMIUM_SKIP_CITIES", None)
        base = scan(fv, risk, mode="mm", top_n=50, bankroll_cents=500000)
        os.environ["KALSHI_WEATHER_PREMIUM_SKIP_CITIES"] = "SFO"
        skip = scan(fv, risk, mode="mm", top_n=50, bankroll_cents=500000)
        assert any("SFO" in s.ticker for s in base), "baseline must quote SFO"
        assert any("LAX" in s.ticker for s in base), "baseline must quote LAX"
        assert not any("SFO" in s.ticker for s in skip), "SFO must be skipped"
        assert any("LAX" in s.ticker for s in skip), "LAX must still be quoted (only SFO skipped)"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_premium_city_skip_drops_only_skipped_city()
    print("[PASS] PREMIUM_SKIP_CITIES=SFO drops SFO premium quotes, keeps the rest")
    print("\nPremium city-skip test passes.")

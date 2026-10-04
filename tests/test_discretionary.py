#!/usr/bin/env python3
"""Tests for bin/discretionary.py — discretionary journal/sizing/scorer pure logic (no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import discretionary as d


def test_analyze_entry_yes_and_no():
    y = d.analyze_entry(0.60, "yes", yes_bid=0.40, yes_ask=0.44)
    assert y["entry_dollars"] == 0.44 and y["market_implied"] == 0.42 and y["edge_prob"] == 0.18
    n = d.analyze_entry(0.60, "no", yes_bid=0.40, yes_ask=0.44)   # buy NO at 1-0.40=0.60; implied P(no)=0.58
    assert n["entry_dollars"] == 0.60 and n["market_implied"] == 0.58 and abs(n["edge_prob"] - 0.02) < 1e-9


def test_outcome_for_side():
    assert d.outcome_for_side("no", "no") == 1.0
    assert d.outcome_for_side("yes", "no") == 0.0
    assert d.outcome_for_side("", "no") is None


def test_brier_skill_items():
    # you said 0.9 and won → you beat the market's 0.6 (positive skill)
    win = [{"ticker": "A", "your_prob": 0.9, "market_implied": 0.6, "_outcome": 1.0}]
    assert abs(d.brier_skill_items(win)[0][1] - 0.15) < 1e-9   # (0.6-1)^2 - (0.9-1)^2 = 0.16-0.01
    lose = [{"ticker": "B", "your_prob": 0.9, "market_implied": 0.6, "_outcome": 0.0}]
    assert abs(d.brier_skill_items(lose)[0][1] - (-0.45)) < 1e-9  # 0.36 - 0.81


def test_calibration_table():
    scored = [{"your_prob": 0.9, "_outcome": 1.0}, {"your_prob": 0.95, "_outcome": 0.0},
              {"your_prob": 0.6, "_outcome": 1.0}]
    cal = d.calibration_table(scored)
    hi = [b for b in cal if b["range"] == "90%-101%"][0]
    assert hi["n"] == 2 and hi["you_said"] == 0.925 and hi["you_won"] == 0.5


def test_verdict_branches():
    # too early
    assert d._verdict([("A", 0.1)], 5).startswith("TOO EARLY")
    # >=20 calls, clearly positive skill → edge
    pos = [(f"E{i}", 0.05) for i in range(25)]
    assert d._verdict(pos, 25).startswith("EDGE DETECTED")
    # >=20 calls, clearly negative → negative
    neg = [(f"E{i}", -0.05) for i in range(25)]
    assert d._verdict(neg, 25).startswith("NEGATIVE")
    # >=20 calls, straddling 0 → no proven edge
    null = [(f"E{i}", 0.001 * ((i % 3) - 1)) for i in range(25)]
    assert d._verdict(null, 25).startswith("NO PROVEN EDGE")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    sys.exit(0 if not failed else 1)

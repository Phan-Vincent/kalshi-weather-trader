#!/usr/bin/env python3
"""Tests for bin/m3_multiday_taker.py — ROADMAP M3 multi-day taker probe.

Pure sim/verdict logic on synthetic markets (real 2–4d data is empty — Kalshi lists weather only
~0–1.7d out, which is the tool's headline finding). Covers taker P&L at the mid, the exact fee, the
per-market dedup, the threshold gate, and every verdict branch (SIGNAL / DEAD / NO-MARKETS).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import m3_multiday_taker as m3


def _snap(fair, mid, y, lead=3, hour=0, event="C-2026-07-05"):
    return {"asof": datetime(2026, 7, 2, hour, tzinfo=timezone.utc), "lead": lead,
            "fair": fair, "mid": mid, "y": float(y), "event": event}


def test_lead_days():
    asof = datetime(2026, 7, 10, 18, tzinfo=timezone.utc)   # NYC afternoon
    assert m3._lead_days("2026-07-13", asof, "NYC") == 3
    assert m3._lead_days("2026-07-11", asof, "NYC") == 1


def test_simulate_yes_win_and_fee():
    # model says YES (fair 0.70 > mid 0.50), outcome YES → gross +50¢, minus ~2¢ fee at 50¢ entry
    by = {"KXHIGHTLAX-26JUL05-B80.5": [_snap(0.70, 0.50, 1)]}
    trades = m3.simulate(by, threshold_c=0.0, spread_haircut_c=0.0)
    assert len(trades) == 1
    ev, net, gross, lead = trades[0]
    assert abs(gross - 50.0) < 1e-9
    assert abs(net - 48.0) < 1e-9          # fee = ceil(0.07*0.5*0.5)=2¢


def test_simulate_no_side_and_wrong():
    # model says NO (fair 0.30 < mid 0.50); outcome YES(1) → model wrong → gross negative
    by = {"KXHIGHTLAX-26JUL05-B80.5": [_snap(0.30, 0.50, 1)]}
    ev, net, gross, lead = m3.simulate(by, 0.0, 0.0)[0]
    assert gross < 0, gross                 # (mid - y)*100 = (0.5-1)*100 = -50


def test_threshold_gate_and_earliest_dedup():
    by = {"T": [_snap(0.60, 0.50, 1, hour=9), _snap(0.72, 0.50, 1, hour=3)]}  # |div| .10 then .22
    assert len(m3.simulate(by, threshold_c=25.0, spread_haircut_c=0.0)) == 0   # neither clears 25¢
    trades = m3.simulate(by, threshold_c=15.0, spread_haircut_c=0.0)          # only the .22 snap clears
    assert len(trades) == 1
    # earliest qualifying snapshot chosen (hour=3, the 0.72 one) — entry mid 0.50, gross +50
    assert abs(trades[0][2] - 50.0) < 1e-9


def test_spread_haircut_subtracts():
    by = {"T": [_snap(0.70, 0.50, 1)]}
    base = m3.simulate(by, 0.0, 0.0)[0][1]
    hair = m3.simulate(by, 0.0, spread_haircut_c=4.0)[0][1]
    assert abs((base - hair) - 4.0) < 1e-9


def test_verdict_signal_dead():
    # SIGNAL: model consistently right → net CI clears 0
    win = {f"M{i}": [_snap(0.70, 0.50, 1, event=f"E{i}")] for i in range(20)}
    cells = [{**m3._summ(m3.simulate(win, 0.0, 0.0)), "threshold": 0.0}]
    assert m3._verdict(cells, 10).startswith("SIGNAL"), cells
    # DEAD: model consistently wrong → gross upper bound negative
    lose = {f"M{i}": [_snap(0.70, 0.50, 0, event=f"E{i}")] for i in range(20)}
    cells = [{**m3._summ(m3.simulate(lose, 0.0, 0.0)), "threshold": 0.0}]
    assert m3._verdict(cells, 10).startswith("DEAD"), cells


def test_no_markets_verdict(monkeypatch=None):
    # evaluate() over an empty forecast log → structural NO-MARKETS verdict
    import tempfile, os
    p = Path(tempfile.mkdtemp()) / "empty.jsonl"
    p.write_text("")
    r = m3.evaluate(flog=p)
    assert r["n_markets"] == 0 and r["verdict"].startswith("NO MARKETS"), r["verdict"]


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    sys.exit(0 if not failed else 1)

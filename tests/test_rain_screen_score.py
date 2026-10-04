#!/usr/bin/env python3
"""Tests for bin/rain_screen_score.py — monthly-rain kill-screen read-out.

Pure scoring/verdict logic with a stubbed settlement (no network): the leak-free month-open selection,
the market-vs-FULL edge, the FULL-vs-BANKED firewall, and every verdict branch.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import rain_screen_score as sc


def _rec(city, month, thr, mkt, full, banked, leakfree=True, ts="2026-06-01T00:00:00+00:00"):
    return {"city": city, "month": month, "ticker": f"{city}-{month}-{thr}", "threshold_in": thr,
            "acis_station": f"K{city}", "leakfree_monthopen": leakfree, "ts_utc": ts,
            "market_prob": mkt, "full_prob": full, "banked_only_prob": banked}


def _stub(realized):  # every city-month resolves to `realized` inches
    return lambda sid, month: realized


def _make(n, mkt, full, banked, month_prefix="2026-0"):
    # n distinct city-months, threshold 3 with realized 5 → outcome=1 for all
    recs = []
    for i in range(n):
        recs.append(_rec(f"C{i}", "2026-05", 3.0, mkt, full, banked))
    return recs


def test_leakfree_selection_earliest():
    recs = [_rec("HOU", "2026-06", 3, 0.5, 0.5, 0.5, leakfree=True, ts="2026-06-02T00:00:00+00:00"),
            _rec("HOU", "2026-06", 3, 0.6, 0.6, 0.6, leakfree=True, ts="2026-06-01T00:00:00+00:00"),
            _rec("HOU", "2026-06", 3, 0.9, 0.9, 0.9, leakfree=False, ts="2026-06-15T00:00:00+00:00")]
    lf = sc.leakfree_monthopen(recs)
    assert len(lf) == 1 and lf[0]["market_prob"] == 0.6   # earliest leak-free, mid-month excluded


def test_verdict_signal():
    recs = _make(40, mkt=0.4, full=0.9, banked=0.4)     # outcome 1: FULL good, market+banked bad
    r = sc.score(recs, settle=_stub(5.0))
    assert r["edge_ci"][1] > 0 and r["firewall_ci"][1] > 0, r
    assert r["verdict"].startswith("SIGNAL"), r["verdict"]


def test_verdict_leak_observation():
    recs = _make(40, mkt=0.4, full=0.9, banked=0.9)     # FULL beats market, but banked==FULL → no forecast add
    r = sc.score(recs, settle=_stub(5.0))
    assert r["verdict"].startswith("LEAK/OBSERVATION"), r["verdict"]


def test_verdict_kill_leaning():
    recs = _make(40, mkt=0.85, full=0.3, banked=0.3)    # FULL worse than market → no edge
    r = sc.score(recs, settle=_stub(5.0))
    assert r["verdict"].startswith("KILL-LEANING"), r["verdict"]


def test_verdict_accruing_thin():
    recs = _make(5, mkt=0.4, full=0.9, banked=0.4)
    r = sc.score(recs, settle=_stub(5.0))
    assert r["verdict"].startswith("ACCRUING"), r["verdict"]


def test_current_month_excluded():
    # a snapshot for the current month must not be scored (month not complete)
    from datetime import datetime, timezone
    cur = datetime.now(timezone.utc).strftime("%Y-%m")
    recs = [_rec("HOU", cur, 3, 0.4, 0.9, 0.4)]
    r = sc.score(recs, settle=_stub(5.0))
    assert r["n_events"] == 0 and r["verdict"].startswith("ACCRUING"), r


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

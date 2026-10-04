#!/usr/bin/env python3
"""Tests for bin/leak_audit.py — ROADMAP P0-1 same-day-skill-vs-outcome-leak harness.

Covers the pure logic that decides the verdict (no network, no forecast-log): the hours-before-extreme
bucketing, the station-local→UTC instant math, and every branch of the leak verdict — GENUINE EDGE,
LEAK-DOMINATED, NO GENUINE EDGE (well-powered null), and ACCRUING. The end-to-end `analyze()` test
reproduces the real legacy-mode leak signature on synthetic rows: model worse than market with lead,
better only near the extreme.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import leak_audit as la


def test_stratum_boundaries():
    assert la._stratum(30.0) == ">=24h"
    assert la._stratum(24.0) == ">=24h"
    assert la._stratum(23.9) == "12-24h"
    assert la._stratum(12.0) == "12-24h"
    assert la._stratum(6.0) == "6-12h"          # ≥6h boundary → still leak-free pool
    assert la._stratum(5.9) == "2-6h"
    assert la._stratum(2.0) == "2-6h"
    assert la._stratum(1.9) == "0-2h"
    assert la._stratum(0.0) == "0-2h"
    assert la._stratum(-0.1) == "post"          # after the extreme → leak-prone


def test_local_instant_utc_uses_station_tz():
    # 17:00 America/Chicago on 2026-07-11 == 22:00Z (CDT, UTC-5).
    inst = la._local_instant_utc("2026-07-11", 17.0, "HOU")   # HOU → America/Chicago
    assert inst.tzinfo == timezone.utc
    assert (inst.hour, inst.minute) == (22, 0), inst
    # settlement instant = local midnight ending the target day = 00:00 CDT next day = 05:00Z.
    settle = la._settle_instant_utc("2026-07-11", "HOU")
    assert (settle.date().isoformat(), settle.hour) == ("2026-07-12", 5), settle


def _mk(hbe, model_b, market_b, ev):
    return {"event": ev, "model_b": model_b, "market_b": market_b, "hbe": hbe, "hts": hbe + 7,
            "same_day": hbe < 12}


def _rows(hbe, mean_diff, n_events, spread=0.02):
    """n_events synthetic rows at `hbe`, each a distinct event, with market_b-model_b ≈ mean_diff.
    Deterministic spread (no RNG — the bootstrap seed is fixed) so the CI is stable across runs."""
    out = []
    for i in range(n_events):
        d = mean_diff + spread * ((i % 5) - 2) / 2.0   # small symmetric jitter around the mean
        model_b, market_b = 0.20, 0.20 + d
        out.append(_mk(hbe, model_b, market_b, f"EV{hbe}-{i}"))
    return out


def test_analyze_detects_leak_signature():
    # leak: model WORSE than market with ≥6h lead (diff<0), BETTER only <2h before extreme (diff>0).
    scored = _rows(10.0, -0.10, 40) + _rows(1.0, +0.10, 40)
    res = la.analyze(scored, min_events=10)
    assert res["clean"]["ci_diff"][2] < 0, res["clean"]["ci_diff"]      # leak-free CI entirely negative
    assert res["suspect"]["ci_diff"][1] > 0, res["suspect"]["ci_diff"]  # near-extreme CI entirely positive
    assert res["verdict"].startswith("LEAK-DOMINATED"), res["verdict"]


def test_analyze_genuine_edge():
    # model beats market even with ≥6h lead → genuine, leak-free edge.
    scored = _rows(10.0, +0.10, 40) + _rows(1.0, +0.10, 20)
    res = la.analyze(scored, min_events=10)
    assert res["verdict"].startswith("GENUINE EDGE"), res["verdict"]


def test_analyze_well_powered_null():
    # leak-free ≈ 0 with many events and no near-extreme spike → confident "no genuine edge".
    scored = _rows(10.0, 0.0, 60, spread=0.01) + _rows(1.0, 0.0, 20, spread=0.01)
    res = la.analyze(scored, min_events=10)
    assert res["verdict"].startswith("NO GENUINE EDGE"), res["verdict"]


def test_analyze_accruing_when_thin():
    scored = _rows(10.0, -0.10, 4)   # only 4 leak-free events → below the ruling floor
    res = la.analyze(scored, min_events=10)
    assert res["verdict"].startswith("ACCRUING"), res["verdict"]


def test_verdict_helpers():
    assert la._pos((0.05, 0.01, 0.09)) and not la._pos((0.0, -0.01, 0.09))
    assert la._neg((-0.05, -0.09, -0.01)) and not la._neg((0.0, -0.09, 0.01))
    assert la._null((0.0, -0.03, 0.03)) and not la._null((0.05, 0.01, 0.09))


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

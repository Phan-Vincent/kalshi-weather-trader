#!/usr/bin/env python3
"""Leak-free skill recompute tests (quant review 2026-07-01, experiment #2).

Pins the two leak corrections and the honest CI:
  - same-day rows (resolution day <= asof STATION-LOCAL day) are dropped leak-free but kept raw;
  - pre-window rows (asof <= cutoff) are dropped leak-free but kept raw;
  - the NY series ("KXHIGHNY" -> parse "NY") uses America/New_York, not the UTC fallback;
  - non-weather (Iran) and unsettled (target >= today) rows are always dropped;
  - the verdict follows the event-clustered Brier-difference CI (SURVIVES / NO SKILL / INCONCLUSIVE).
No network, no cache — score() is pure and actuals come from a dict.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import leakfree_skill as lf  # noqa: E402

CUTOFF = datetime.fromisoformat("2026-06-30T01:21:00+00:00")
TODAY = "2026-07-05"


def _row(city, date, asof, fp, mid, thr=80.0, mode="forecast", series="KXHIGH"):
    return {"asof_utc": asof, "ticker": f"{series}{city}-26{date}-B{thr}",
            "fair_prob": fp, "market_mid": mid, "mode": mode,
            "bin_kind": "above", "thr": thr, "lo": None, "hi": None}


def _actual_of(_city, _mtype, _date):
    return 90.0  # > thr 80 => outcome YES(1) for every above-market


def test_same_day_leak_dropped_leakfree_kept_raw():
    # target 2026-07-01, asof 2026-07-01T15Z (HOU local 2026-07-01) => resolution day <= asof local
    rows = [_row("HOU", "JUL01", "2026-07-01T15:00:00+00:00", 0.9, 0.5)]
    lfree = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)
    raw = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=False)
    assert lfree.get("forecast", {"n": 0})["n"] == 0
    assert lfree["_dropped"]["same_day"] == 1
    assert raw["forecast"]["n"] == 1


def test_pre_window_dropped_leakfree_kept_raw():
    # asof before the cutoff, but a genuine 2-day-ahead (non-same-day) forecast
    rows = [_row("HOU", "JUL03", "2026-06-25T15:00:00+00:00", 0.9, 0.5)]
    lfree = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)
    raw = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=False)
    assert lfree.get("forecast", {"n": 0})["n"] == 0
    assert lfree["_dropped"]["pre_window"] == 1
    assert raw["forecast"]["n"] == 1


def test_ny_uses_local_tz_not_utc_fallback():
    # asof 2026-07-02T03:00Z: UTC day is 07-02 but NY (EDT) local day is 07-01. A target of
    # 07-02 is NOT a same-day leak under the correct tz (07-02 > 07-01) and must be KEPT.
    rows = [_row("NY", "JUL02", "2026-07-02T03:00:00+00:00", 0.9, 0.5)]
    lfree = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)
    assert lfree["forecast"]["n"] == 1        # UTC fallback would have dropped it (07-02 <= 07-02)
    assert lfree["_dropped"]["same_day"] == 0


def test_nonweather_and_unsettled_always_dropped():
    rows = [
        {"asof_utc": "2026-07-02T12:00:00+00:00", "ticker": "KXUSAIRANAGREEMENT-27-26SEP",
         "fair_prob": 0.6, "market_mid": 0.4, "mode": "forecast",
         "bin_kind": "above", "thr": 1, "lo": None, "hi": None},                 # non-weather
        _row("HOU", "JUL30", "2026-07-02T12:00:00+00:00", 0.9, 0.5),             # target >= today
    ]
    lfree = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)
    assert lfree.get("forecast", {"n": 0})["n"] == 0


# asof precedes the target day so these are genuine ahead-of-time forecasts that survive leak-free
_ASOF = "2026-07-01T12:00:00+00:00"
_CITIES = ("HOU", "LAX", "BOS", "PHX", "MIA", "SEA")  # 6 cities x 2 dates = 12 events (> the floor)


def test_verdict_skill_survives():
    # every event: outcome=1, model near-certain (low brier), market at 0.5 (brier 0.25) => diff>0
    rows = [_row(c, d, _ASOF, fp, 0.5)
            for d in ("JUL02", "JUL03")
            for c, fp in zip(_CITIES, (0.85, 0.9, 0.95, 0.88, 0.92, 0.86))]
    r = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)["forecast"]
    assert r["n_events"] >= lf.MIN_EVENTS_FOR_VERDICT
    assert r["skill"] > 0 and r["ci_diff"][1] > 0                # CI lower bound excludes 0
    assert lf._verdict(r).startswith("SKILL SURVIVES")


def test_verdict_no_skill():
    rows = [_row(c, d, _ASOF, 0.1, 0.5) for d in ("JUL02", "JUL03") for c in _CITIES]
    r = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)["forecast"]
    assert r["ci_diff"][2] < 0                                   # CI upper bound below 0
    assert lf._verdict(r).startswith("NO SKILL")


def test_verdict_inconclusive_when_ci_spans_zero():
    # half events model-better, half model-worse -> Brier-diff CI straddles 0
    good = [_row(c, "JUL02", _ASOF, 0.9, 0.5) for c in _CITIES]
    bad = [_row(c, "JUL03", _ASOF, 0.1, 0.5) for c in _CITIES]
    r = lf.score(good + bad, _actual_of, CUTOFF, TODAY, leakfree=True)["forecast"]
    lo, hi = r["ci_diff"][1], r["ci_diff"][2]
    assert lo < 0 < hi
    assert lf._verdict(r).startswith("INCONCLUSIVE")


def test_single_event_does_not_flash_false_survives():
    # 3 bins of ONE event, model beats market => n_events==1 => degenerate zero-width CI. The floor
    # must keep this ACCRUING, not rule "SKILL SURVIVES" off a single day.
    rows = [_row("HOU", "JUL02", _ASOF, 0.9, 0.5, thr=t) for t in (80.0, 82.0, 84.0)]
    r = lf.score(rows, _actual_of, CUTOFF, TODAY, leakfree=True)["forecast"]
    assert r["n_events"] == 1
    assert lf._verdict(r).startswith("ACCRUING")


def test_ny_series_canonicalized_to_nyc_and_scored():
    # KXHIGHNY parses city "NY"; STATIONS only keys "NYC", so without canonicalization get_actual
    # returns None forever and the row is silently unscoreable. Prove the tool queries "NYC".
    seen = []

    def actual_spy(city, _mtype, _date):
        seen.append(city)
        return 90.0 if city == "NYC" else None

    rows = [_row("NY", "JUL02", _ASOF, 0.9, 0.5)]
    r = lf.score(rows, actual_spy, CUTOFF, TODAY, leakfree=True)
    assert r["forecast"]["n"] == 1            # scored (would be 0 without the NY->NYC canonicalize)
    assert "NYC" in seen and "NY" not in seen

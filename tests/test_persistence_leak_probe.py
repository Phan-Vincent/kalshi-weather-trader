#!/usr/bin/env python3
"""Persistence leak probe tests (quant review 2026-07-01, experiment #8).

Pins the persistence-SPECIFIC leak definition (UTC-yesterday ≥ market_date, NOT the broad
same-day-forecast window), the fix-verification split (now-legit prior day vs guard-skipped),
the leaked-magnitude recompute (old fetch ≈ resolution actual), the old-leak-vs-clean skill
partition, the NY→NYC canonicalization, and the verdict logic. Hermetic — synthetic rows, stubbed
actuals + climo, no network.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import persistence_leak_probe as p  # noqa: E402
from model.persistence import persistence_exceedance_prob, persistence_weight  # noqa: E402

TODAY = "2026-07-10"


def _dt(s):
    return datetime.fromisoformat(s)


def _row(city, date, asof, fp=0.9, mid=0.5, thr=80.0, series="KXHIGH"):
    return {"asof_utc": asof, "ticker": f"{series}{city}-26{date}-B{thr}",
            "fair_prob": fp, "market_mid": mid, "bin_kind": "above", "thr": thr, "lo": None, "hi": None}


class _Climo:
    def exceedance_prob(self, city, mmdd, mtype, thr):
        return 0.3

    def below_prob(self, city, mmdd, mtype, thr):
        return 0.7

    def between_prob(self, city, mmdd, mtype, lo, hi):
        return 0.2


def test_utc_yesterday_and_old_leak_boolean():
    # 02Z Jul 2, market Jul 1: UTC-yesterday == Jul 1 == market_date → OLD leak
    assert p._utc_yesterday(_dt("2026-07-02T02:00:00+00:00")) == "2026-07-01"
    assert p.old_leak(_dt("2026-07-02T02:00:00+00:00"), "2026-07-01") is True
    # midday: UTC-yesterday is a genuine prior day → not a leak
    assert p.old_leak(_dt("2026-07-01T15:00:00+00:00"), "2026-07-02") is False
    # past-dated market: UTC-yesterday later than market → leak (guard territory)
    assert p.old_leak(_dt("2026-07-05T02:00:00+00:00"), "2026-07-01") is True


def test_guard_fires_only_for_past_dated():
    # same-day 02Z leak: station-local yesterday is a genuine prior day → guard does NOT fire
    assert p.guard_fires(_dt("2026-07-02T02:00:00+00:00"), "LAX", "2026-07-01") is False
    # past-dated: station-local yesterday >= market_date → guard fires
    assert p.guard_fires(_dt("2026-07-05T02:00:00+00:00"), "LAX", "2026-07-01") is True


def test_instrument_split():
    rows = [
        _row("LAX", "JUL01", "2026-07-02T02:00:00+00:00"),   # old-leak, same-day → now-legit
        _row("LAX", "JUL01", "2026-07-05T02:00:00+00:00"),   # old-leak, past-dated → guard-skipped
        _row("LAX", "JUL03", "2026-07-01T12:00:00+00:00"),   # clean (future market at asof)
    ]
    inst = p.instrument(rows)
    assert inst["n"] == 3 and inst["leak"] == 2
    assert inst["now_legit"] == 1 and inst["guard_skipped"] == 1
    assert inst["station_local_fix_engaged"] is True


def test_station_local_fix_signal_is_falsifiable(monkeypatch):
    # A regression of the station-local date fn back to UTC-yesterday would push the same-day-leak
    # row into guard-skipped and collapse now_legit to 0 — proving the signal is not a tautology.
    rows = [_row("LAX", "JUL01", "2026-07-02T02:00:00+00:00")]   # old-leak same-day → normally now-legit
    assert p.instrument(rows)["now_legit"] == 1
    monkeypatch.setattr(p, "_local_yesterday", lambda asof, city: p._utc_yesterday(asof))  # regress
    inst = p.instrument(rows)
    assert inst["now_legit"] == 0 and inst["guard_skipped"] == 1
    assert inst["station_local_fix_engaged"] is False


def test_leaked_magnitude_formula_and_sign():
    row = _row("LAX", "JUL01", "2026-07-02T02:00:00+00:00")   # utc-yesterday == market → same-day leak
    mag = p.leaked_magnitude([row], lambda c, m, d: 90.0, _Climo(), TODAY)   # actual 90 > thr 80
    assert mag["n"] == 1
    w = persistence_weight(p.hours_to_close(_dt("2026-07-02T02:00:00+00:00"), "2026-07-01"))
    expected = abs(w * (persistence_exceedance_prob(80.0, 90.0, "daily_high") - 0.3))
    assert abs(mag["delta_median_pp"] - expected) < 1e-9
    # actual 90 => outcome YES; persist_p(90) > climo => prior pulled TOWARD truth (positive)
    assert mag["mean_pull_toward_outcome_pp"] > 0


def test_magnitude_excludes_non_sameday_leak():
    # past-dated old-leak (utc-yesterday 07-04 != market 07-01) is NOT counted in magnitude
    row = _row("LAX", "JUL01", "2026-07-05T02:00:00+00:00")
    assert p.leaked_magnitude([row], lambda c, m, d: 90.0, _Climo(), TODAY)["n"] == 0


def test_skill_partition_buckets():
    rows = [
        _row("LAX", "JUL01", "2026-07-02T02:00:00+00:00"),   # old-leak
        _row("BOS", "JUL03", "2026-07-01T12:00:00+00:00"),   # clean
    ]
    r = p.skill_partition(rows, lambda c, m, d: 90.0, TODAY)
    assert r["leak"]["n_events"] == 1 and r["clean"]["n_events"] == 1


def test_ny_alias_routes_to_nyc():
    # KXHIGHNY parses city "NY"; actuals/climo are keyed "NYC" — must canonicalize or the row drops
    seen = []

    def spy(city, mtype, date):
        seen.append(city)
        return 90.0 if city == "NYC" else None

    rows = [_row("NY", "JUL01", "2026-07-02T02:00:00+00:00")]
    r = p.skill_partition(rows, spy, TODAY)
    assert r["leak"]["n"] == 1 and "NYC" in seen and "NY" not in seen


def test_verdict_materiality_branches():
    def sk(leak_ci, clean_ci, ln=20, cn=20):
        return {"leak": {"n_events": ln, "ci_diff": leak_ci},
                "clean": {"n_events": cn, "ci_diff": clean_ci}}
    inst = {"leak": 6, "now_legit": 5, "guard_skipped": 1, "station_local_fix_engaged": True}
    # leak beats market (CI excludes 0) while clean does not → CONFIRMED
    v = p._verdict(inst, sk((0.02, 0.01, 0.03), (0.0, -0.02, 0.02)))
    assert v["materiality"].startswith("SKILL-INFLATION CONFIRMED")
    # both span 0 → no clear inflation
    v2 = p._verdict(inst, sk((0.01, -0.01, 0.03), (0.0, -0.02, 0.02)))
    assert v2["materiality"].startswith("NO CLEAR SKILL-INFLATION")
    # too few events → insufficient
    v3 = p._verdict(inst, sk((0.02, 0.01, 0.03), (0.0, -0.02, 0.02), ln=4))
    assert v3["materiality"].startswith("SKILL-INFLATION: INSUFFICIENT DATA")
    # projection describes the split + points to the real verification, and never over-claims on 0 rows
    assert "dispositions all 6" in v["fix_projection"] and "test_persistence_leak_guard" in v["fix_projection"]
    empty = p._verdict({"leak": 0, "now_legit": 0, "guard_skipped": 0, "station_local_fix_engaged": False},
                       sk((None, None, None), (None, None, None), ln=0, cn=0))
    assert "nothing to project" in empty["fix_projection"]

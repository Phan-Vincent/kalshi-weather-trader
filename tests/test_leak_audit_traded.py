#!/usr/bin/env python3
"""Tests for bin/leak_audit_traded.py — ROADMAP P0-1 closed on the TRADED bins.

No network / no real logs: synthetic settlement records + a synthetic market_mid index exercise the
decision-time replay — the market-source selection (mid vs entry vs auto-fallback), the parse_ticker
recovery when market_parsed is absent (older pre-guard records), the since-cutoff, the nearest-snapshot
join, and that a synthetic leak dataset produces a LEAK-DOMINATED verdict via the shared analyze().
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import leak_audit_traded as lat


def _rec(ticker, side, result, fair, entry, opened, market_parsed=None):
    r = {"ticker": ticker, "side": side, "settlement_result": result, "fair_prob_at_open": fair,
         "entry_cents": entry, "opened_utc": opened}
    if market_parsed is not None:
        r["market_parsed"] = market_parsed
    return r


def test_entry_implied_yes_side():
    assert lat._entry_implied_yes({"entry_cents": 53, "side": "yes"}) == 0.53
    assert abs(lat._entry_implied_yes({"entry_cents": 53, "side": "no"}) - 0.47) < 1e-9
    assert lat._entry_implied_yes({"side": "yes"}) is None


def test_city_type_date_falls_back_to_ticker():
    # market_parsed present → use it
    r = _rec("KXHIGHTHOU-26JUL11-B93.5", "yes", "no", 0.5, 40, "2026-07-11T12:00:00+00:00",
             market_parsed={"city_code": "HOU", "market_type": "daily_high", "date_iso": "2026-07-11"})
    assert lat._city_type_date(r)[1:] == ("HOU", "daily_high", "2026-07-11")
    # market_parsed absent → recover from the ticker (the pre-guard June records)
    r2 = _rec("KXLOWTDEN-26JUN17-B59.5", "yes", "no", 0.5, 40, "2026-06-17T12:00:00+00:00")
    city, code, mtype, date_iso = lat._city_type_date(r2)
    assert (code, mtype, date_iso) == ("DEN", "daily_low", "2026-06-17"), (code, mtype, date_iso)


def test_nearest_mid_picks_closest_snapshot():
    t = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    idx = {"TK": [(t - timedelta(hours=2), 0.3), (t + timedelta(minutes=5), 0.6),
                  (t + timedelta(hours=3), 0.9)]}
    mid, gap = lat._nearest_mid(idx, "TK", t)
    assert mid == 0.6 and abs(gap - 5.0) < 1e-6, (mid, gap)
    assert lat._nearest_mid(idx, "MISSING", t) == (None, None)


def _leak_recs(n, mtype="daily_high"):
    """n same-city events: with ≥6h lead the model is WORSE than the entry price; <2h before the extreme
    it 'beats' it (the leak/selection signature). HIGH extreme ref = 17:00 local."""
    recs = []
    base = "2026-07-05"
    for i in range(n):
        # ≥6h-lead trade (08:00 UTC ≈ well before extreme) — model far from outcome, entry close
        recs.append(_rec(f"KXHIGHT{'LAX'}-26JUL05-B80.5", "yes", "no", 0.60, 40,
                         f"2026-07-05T08:00:00+00:00",
                         market_parsed={"city_code": "LAX", "market_type": mtype, "date_iso": base}))
        # <2h trade (near 17:00 local = ~00:00 UTC next day for LAX) — model near outcome, entry far
        recs.append(_rec(f"KXHIGHT{'LAX'}-26JUL05-B80.5", "yes", "no", 0.02, 80,
                         f"2026-07-05T23:30:00+00:00",
                         market_parsed={"city_code": "LAX", "market_type": mtype, "date_iso": base}))
    # give each pair a distinct event so clustering has >1 cluster
    for i, r in enumerate(recs):
        d = f"2026-07-{5 + i//2:02d}"
        r["ticker"] = f"KXHIGHTLAX-26JUL{5 + i//2:02d}-B80.5"
        r["market_parsed"]["date_iso"] = d
        r["opened_utc"] = r["opened_utc"].replace("2026-07-05", d)
    return recs


def test_build_scored_market_source_and_leak_verdict():
    recs = _leak_recs(24)
    # entry-implied market: near-extreme model beats our (bad) entry price → leak signature
    scored, meta = lat.build_scored(recs, idx={}, market_src="entry", since_dt=None)
    assert meta["n"] == len(recs) and meta["mkt_from_entry"] == len(recs)
    res = lat.la.analyze(scored, min_events=10)
    assert res["clean"]["ci_diff"][2] <= 0.02      # ≥6h lead: model NOT better than market
    assert res["suspect"]["ci_diff"][1] > 0        # <2h: model beats market (leak)
    assert res["verdict"].startswith("LEAK-DOMINATED"), res["verdict"]


def test_since_cutoff_excludes_pre_guard():
    recs = _leak_recs(4)
    cutoff = datetime(2026, 7, 6, tzinfo=timezone.utc)   # excludes the 2026-07-05 pair, keeps later
    _, meta = lat.build_scored(recs, idx={}, market_src="entry", since_dt=cutoff)
    assert meta["pre_cutoff"] >= 1 and meta["n"] < len(recs), meta


def test_mid_source_drops_unmatched():
    recs = _leak_recs(2)
    scored, meta = lat.build_scored(recs, idx={}, market_src="mid", since_dt=None)  # empty idx → no mids
    assert meta["n"] == 0 and meta["no_market"] == len(recs), meta


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

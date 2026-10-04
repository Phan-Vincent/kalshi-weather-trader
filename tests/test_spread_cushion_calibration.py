#!/usr/bin/env python3
"""Spread-cushion calibration trigger tests (proposal #4 follow-up).

Pins the pre-registered mechanism: net EV/ct is joined to spread_cents_at_post and bucketed by
spread; a bucket is 'confidently losing' only with >= MIN_EVENTS events AND an anytime-valid CS
upper bound < 0; the recommendation is hi+1 of the widest CONTIGUOUS confidently-losing run from the
tightest bucket, and a not-yet-decisive bucket STOPS the run (never cut an unmeasured band); fills
without spread telemetry are excluded; the trigger fires once and is immutable. Hermetic tmp dirs.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import spread_cushion_calibration as sc  # noqa: E402


def _write(sd, posted, settle):
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(r) for r in posted))
    (sd / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in settle))


def _fills(specs):
    """specs = [(n_events, spread, base_pnl_per_ct), ...] → (posted_live rows, settlement rows).
    limit_price_cents == entry_cents (40) so the fill-price join attributes the spread."""
    posted, settle = [], []
    e = 0
    for n, spread, base in specs:
        for i in range(n):
            tk = f"KXHIGHTNY-26JUL{e:02d}-B80.5"; e += 1
            posted.append({"event": "posted_live", "ticker": tk, "side": "yes",
                           "limit_price_cents": 40, "spread_cents_at_post": spread})
            settle.append({"ticker": tk, "side": "yes", "qty": 2,
                           "pnl_cents": (base + (i % 5 - 2) * 4) * 2, "entry_cents": 40})
    return posted, settle


def test_requote_attributes_to_fill_cycle_spread(tmp_path):
    # each market is FIRST posted wide (spread 10 @ 55c) then re-quoted tight (spread 2 @ 40c) and
    # fills at 40c. The loss must attribute to the FILL-cycle spread (2 → 1-2c bucket), not the stale
    # first post (10 → 9+c). Without the fill-price join this would land in 9+c and never trigger.
    posted, settle = [], []
    for e in range(12):
        tk = f"KXHIGHTNY-26JUL{e:02d}-B80.5"
        posted.append({"event": "posted_live", "ticker": tk, "side": "yes",
                       "limit_price_cents": 55, "spread_cents_at_post": 10})
        posted.append({"event": "posted_live", "ticker": tk, "side": "yes",
                       "limit_price_cents": 40, "spread_cents_at_post": 2})
        settle.append({"ticker": tk, "side": "yes", "qty": 2,
                       "pnl_cents": (-30 + (e % 5 - 2) * 4) * 2, "entry_cents": 40})
    _write(tmp_path, posted, settle)
    r = sc.evaluate(tmp_path)
    b12 = next(b for b in r["buckets"] if b["bucket"] == "1-2c")
    b9 = next(b for b in r["buckets"] if b["bucket"] == "9+c")
    assert b12["n_events"] == 12 and b12["confidently_losing"]   # attributed to fill spread 2
    assert b9["n_events"] == 0                                    # stale wide post NOT counted
    assert r["recommend_min_spread"] == 3


def test_multi_bin_fills_cluster_to_one_event(tmp_path):
    # 12 events x 3 bins (same series+city+date, different strike) → 36 fills but 12 EVENTS
    posted, settle = [], []
    for e in range(12):
        for b in range(3):
            tk = f"KXHIGHTNY-26JUL{e:02d}-B{80 + b}.5"
            posted.append({"event": "posted_live", "ticker": tk, "side": "yes",
                           "limit_price_cents": 40, "spread_cents_at_post": 2})
            # per-event mean varies (the +(b-1) bin term averages out) so the CS isn't degenerate
            settle.append({"ticker": tk, "side": "yes", "qty": 2,
                           "pnl_cents": (-30 + (e % 5 - 2) * 4 + (b - 1)) * 2, "entry_cents": 40})
    _write(tmp_path, posted, settle)
    b = next(x for x in sc.evaluate(tmp_path)["buckets"] if x["bucket"] == "1-2c")
    assert b["n_fills"] == 36 and b["n_events"] == 12            # bins collapse to events
    assert b["confidently_losing"]


def test_recommend_contiguous_losing_run(tmp_path):
    # [1-2] and [3-4] confidently losing, [5-6] positive → cut spread<5 → recommend 5
    posted, settle = _fills([(12, 2, -30), (12, 4, -30), (12, 6, +12)])
    _write(tmp_path, posted, settle)
    r = sc.evaluate(tmp_path)
    assert r["recommend_min_spread"] == 5, [(b["bucket"], b["confidently_losing"]) for b in r["buckets"]]


def test_run_stops_at_nondecisive_bucket(tmp_path):
    # [1-2] losing, [3-4] losing but UNDER-powered (5 events), [5-6] losing → run stops at [3-4] → 3
    posted, settle = _fills([(12, 2, -30), (5, 4, -30), (12, 6, -30)])
    _write(tmp_path, posted, settle)
    r = sc.evaluate(tmp_path)
    assert r["recommend_min_spread"] == 3


def test_no_losing_bucket_no_cut(tmp_path):
    posted, settle = _fills([(12, 2, +10), (12, 6, +10)])
    _write(tmp_path, posted, settle)
    assert sc.evaluate(tmp_path)["recommend_min_spread"] == 0


def test_all_buckets_losing_flag(tmp_path):
    posted, settle = _fills([(12, 2, -30), (12, 4, -30), (12, 6, -30), (12, 8, -30), (12, 11, -30)])
    _write(tmp_path, posted, settle)
    r = sc.evaluate(tmp_path)
    assert r["all_buckets_losing"] is True and r["recommend_min_spread"] == 13


def test_join_excludes_fills_without_spread_telemetry(tmp_path):
    posted, settle = _fills([(12, 2, -30)])
    # add a settled fill with NO matching posted_live spread → must be excluded from all buckets
    settle.append({"ticker": "KXHIGHTLAX-26JUN01-B70.5", "side": "yes", "qty": 2,
                   "pnl_cents": -200, "entry_cents": 40})
    _write(tmp_path, posted, settle)
    r = sc.evaluate(tmp_path)
    assert r["total_events_with_spread"] == 12          # the un-tagged fill isn't counted


def test_cs_neg_degenerate_guard():
    assert sc._cs_neg((-5.0, -5.0, -5.0)) is False       # zero-width CS is not decisive
    assert sc._cs_neg((-5.0, -9.0, -1.0)) is True
    assert sc._cs_neg((0.0, -3.0, 3.0)) is False


def test_trigger_fires_once_and_is_immutable(monkeypatch, tmp_path):
    sd = tmp_path / "live-premium"
    monkeypatch.setattr(sc, "STATE_DIR", sd)
    monkeypatch.setattr(sc, "STATE_PATH", sd / "spread-cushion-calibration.json")
    posted, settle = _fills([(12, 2, -30), (12, 6, +12)])   # cut spread<3 → recommend 3
    _write(sd, posted, settle)
    sc.main()
    st = json.loads((sd / "spread-cushion-calibration.json").read_text())
    assert st["review_triggered"] is not None and st["review_triggered"]["recommend_min_spread"] == 3
    locked = st["review_triggered"]["utc"]
    # data later turns all-positive — the locked review recommendation must not change
    posted2, settle2 = _fills([(12, 2, +20), (12, 6, +20)])
    _write(sd, posted2, settle2)
    sc.main()
    st2 = json.loads((sd / "spread-cushion-calibration.json").read_text())
    assert st2["review_triggered"]["recommend_min_spread"] == 3 and st2["review_triggered"]["utc"] == locked


def test_accruing_no_trigger_when_empty(monkeypatch, tmp_path):
    sd = tmp_path / "live-premium"
    monkeypatch.setattr(sc, "STATE_DIR", sd)
    monkeypatch.setattr(sc, "STATE_PATH", sd / "spread-cushion-calibration.json")
    _write(sd, [], [])
    sc.main()
    st = json.loads((sd / "spread-cushion-calibration.json").read_text())
    assert st["review_triggered"] is None and st["last_recommendation"] == 0


def test_fmt_single_event_bucket_no_crash():
    # asymp_confseq returns (point, None, None) for a single-event bucket — the report row must format
    # the point without raising (regression: previously TypeError on formatting None as +.2f).
    assert sc._fmt((76.33, None, None)) == "+76.33¢ [n/a]"
    assert sc._fmt((None, None, None)) == "n/a"
    assert sc._fmt(None) == "n/a"
    assert sc._fmt((3.5, 3.5, 3.5)) == "+3.50¢ [+3.50, +3.50]"


def test_main_single_event_bucket_runs_and_saves(monkeypatch, tmp_path):
    # A bucket whose fills are all ONE event (5-6c here) makes asymp_confseq return (point, None, None).
    # main() must print that row and reach _save_state — before the fix it crashed at the report loop,
    # so state never refreshed. Asserts the full print loop + state write are exercised end to end.
    sd = tmp_path / "live-premium"
    monkeypatch.setattr(sc, "STATE_DIR", sd)
    monkeypatch.setattr(sc, "STATE_PATH", sd / "spread-cushion-calibration.json")
    posted, settle = _fills([(1, 6, +76)])              # single event, spread 6 → 5-6c bucket
    _write(sd, posted, settle)
    assert sc.main() == 0
    st = json.loads((sd / "spread-cushion-calibration.json").read_text())
    assert "last_run_utc" in st and st["last_recommendation"] == 0   # not enough events to rule → 0

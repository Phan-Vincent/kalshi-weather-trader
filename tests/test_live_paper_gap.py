#!/usr/bin/env python3
"""Paired live-vs-paper gap tests (quant review 2026-07-01, experiment #6).

Pins the landmines the synthesis spec called out: ticker-only join (live rows carry
market_close_time=""), per-lot→per-contract normalization, the TOTAL=EXEC+SELECTION identity,
gap/ghost-row exclusion, joint/paper-only/live-only set derivation, side-disagreement flagging,
graceful degeneracy, and the four-way verdict classifier. Hermetic — synthetic settlement logs in
tmp dirs, no live state, no network.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import live_paper_gap as lpg  # noqa: E402


def _rec(ticker, side, qty, pnl_cents, close="", gap=False):
    r = {"ticker": ticker, "side": side, "qty": qty, "pnl_cents": pnl_cents,
         "entry_cents": 40, "market_close_time": close}
    if gap:
        r.update(gap_settled=True, qty=0, side=None)
    return r


def _arm(tmp, name, rows):
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return d


# distinct events (series+city+date); strike suffix is dropped by _event
T = ["KXHIGHHOU-26JUL01-B80.5", "KXHIGHLAX-26JUL02-B90.5", "KXHIGHBOS-26JUL03-B70.5",
     "KXHIGHPHX-26JUL04-B100.5", "KXHIGHMIA-26JUL05-B88.5", "KXHIGHSEA-26JUL06-B75.5",
     "KXHIGHDEN-26JUL07-B60.5"]


def test_ticker_join_survives_empty_live_close_time(tmp_path):
    # paper rows carry a close_time, live rows carry "" (as real live rows do). A (ticker,close)
    # join would match 0; the tool must join on ticker only.
    paper = _arm(tmp_path, "paper", [_rec(t, "yes", 10, 200, close="2026-07-08T00:00:00Z") for t in T[:6]])
    live = _arm(tmp_path, "live", [_rec(t, "yes", 2, 20, close="") for t in T[:6]])
    r = lpg.evaluate(paper, live)
    assert r["execution"]["matched_markets"] == 6      # 0 if the close-time key were used


def test_per_lot_normalized_to_per_contract(tmp_path):
    # paper +20c/ct (qty10 pnl200) vs live +10c/ct (qty2 pnl20) -> EXEC = +10c/ct, not the raw +180
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 200)])
    live = _arm(tmp_path, "live", [_rec(T[0], "yes", 2, 20)])
    r = lpg.evaluate(paper, live)
    assert abs(r["execution"]["ci"][0] - 10.0) < 1e-9


def _mixed(tmp_path):
    # 2 joint + 3 paper-only + 1 live-only, varied per-ct so nothing is degenerate
    paper = _arm(tmp_path, "paper", [
        _rec(T[0], "yes", 10, 300), _rec(T[1], "yes", 10, 100),          # joint
        _rec(T[2], "yes", 10, 250), _rec(T[3], "yes", 10, 150), _rec(T[4], "yes", 10, 200)])  # paper-only
    live = _arm(tmp_path, "live", [
        _rec(T[0], "yes", 5, 60), _rec(T[1], "yes", 5, 40),              # joint
        _rec(T[5], "yes", 5, 30)])                                        # live-only
    return paper, live


def test_decomposition_literals(tmp_path):
    # cluster_bootstrap_ci's point IS the plain mean, so every quantity is deterministic. Pin the
    # ACTUAL values (not the tautology total==exec+sel): paper per-ct = mean(30,10,25,15,20)=20.0;
    # live per-ct = mean(12,8,6)=8.6667; TOTAL=11.3333; EXEC=mean(30-12,10-8)=10.0; SEL=1.3333.
    # A per-lot bug in TOTAL would read 200 vs 43.33 → +156.67, which these literals catch.
    paper, live = _mixed(tmp_path)
    r = lpg.evaluate(paper, live)
    assert abs(r["total_gap"]["paper_all_ci"][0] - 20.0) < 1e-6
    assert abs(r["total_gap"]["live_all_ci"][0] - (26.0 / 3.0)) < 1e-6
    assert abs(r["total_gap"]["point"] - (20.0 - 26.0 / 3.0)) < 1e-6
    assert abs(r["execution"]["ci"][0] - 10.0) < 1e-9
    assert abs(r["selection_residual"]["point"] - (20.0 - 26.0 / 3.0 - 10.0)) < 1e-6


def test_direct_selection_sign_and_magnitude(tmp_path):
    # paper joint edge (40) HIGHER than paper-only (10): direct_selection = paper_all − paper_joint
    # = mean(40,40,10,10)=25 − mean(40,40)=40 = −15 (the opposite-signed split the tool warns about).
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 400), _rec(T[1], "yes", 10, 400),
                                     _rec(T[2], "yes", 10, 100), _rec(T[3], "yes", 10, 100)])
    live = _arm(tmp_path, "live", [_rec(T[0], "yes", 5, 50), _rec(T[1], "yes", 5, 50)])
    r = lpg.evaluate(paper, live)
    assert abs(r["selection_residual"]["direct_selection"] - (-15.0)) < 1e-6


def test_side_disagreement_excluded_from_robustness_exec(tmp_path):
    # 5 same-side joint (+10c/ct each) + 1 opposite-side joint (paper YES +50, live NO +10 → diff +40).
    # Primary EXEC includes the +40 outlier; the ex-side-disagree EXEC must be exactly +10.
    same = [(T[i], 10, 200, 2, 20) for i in range(1, 6)]  # paper +20/ct, live +10/ct → diff +10
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 500)] + [_rec(t, "yes", pq, pp) for t, pq, pp, _, _ in same])
    live = _arm(tmp_path, "live", [_rec(T[0], "no", 2, 20)] + [_rec(t, "yes", lq, lp) for t, _, _, lq, lp in same])
    r = lpg.evaluate(paper, live)
    assert T[0] in r["side_disagreement"]
    assert abs(r["execution"]["ci_ex_side_disagree"][0] - 10.0) < 1e-9   # +40 outlier removed
    assert r["execution"]["ci"][0] > 10.0                                # primary still contaminated by it


def test_set_derivation(tmp_path):
    paper, live = _mixed(tmp_path)
    r = lpg.evaluate(paper, live)
    assert r["sets"] == {"joint": 2, "paper_only": 3, "live_only": 1}


def test_gap_ghost_row_changes_nothing(tmp_path):
    paper, live = _mixed(tmp_path)
    base = lpg.evaluate(paper, live)
    # append a live gap-settled ghost (qty0, side None) — must be excluded, must not crash
    (live / "settlement-log.jsonl").write_text(
        (live / "settlement-log.jsonl").read_text() + "\n" + json.dumps(_rec(T[6], "yes", 5, 999, gap=True)))
    after = lpg.evaluate(paper, live)
    assert after["total_gap"]["point"] == base["total_gap"]["point"]
    assert after["execution"]["ci"][0] == base["execution"]["ci"][0]
    assert after["sets"] == base["sets"]


def test_side_disagreement_flagged(tmp_path):
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 200)] + [_rec(t, "yes", 10, 100) for t in T[1:6]])
    live = _arm(tmp_path, "live", [_rec(T[0], "no", 5, 50)] + [_rec(t, "yes", 5, 40) for t in T[1:6]])
    r = lpg.evaluate(paper, live)
    assert T[0] in r["side_disagreement"]


def test_empty_live_is_graceful(tmp_path):
    paper = _arm(tmp_path, "paper", [_rec(t, "yes", 10, 100) for t in T[:6]])
    live = _arm(tmp_path, "live", [])
    r = lpg.evaluate(paper, live)
    assert r["sets"]["live_only"] == 0 and r["sets"]["joint"] == 0
    assert r["verdict"].startswith("INSUFFICIENT DATA")


def test_classifier_branches():
    clear_big = (6.85, 3.9, 12.1)      # excludes 0, dominates
    clear_small = (4.0, 1.0, 7.0)      # excludes 0, does not dominate 9.0
    noisy = (2.0, -3.0, 7.0)           # spans 0
    assert lpg._classify(clear_big, 3.71, 45).startswith("MOSTLY EXECUTION")
    assert lpg._classify(clear_small, 9.0, 45).startswith("BOTH")
    assert lpg._classify(noisy, 9.0, 45).startswith("SUGGESTS SELECTION")
    # exec is the larger component but its CI spans 0 → must NOT be called "within noise ~0"
    assert lpg._classify(noisy, 1.0, 45).startswith("INCONCLUSIVE")
    assert lpg._classify(clear_big, 3.71, 3).startswith("INSUFFICIENT DATA")   # n < MIN_EVENTS


def test_classifier_dominance_margin():
    # 7.0 vs 6.9 is a near-tie, not domination → BOTH, not MOSTLY EXECUTION (knife-edge guard)
    assert lpg._classify((7.0, 4.0, 10.0), 6.9, 45).startswith("BOTH")


def test_classifier_direction_aware_negative_exec():
    # paper−live = −6c/ct clear of 0 ⇒ live out-executes paper; text must NOT claim paper over-books
    v = lpg._classify((-6.0, -10.0, -2.0), -1.0, 45)
    assert v.startswith("MOSTLY EXECUTION") and "OUT-executes" in v


def test_degenerate_ci_is_not_significant():
    # a zero-width bootstrap CI (all events identical) is not real significance
    assert lpg._excludes_zero((10.0, 10.0, 10.0)) is False
    assert lpg._excludes_zero((10.0, 8.0, 12.0)) is True


# ── entry timing (2026-07-12 fix: POST-anchored entries, same-cycle EXEC subset) ──────────────

def _lifecycle(d, rows):
    (d / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _post(ticker, ts, event="posted", side="yes"):
    return {"event": event, "ts": ts, "ticker": ticker, "side": side,
            "limit_price_cents": 40, "qty": 5}


def test_entry_timing_post_anchored_and_same_cycle_subset(tmp_path):
    # T0 skew +20m (same cycle), T1 skew +360m (drift) → 2 timed pairs, median +190m,
    # same-cycle EXEC uses ONLY T0: paper +30/ct − live +12/ct = +18.
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 300), _rec(T[1], "yes", 10, 100)])
    live = _arm(tmp_path, "live", [_rec(T[0], "yes", 5, 60), _rec(T[1], "yes", 5, 40)])
    _lifecycle(paper, [_post(T[0], "2026-07-08T12:00:00+00:00"), _post(T[1], "2026-07-08T12:00:00+00:00")])
    _lifecycle(live, [_post(T[0], "2026-07-08T12:20:00+00:00", event="posted_live"),
                      _post(T[1], "2026-07-08T18:00:00+00:00", event="posted_live")])
    r = lpg.evaluate(paper, live)
    et = r["entry_timing"]
    assert et["timed_pairs"] == 2
    assert abs(et["median_skew_min"] - 190.0) < 1e-9
    assert et["same_cycle"]["markets"] == 1
    assert abs(et["same_cycle"]["ci"][0] - 18.0) < 1e-9


def test_entry_timing_first_post_wins_and_synced_discovery_excluded(tmp_path):
    # T0: two live posts — the FIRST (12:20) anchors, not the repost. T1: live has NO lifecycle row,
    # only a synced settlement (opened_utc = overnight discovery) → excluded + counted, never timed.
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 300), _rec(T[1], "yes", 10, 100)])
    live_rows = [_rec(T[0], "yes", 5, 60),
                 dict(_rec(T[1], "yes", 5, 40), opened_utc="2026-07-09T04:59:00+00:00",
                      synced_from_kalshi=True)]
    live = _arm(tmp_path, "live", live_rows)
    _lifecycle(paper, [_post(T[0], "2026-07-08T12:00:00+00:00"), _post(T[1], "2026-07-08T12:00:00+00:00")])
    _lifecycle(live, [_post(T[0], "2026-07-08T15:20:00+00:00", event="posted_live"),
                      _post(T[0], "2026-07-08T12:20:00+00:00", event="posted_live")])
    r = lpg.evaluate(paper, live)
    et = r["entry_timing"]
    assert et["timed_pairs"] == 1
    assert abs(et["median_skew_min"] - 20.0) < 1e-9      # first post (12:20), not the 15:20 repost
    assert et["synced_only_excluded"] == 1


def test_entry_timing_paper_synced_discovery_also_excluded(tmp_path):
    # evaluate() is generic: if the PAPER side's only ts is a synced discovery time, the pair must
    # be excluded too (not silently timed off a fill-discovery timestamp).
    paper = _arm(tmp_path, "paper", [dict(_rec(T[0], "yes", 10, 300),
                                          opened_utc="2026-07-09T04:59:00+00:00", synced_from_kalshi=True)])
    live = _arm(tmp_path, "live", [_rec(T[0], "yes", 5, 60)])
    _lifecycle(live, [_post(T[0], "2026-07-08T12:20:00+00:00", event="posted_live")])
    r = lpg.evaluate(paper, live)
    assert r["entry_timing"]["timed_pairs"] == 0
    assert r["entry_timing"]["synced_only_excluded"] == 1


def test_entry_timing_side_disagreement_never_timed(tmp_path):
    # opposite-side joint ticker is a different DECISION — excluded from timing even with post times
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 300)])
    live = _arm(tmp_path, "live", [_rec(T[0], "no", 5, 60)])
    _lifecycle(paper, [_post(T[0], "2026-07-08T12:00:00+00:00")])
    _lifecycle(live, [_post(T[0], "2026-07-08T12:20:00+00:00", event="posted_live", side="no")])
    r = lpg.evaluate(paper, live)
    assert r["entry_timing"]["timed_pairs"] == 0


def test_entry_timing_graceful_without_lifecycle(tmp_path):
    # hermetic dirs with no maker-lifecycle.jsonl and no opened_utc → n/a, nothing crashes,
    # canonical decomposition unchanged
    paper, live = _mixed(tmp_path)
    r = lpg.evaluate(paper, live)
    assert r["entry_timing"]["timed_pairs"] == 0
    assert r["entry_timing"]["same_cycle"]["ci"] == (None, None, None)
    assert abs(r["execution"]["ci"][0] - 10.0) < 1e-9    # canonical EXEC untouched by the feature


# ── _parse_ts fractional-second normalization (2026-07-12 QA fix: 3.9.6 fromisoformat) ──────────

def test_parse_ts_accepts_kalshi_stripped_fractional_seconds():
    # 3.9.6's fromisoformat rejects any fractional-second field that is not 3 or 6 digits, but
    # Kalshi strips trailing zeros. All of these are valid ISO-8601 and must parse to the same
    # instant (fraction padded/truncated to 6 digits), not silently become None.
    base = lpg._parse_ts("2026-06-25T18:19:08+00:00")
    assert base is not None
    for frac, micro in [("6", 600000), ("19", 190000), ("193", 193000),
                        ("19355", 193550), ("193550", 193550), ("1935509", 193550)]:
        d = lpg._parse_ts(f"2026-06-25T18:19:08.{frac}Z")
        assert d is not None, f"fraction .{frac} dropped"
        assert d.tzinfo is not None and d.microsecond == micro
    assert lpg._parse_ts("not-a-timestamp") is None    # real garbage still → None
    assert lpg._parse_ts(None) is None


def test_entry_timing_five_digit_fraction_ticker_is_counted_not_dropped(tmp_path):
    # A same-side joint LIVE ticker resolvable only to a synced settlement whose opened_utc has a
    # 5-digit fraction must be EXCLUDED-and-COUNTED (synced_only), never silently dropped by a
    # _parse_ts→None. Before the fix the row parsed to None and fell out of the count entirely.
    paper = _arm(tmp_path, "paper", [_rec(T[0], "yes", 10, 300)])
    live = _arm(tmp_path, "live", [dict(_rec(T[0], "yes", 5, 60),
                                        opened_utc="2026-07-09T04:59:00.19355Z", synced_from_kalshi=True)])
    _lifecycle(paper, [_post(T[0], "2026-07-08T12:00:00+00:00")])   # paper lifecycle-backed
    # live has NO lifecycle row → only the 5-digit synced opened_utc resolves it
    r = lpg.evaluate(paper, live)
    et = r["entry_timing"]
    assert et["timed_pairs"] == 0
    assert et["synced_only_excluded"] == 1     # counted, not dropped

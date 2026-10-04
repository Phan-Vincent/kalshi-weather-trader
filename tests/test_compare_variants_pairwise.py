#!/usr/bin/env python3
"""compare_variants pairwise-vs-baseline significance (2026-06-28): the rigorous 'is arm X better than
baseline' on MATCHED markets (so shared outcome noise cancels) + within-noise flagging, so a flip
decision can't be fooled by a point estimate the way a raw EV/ct ranking can. Plain python3."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import compare_variants as cv  # noqa: E402


def _arm(tmpd, name, rows):
    d = Path(tmpd, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return d


def test_pairwise_baseline_detects_sig_and_noise():
    tmpd = tempfile.mkdtemp()
    try:
        # arm A wins +$1 on every matched market; B is flat -> A is significantly better.
        a = _arm(tmpd, "a", [{"ticker": f"T{i}", "qty": 1, "pnl_cents": 100} for i in range(8)])
        b = _arm(tmpd, "b", [{"ticker": f"T{i}", "qty": 1, "pnl_cents": 0} for i in range(8)])
        r = cv.pairwise(a, b, by_ticker_only=True)
        pt, lo, hi = r["diff_ci"]
        assert r["matched_markets"] == 8
        assert lo > 0, f"A must read significantly better, CI [{lo},{hi}]"      # every diff +$1
        # A vs itself: every diff is 0 -> within noise (CI does not exclude 0)
        _, lo2, hi2 = cv.pairwise(a, a, by_ticker_only=True)["diff_ci"]
        assert not (lo2 > 0 or hi2 < 0), "identical arms must read as within noise"
        # zero-mean noisy diff: arms differ per market but cancel -> within noise (the trap)
        c = _arm(tmpd, "c", [{"ticker": f"T{i}", "qty": 1, "pnl_cents": (100 if i % 2 else -100)} for i in range(8)])
        _, lo3, hi3 = cv.pairwise(c, b, by_ticker_only=True)["diff_ci"]
        assert lo3 < 0 < hi3, "zero-mean noisy diff must span 0 (within noise)"
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_pairwise_per_contract_is_size_neutral():
    # A and A_big have the SAME per-contract P&L but A_big trades 10x size. vs B the PER-CONTRACT diff
    # must be identical (no size artifact) while the raw $ diff scales 10x — the exact bug the
    # validation caught (live $5 cap vs paper flat size made live look 'worse' on raw $).
    tmpd = tempfile.mkdtemp()
    try:
        a = _arm(tmpd, "a", [{"ticker": f"E{i}-D-B1", "qty": 1, "pnl_cents": 50} for i in range(8)])
        abig = _arm(tmpd, "abig", [{"ticker": f"E{i}-D-B1", "qty": 10, "pnl_cents": 500} for i in range(8)])
        b = _arm(tmpd, "b", [{"ticker": f"E{i}-D-B1", "qty": 1, "pnl_cents": 0} for i in range(8)])
        r1 = cv.pairwise(a, b, by_ticker_only=True)
        r2 = cv.pairwise(abig, b, by_ticker_only=True)
        assert abs(r1["diff_ci"][0] - r2["diff_ci"][0]) < 1e-6, "per-contract diff must be size-neutral"
        assert r1["diff_ci"][0] == 50, r1["diff_ci"][0]                      # 50c/ct − 0
        assert r2["diff_ci_usd"][0] > 5 * r1["diff_ci_usd"][0], "raw $ diff scales with size (the artifact)"
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_cluster_bootstrap_widens_for_correlated():
    # 2 events, 4 perfectly-correlated bins each. The true independent sample is 2 events, so the
    # event-clustered CI must be WIDER than the (wrong) i.i.d. bootstrap that counts 8 bins as independent.
    vals = [30.0] * 4 + [-10.0] * 4
    items = [("ev1", 30.0)] * 4 + [("ev2", -10.0)] * 4
    _, lo_i, hi_i = cv.bootstrap_ci(vals, seed=7)
    _, lo_c, hi_c = cv.cluster_bootstrap_ci(items, seed=7)
    assert (hi_c - lo_c) > (hi_i - lo_i), f"clustered CI must be wider (clustered {hi_c-lo_c} vs iid {hi_i-lo_i})"
    assert lo_c < 0 < hi_c, "2-event {+30,-10} clustered CI must span 0 (can't be significant on 2 events)"


def test_gap_settled_row_excluded_from_per_contract():
    # a gap-settled row (qty=0, entry None, gap_settled) is attribution-less — it must NOT enter the
    # per-contract pairwise diff or the contract-weighted EV; it stays only in total P&L. (QA 2026-06-28)
    tmpd = tempfile.mkdtemp()
    try:
        a = _arm(tmpd, "a", [{"ticker": "E1-D-B1", "qty": 2, "pnl_cents": 20},
                             {"ticker": "E2-D-B1", "qty": 0, "pnl_cents": 340, "entry_cents": None, "gap_settled": True}])
        b = _arm(tmpd, "b", [{"ticker": "E1-D-B1", "qty": 2, "pnl_cents": 0}])
        r = cv.pairwise(a, b, by_ticker_only=True)
        assert r["matched_markets"] == 1, r["matched_markets"]                    # gap market excluded
        assert abs(r["diff_ci"][0] - 10.0) < 1e-6, r["diff_ci"]                    # 20/2 − 0, gap not in diff
        m = cv.arm_metrics(Path(tmpd, "a"))
        assert abs(m["pnl_per_contract_wt"] - 10.0) < 1e-6, m["pnl_per_contract_wt"]  # 20/2, NOT 360/2
        assert abs(m["total_pnl"] - (20 + 340) / 100.0) < 1e-6, m["total_pnl"]        # total INCLUDES gap
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_pairwise_baseline_detects_sig_and_noise()
    print("[PASS] pairwise vs baseline: detects a significant winner, flags within-noise + zero-mean noise")
    test_gap_settled_row_excluded_from_per_contract()
    print("[PASS] gap-settled row excluded from per-contract/wt, kept in total P&L")
    test_pairwise_per_contract_is_size_neutral()
    print("[PASS] pairwise is per-contract size-neutral (raw $ diff would be a size artifact)")
    test_cluster_bootstrap_widens_for_correlated()
    print("[PASS] event-cluster bootstrap widens the CI for correlated bins")
    print("\ncompare_variants pairwise-baseline test passes.")

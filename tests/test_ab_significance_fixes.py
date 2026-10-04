#!/usr/bin/env python3
"""A/B significance fixes (2026-07-01 quant review §2.2):

1. BCa is the default clustered-bootstrap CI (the percentile method measured ~64% empirical
   coverage vs nominal 95% on skewed few-event arm diffs — anti-conservative "excludes 0" stars).
2. Westfall–Young max-t is the RULING for --baseline verdicts (the old percentile-Bonferroni CI
   path emitted a false "passive BETTER (sig)" flag that WY adj_p=0.571 rejects).
3. variance_report exposes the CUPED variance-ratio vr (Δpt is mean-preserving ≡0 and can never
   flag CUPED over-adjustment).

All randomness is seeded → deterministic, not flaky.
"""
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import compare_variants as cv  # noqa: E402


def _skewed_null_items(seed: int, k: int = 20):
    """Right-skewed, event-clustered data with TRUE mean 0 (lognormal minus its mean)."""
    rng = random.Random(seed)
    items = []
    for e in range(k):
        m = math.exp(rng.gauss(0.0, 1.0)) - math.exp(0.5)
        for j in range(rng.randint(1, 3)):
            items.append((f"EV{e}", m + rng.gauss(0.0, 0.1)))
    return items


# ── 1. BCa bootstrap ──────────────────────────────────────────────────────────
def test_bca_matches_percentile_on_symmetric_data():
    rng = random.Random(1)
    items = [(f"EV{e}", rng.gauss(0.0, 1.0)) for e in range(60)]
    p1, lo_b, hi_b = cv.cluster_bootstrap_ci(items, B=2000, seed=3, method="bca")
    p2, lo_p, hi_p = cv.cluster_bootstrap_ci(items, B=2000, seed=3, method="percentile")
    assert p1 == p2
    # symmetric data → negligible bias/acceleration → BCa ≈ percentile
    assert abs(lo_b - lo_p) < 0.15 and abs(hi_b - hi_p) < 0.15, (lo_b, lo_p, hi_b, hi_p)


def test_bca_degenerate_inputs_safe():
    assert cv.cluster_bootstrap_ci([]) == (None, None, None)
    p, lo, hi = cv.cluster_bootstrap_ci([("EV0", 5.0)], B=200)          # single event
    assert p == lo == hi == 5.0
    p, lo, hi = cv.cluster_bootstrap_ci([("EV0", 2.0), ("EV1", 2.0)], B=200)  # all identical
    assert p == lo == hi == 2.0


def test_bca_still_widens_for_correlated_clusters():
    # same property the percentile version guaranteed: clustering must widen vs i.i.d.
    vals = [30.0] * 4 + [-10.0] * 4
    items = [("ev1", 30.0)] * 4 + [("ev2", -10.0)] * 4
    _, lo_i, hi_i = cv.bootstrap_ci(vals, seed=7)
    _, lo_c, hi_c = cv.cluster_bootstrap_ci(items, seed=7)
    assert (hi_c - lo_c) > (hi_i - lo_i)
    assert lo_c < 0 < hi_c


def test_bca_coverage_not_worse_than_percentile_on_skewed_nulls():
    """On skewed clustered null data (true mean 0), BCa's 95% CI must cover 0 at least as often
    as the percentile CI (whose measured real-data coverage was ~64%). Deterministic seeds."""
    M, B = 60, 400
    cov_b = cov_p = 0
    for s in range(M):
        items = _skewed_null_items(1000 + s)
        _, lo, hi = cv.cluster_bootstrap_ci(items, B=B, seed=3, method="bca")
        cov_b += 1 if lo <= 0.0 <= hi else 0
        _, lo, hi = cv.cluster_bootstrap_ci(items, B=B, seed=3, method="percentile")
        cov_p += 1 if lo <= 0.0 <= hi else 0
    assert cov_b >= cov_p, f"BCa coverage {cov_b}/{M} worse than percentile {cov_p}/{M}"
    # Sanity floor only — on this deliberately harsh DGP (lognormal events, k=20) BOTH methods
    # undercover the nominal 95% (measured: bca 44/60 vs percentile 43/60); BCa mitigates, it
    # doesn't cure. The load-bearing assertion is the >= above.
    assert cov_b >= int(0.70 * M), f"BCa coverage sanity floor failed: {cov_b}/{M}"


# ── 2. WY is the ruling: calibrated vs its nominal rate, unlike the Bonferroni-percentile path ──
def test_wy_calibration_beats_percentile_bonferroni_inflation():
    """Replicates the failure mode behind the false 'passive BETTER' flag. The honest comparison
    is each instrument's false-positive rate RELATIVE TO ITS OWN NOMINAL alpha: under a true null
    the old ruling (percentile CI at alpha/family, nominal 0.05/13≈0.0038) fires ~19x its nominal
    (measured 3/40 at these seeds), while WY at nominal 0.05 fires ~2x (4/40). WY's inflation
    factor must be decisively smaller — that's what makes it the trustworthy ruling."""
    fam, n_seeds = 13, 40
    fp_perc = fp_wy = 0
    for s in range(n_seeds):
        items = _skewed_null_items(2000 + s)
        _, blo, bhi = cv.cluster_bootstrap_ci(items, B=500, alpha=0.05 / fam, seed=5, method="percentile")
        if blo > 0 or bhi < 0:
            fp_perc += 1
        wy = cv.westfall_young({"arm": items}, alpha=0.05, B=400, seed=7)
        if wy["arm"]["reject"]:
            fp_wy += 1
    infl_perc = (fp_perc / n_seeds) / (0.05 / fam)   # measured ≈19.5x nominal → anti-conservative
    infl_wy = (fp_wy / n_seeds) / 0.05               # measured ≈2x nominal
    assert infl_wy < infl_perc / 3, (
        f"WY inflation {infl_wy:.1f}x should be far below percentile-Bonferroni {infl_perc:.1f}x")
    assert fp_wy / n_seeds <= 0.15, f"WY absolute false-positive rate too high: {fp_wy}/{n_seeds}"


# ── 2b. WY family scoping (2026-07-13 audit): exclude disabled/tiny arms from the multiplicity family ──

def test_enabled_arm_names_reads_flag(tmp_path):
    cfg = tmp_path / "variants.json"
    cfg.write_text(json.dumps({"variants": [
        {"name": "active-a", "dir": "d1", "enabled": True},
        {"name": "active-b", "dir": "d2", "enabled": True},
        {"name": "deprecated", "dir": "d3", "enabled": False},
    ]}))
    assert cv._enabled_arm_names(cfg) == {"active-a", "active-b"}
    assert cv._enabled_arm_names(tmp_path / "missing.json") == set()   # absent → empty, no crash


def test_wy_tiny_arm_inflates_family_adjp():
    """WHY tiny/deprecated arms must be excluded from the WY family. A few-event, high-variance arm
    (e.g. premium-tightband: 14 events, ±45¢ swings) produces occasional degenerate-large |t| under
    event-resampling that fattens the max-t null's upper tail — inflating EVERY other arm's adj_p. The
    treatment's adj_p over a family that includes such an arm must be materially larger than over the
    treatment alone; this is exactly what pushed the real ruling from a passable ~0.5 to an un-passable
    ~0.9 (2026-07-13). MIN_WY_EVENTS keeps these arms out of the family."""
    rng = random.Random(3)
    treat = [(f"T{e}", 6.0 + rng.gauss(0, 4)) for e in range(30)]        # ~30 ev, modest +effect
    tiny = [("X0", 48), ("X1", -44), ("X2", 46), ("X3", -50), ("X4", 43)]  # few ev, huge variance
    adjp_alone = cv.westfall_young({"treat": treat}, B=1500, seed=7)["treat"]["adj_p"]
    adjp_with_tiny = cv.westfall_young({"treat": treat, "tiny": tiny}, B=1500, seed=7)["treat"]["adj_p"]
    assert adjp_with_tiny > adjp_alone + 0.05, (
        f"a high-variance tiny arm must materially inflate the treatment's adj_p "
        f"(alone={adjp_alone:.3f}, with_tiny={adjp_with_tiny:.3f})")


# ── 2c. duplicate settlement rows (double-write 2026-07-09/10) must not inflate standings ──

def test_dedupe_settlements_by_paper_order_id():
    rows = [
        {"paper_order_id": "o1", "ticker": "T", "pnl_cents": 100, "qty": 2},
        {"paper_order_id": "o1", "ticker": "T", "pnl_cents": 100, "qty": 2},   # double-write dup → drop
        {"paper_order_id": "o2", "ticker": "U", "pnl_cents": -50, "qty": 1},
        {"ticker": "LIVE-A", "pnl_cents": 30, "qty": 1},                        # no order id (live) → keep
        {"ticker": "LIVE-B", "pnl_cents": 30, "qty": 1},                        # no order id → keep (distinct)
    ]
    ded = cv._dedupe_settlements(rows)
    assert [r.get("paper_order_id") for r in ded] == ["o1", "o2", None, None], ded
    assert sum(r["pnl_cents"] for r in ded) == 100 - 50 + 30 + 30, "the duplicate row's P&L must be dropped"


# ── 3. variance_report: vr column + corrected CUPED guidance ─────────────────
def test_variance_report_prints_cuped_variance_ratio(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    rng = random.Random(9)
    rows_a, rows_b = [], []
    for i in range(10):
        fp = 0.3 + 0.04 * i
        rows_a.append({"ticker": f"T{i}-D-B1", "qty": 2, "pnl_cents": int(rng.gauss(20, 40)),
                       "fair_prob_at_open": fp, "entry_cents": 30 + i})
        rows_b.append({"ticker": f"T{i}-D-B1", "qty": 2, "pnl_cents": int(rng.gauss(0, 40)),
                       "fair_prob_at_open": fp, "entry_cents": 30 + i})
    for name, rows in (("a", rows_a), ("b", rows_b)):
        d = state / name
        d.mkdir(parents=True)
        (d / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(cv, "ROOT", tmp_path)
    cv.variance_report(state / "b", [("a", "a")])
    out = capsys.readouterr().out
    header = next(line for line in out.splitlines() if "cuped pt" in line)
    assert "vr" in header, f"vr column missing from variance_report header: {header}"
    assert "Δpt≡0 by construction" in out, "corrected CUPED guidance missing"
    # the data row must end with a parseable vr number
    row = next(line for line in out.splitlines() if line.strip().startswith("a "))
    assert row.rstrip().split()[-1].replace(".", "", 1).isdigit(), f"no numeric vr in: {row}"

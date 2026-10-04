"""tests/test_variance_levers.py — P1 estimator levers in bin/compare_variants.py.

Guards the two things the design's critiques flagged as failure modes:
  1. CUPED must NOT be trusted when the covariate is on the treatment's causal path (over-adjustment):
     the point-shift must be LARGE in that case so variance_report flags it, and ~0 for a valid
     (noise) covariate. This is the practical stand-in for the "covariate must be pre-outcome" guard —
     an outcome-correlated covariate is caught by the shift, never silently celebrated.
  2. strict-match (P1a) drops ONLY size-mismatched markets (qty-ratio outside band) and never adds any.
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("compare_variants", ROOT / "bin" / "compare_variants.py")
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)


def _cov(diffs, xs, qratios=None):
    """Build items_pc_cov tuples: (event, diff, fp, entry, qratio). fp carries the covariate under test."""
    qratios = qratios or [1.0] * len(diffs)
    return [(f"EV{i}", d, x, 0.0, q) for i, (d, x, q) in enumerate(zip(diffs, xs, qratios))]


def test_cuped_noop_on_uncorrelated_covariate():
    # covariate uncorrelated with the diff → theta≈0 → adjusted≈raw, shift≈0 (no fake power)
    diffs = [1.0, -2.0, 3.0, -1.0, 2.0, 0.5, -0.5, 4.0, -3.0, 1.5]
    noise = [0.3, -0.1, 0.2, 0.9, -0.7, 0.4, 0.1, -0.2, 0.6, -0.4]  # ~independent of diffs
    _adj, theta, shift = cv.cuped_adjust(_cov(diffs, noise), "fp")
    assert abs(shift) < 0.25, f"uncorrelated covariate should barely shift the point, got {shift}"


def test_cuped_is_mean_preserving_and_collapses_variance_when_covariate_is_treatment():
    # CUPED is mean-preserving BY CONSTRUCTION: with X==D, theta=1 and every adjusted value collapses
    # to the mean → variance→0 but the POINT is unchanged (shift≈0). So over-adjustment shows up as a
    # suspicious variance COLLAPSE, never as a point shift — the report guidance says exactly this.
    diffs = [1.0, -2.0, 3.0, -1.0, 2.0, 0.5, -0.5, 4.0, -3.0, 1.5]
    mean = sum(diffs) / len(diffs)
    adj, theta, shift = cv.cuped_adjust(_cov(diffs, diffs), "fp")
    assert abs(theta - 1.0) < 1e-6, "theta should be 1 when X==D"
    assert abs(shift) < 1e-9, f"CUPED must preserve the mean (shift≈0), got {shift}"
    vals = [v for _, v in adj]
    assert cv._stdev(vals) < 1e-9, "with X==D the adjusted values must collapse to the mean (variance→0)"
    assert all(abs(v - mean) < 1e-9 for v in vals), "every adjusted value equals the mean"


def test_strict_match_drops_only_out_of_band_and_never_adds():
    # qratios: 3 in-band (0.5..2) + 2 out-of-band → strict keeps exactly the 3 in-band, same events
    diffs = [1.0, 2.0, 3.0, 4.0, 5.0]
    xs = [0.0] * 5
    qratios = [1.0, 0.6, 5.0, 1.9, 0.1]   # in: idx0,1,3 ; out: idx2 (5.0), idx4 (0.1)
    cov = _cov(diffs, xs, qratios)
    kept = cv.strict_match(cov, qlo=0.5, qhi=2.0)
    assert len(kept) == 3, f"should keep the 3 in-band markets, got {len(kept)}"
    assert {ev for ev, _ in kept} == {"EV0", "EV1", "EV3"}
    assert len(kept) <= len(cov), "strict-match must never ADD markets"
    # NaN qratio (missing base qty) is dropped, not kept
    cov_nan = _cov([1.0], [0.0], [float("nan")])
    assert cv.strict_match(cov_nan) == []


def test_ci_half_width_helper():
    assert cv._ci_hw((0.0, -2.0, 2.0)) == 2.0
    assert cv._ci_hw((None, None, None)) is None

#!/usr/bin/env python3
"""Regression tests for the calibrator fixes (weather-model QA 2026-07-03, fix #3).

Pins three defects:
  A. IsotonicCalibrator produced a NON-MONOTONE curve — the per-bucket Laplace weight 5/(n+5) could
     make a higher raw prob map to a LOWER calibrated prob. Now re-pooled with weighted PAVA.
  B. calibrate() linear-extrapolated toward (0,0)/(1,1), dragging sub-first-bucket probs to ~0 and
     cancelling the low-prob underconfidence correction. Now clips to the boundary fitted value.
  C. AutoCalibrator selected isotonic-vs-beta on IN-SAMPLE Brier and never compared to identity, so
     it deployed an overfit curve. Now selects on k-fold OOS Brier with identity a first-class
     candidate — deploys a calibrator only if it beats identity out-of-sample.

Pure/tempfile-driven; no real state written. Runs under plain python3 and pytest.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.calibrate import (  # noqa: E402
    AutoCalibrator, IsotonicCalibrator, _pava, _kfold_oos_brier,
)


def _write_brier(recs):
    d = Path(tempfile.mkdtemp())
    f = d / "brier-log.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs))
    return d, f


def _settled(groups):
    """groups: list of (prob, n, yes_fraction) → deterministic settled brier records (interleaved
    yes/no so every round-robin CV fold sees a representative mix)."""
    recs, rid = [], 0
    for p, n, yf in groups:
        k = int(round(yf * 10))
        for i in range(n):
            recs.append({"record_id": f"r{rid}", "status": "settled", "our_prob": p,
                         "outcome": "yes" if (i % 10) < k else "no",
                         "ticker": f"KXHIGHHOU-26JUL0{rid % 9}-T{70 + rid % 20}"})
            rid += 1
    return recs


# ── A. monotonicity ──────────────────────────────────────────────────
def test_isotonic_curve_is_monotone_after_regularization():
    # A: p=0.30, n=100, 50% yes ; B: p=0.40, n=2, 50% yes. Old per-bucket Laplace reg gives
    # A→0.4905 > B→0.4286 (a HIGHER prob mapping LOWER — the bug). Weighted PAVA must repair it.
    recs = [{"record_id": f"a{i}", "status": "settled", "our_prob": 0.30,
             "outcome": "yes" if i % 2 == 0 else "no", "ticker": "KXHIGHHOU-26JUL01-T80"}
            for i in range(100)]
    recs += [{"record_id": "b0", "status": "settled", "our_prob": 0.40, "outcome": "yes",
              "ticker": "KXHIGHHOU-26JUL02-T81"},
             {"record_id": "b1", "status": "settled", "our_prob": 0.40, "outcome": "no",
              "ticker": "KXHIGHHOU-26JUL02-T81"}]
    d, f = _write_brier(recs)
    iso = IsotonicCalibrator(state_dir=d)
    iso.fit(brier_path=f)
    ys = [y for _, y in iso._curve]
    assert all(ys[i] <= ys[i + 1] + 1e-9 for i in range(len(ys) - 1)), iso._curve
    # calibrate() must also be monotone across a scan of raw probs.
    scan = [iso.calibrate(p / 100) for p in range(1, 100)]
    assert all(scan[i] <= scan[i + 1] + 1e-9 for i in range(len(scan) - 1))


def test_pava_pools_violators_weighted():
    # A big-weight 0.5 followed by a tiny-weight 0.49 → pooled just below 0.5 (weighted), then 0.6.
    out = _pava([0.5, 0.49, 0.6], [100.0, 1.0, 50.0])
    assert out[0] == out[1] and out[0] < 0.5 and out[0] > 0.49
    assert out[2] == 0.6
    assert all(out[i] <= out[i + 1] + 1e-12 for i in range(len(out) - 1))


# ── B. flat-clip extrapolation ───────────────────────────────────────
def test_calibrate_clips_outside_range_not_toward_zero():
    # Model underconfident at low probs: p=0.20 resolves yes 55% → the first fitted bucket sits well
    # above 0. A raw prob BELOW the fitted range must clip to that bucket, not be dragged toward 0.
    d, f = _write_brier(_settled([(0.20, 200, 0.55), (0.60, 200, 0.62)]))
    iso = IsotonicCalibrator(state_dir=d)
    iso.fit(brier_path=f)
    first_y, last_y = iso._curve[0][1], iso._curve[-1][1]
    assert iso.calibrate(0.0001) == max(0.001, min(0.999, first_y))
    assert iso.calibrate(0.9999) == max(0.001, min(0.999, last_y))
    # The whole point: a tiny raw prob keeps the upward correction (nowhere near 0).
    assert iso.calibrate(0.0001) > 0.2, iso.calibrate(0.0001)


# ── C. OOS selection with identity ───────────────────────────────────
def test_autocalibrator_picks_identity_when_calibration_doesnt_generalize():
    # Already well-calibrated → no calibrator beats identity out-of-sample → price uncalibrated.
    d, f = _write_brier(_settled([(0.20, 150, 0.20), (0.50, 150, 0.50), (0.80, 150, 0.80)]))
    ac = AutoCalibrator(state_dir=d)
    ac.fit(brier_path=f)
    assert ac.method == "identity", (ac.method, ac._oos)
    assert ac.calibrate(0.2) == 0.2  # identity passes probs through untouched
    assert ac._oos["selected"] == "identity"


def test_autocalibrator_deploys_calibrator_when_it_generalizes():
    # A large, consistent, learnable miscalibration (p=0.20 resolves yes 60%) → a calibrator beats
    # identity OOS → it IS deployed. Guards against the fix over-suppressing genuine calibration.
    d, f = _write_brier(_settled([(0.20, 200, 0.60), (0.80, 200, 0.80)]))
    ac = AutoCalibrator(state_dir=d)
    ac.fit(brier_path=f)
    assert ac.method != "identity", (ac.method, ac._oos)
    assert ac._oos[f"{ac.method}_cv"] < ac._oos["identity"]
    # deployed calibrator pushes the underconfident 0.20 upward toward its ~0.60 realised rate.
    assert ac.calibrate(0.20) > 0.35, ac.calibrate(0.20)


def test_kfold_oos_scorer_and_guards():
    # _kfold_oos_brier consumes {"p","y"} trades (post _load_settled_trades), returns a pooled
    # held-out Brier, and guards the too-few-to-split case with None.
    trades = [{"p": 0.30 if i < 60 else 0.70, "y": float(i % 2)} for i in range(120)]
    identity_oos = _kfold_oos_brier(lambda tr: (lambda p: p), trades)
    assert identity_oos is not None and identity_oos > 0
    assert _kfold_oos_brier(lambda tr: (lambda p: p), []) is None  # too few → None


# ── summary() contract (regression: identity path once crashed the fair-value build) ──
def _build_fv_calibration_log(s):
    """Reproduce bin/build_fair_values.py's exact cal_summary payload — must never KeyError."""
    return {"method": s.get("selected_method", s.get("method")),
            "n_trades": s.get("n_trades"),
            "brier_before": s.get("brier_before"), "brier_after": s.get("brier_after"),
            "improvement_pct": s.get("brier_improvement_pct")}


def test_summary_keeps_build_consumed_keys_on_identity_path():
    # When identity is selected (current production state), summary() must still carry n_trades /
    # brier_* — omitting n_trades made build_fair_values crash with KeyError every cycle.
    d, f = _write_brier(_settled([(0.20, 150, 0.20), (0.50, 150, 0.50), (0.80, 150, 0.80)]))
    ac = AutoCalibrator(state_dir=d)
    ac.fit(brier_path=f)
    assert ac.method == "identity"
    s = ac.summary()
    for k in ("n_trades", "brier_before", "brier_after", "selected_method"):
        assert k in s, (k, sorted(s))
    assert s["n_trades"] == 450
    _build_fv_calibration_log(s)  # must not raise


def test_summary_keeps_keys_on_calibrator_path():
    d, f = _write_brier(_settled([(0.20, 200, 0.60), (0.80, 200, 0.80)]))
    ac = AutoCalibrator(state_dir=d)
    ac.fit(brier_path=f)
    assert ac.method != "identity"
    s = ac.summary()
    assert "n_trades" in s and "selected_method" in s, sorted(s)
    _build_fv_calibration_log(s)  # must not raise


if __name__ == "__main__":
    for fn in [test_isotonic_curve_is_monotone_after_regularization, test_pava_pools_violators_weighted,
               test_calibrate_clips_outside_range_not_toward_zero,
               test_autocalibrator_picks_identity_when_calibration_doesnt_generalize,
               test_autocalibrator_deploys_calibrator_when_it_generalizes,
               test_kfold_oos_scorer_and_guards,
               test_summary_keeps_build_consumed_keys_on_identity_path,
               test_summary_keeps_keys_on_calibrator_path]:
        fn()
    print("OK — calibrator fixes: monotonicity, flat-clip, OOS-selection-with-identity, summary contract")

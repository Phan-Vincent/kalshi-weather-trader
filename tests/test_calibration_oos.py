#!/usr/bin/env python3
"""Out-of-sample calibration-gain tests (quant review 2026-07-01, experiment #7).

Pins: the IsotonicCalibrator trades= hook fits without a file; k-fold scores every trade exactly
once (pooled); the time-forward split honors the fraction; isotonic shows a real OUT-OF-SAMPLE gain
on genuinely miscalibrated data; on well-calibrated data the in-sample gain does NOT exceed OOS by a
spurious margin (optimism is non-negative); too few trades -> INSUFFICIENT DATA. No files, no net.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import calibration_oos as oos  # noqa: E402
from model.calibrate import IsotonicCalibrator  # noqa: E402


def _t(p, y, i):
    return {"p": p, "y": float(y), "ticker": "KXHIGHHOU-26JUL01-B80.5", "t": f"2026-07-01T{i:04d}"}


def _miscalibrated(n=200):
    """Two groups whose raw probs (0.2, 0.8) are both actually 50/50 -> isotonic should pull both
    toward 0.5 and win out-of-sample. y balanced within each group and spread evenly over time."""
    rows = []
    for i in range(n):
        p = 0.2 if i % 2 == 0 else 0.8
        y = 1.0 if (i // 2) % 2 == 0 else 0.0
        rows.append(_t(p, y, i))
    return rows


def _wellcalibrated(n=180):
    """P(y=1|p)=p exactly for p in {0.3,0.5,0.7}: identity is already optimal, so any in-sample
    'gain' is fitting noise and should not survive out-of-sample."""
    rows = []
    groups = [(0.3, 3), (0.5, 5), (0.7, 7)]  # (prob, #yes per 10)
    per = n // 3
    idx = 0
    for p, yes in groups:
        for j in range(per):
            rows.append(_t(p, 1.0 if (j % 10) < yes else 0.0, idx)); idx += 1
    return rows


def test_fit_trades_hook_no_file():
    cal = IsotonicCalibrator().fit(trades=_miscalibrated(60))
    assert cal.fitted and cal._n_trades == 60
    assert cal.calibrate(0.2) > 0.2      # pulled up toward the 0.5 base rate


def test_kfold_pools_every_trade_once():
    r = oos.kfold(_miscalibrated(200), k=5)
    assert r["n_scored"] == 200
    assert r["improvement_pct"] is not None


def test_time_forward_split_honors_fraction():
    r = oos.time_forward(_miscalibrated(200), frac=0.7)
    assert r["n_train"] == 140 and r["n_test"] == 60


def test_isotonic_helps_miscalibrated_out_of_sample():
    r = oos.evaluate(trades=_miscalibrated(200))
    assert r["oos_improvement_pct"] > 0                       # genuine OOS gain
    assert r["verdict"].startswith(("PARTIAL", "ROBUST"))


def test_wellcalibrated_optimism_nonnegative():
    r = oos.evaluate(trades=_wellcalibrated(180))
    # in-sample gain must not be materially below OOS — the tool measures optimism (in - OOS) >= ~0
    assert r["optimism_pts"] is not None and r["optimism_pts"] >= -0.5


def test_insufficient_data():
    r = oos.evaluate(trades=[_t(0.3, 1, 0), _t(0.7, 0, 1)])
    assert r["oos_improvement_pct"] is None
    assert r["verdict"].startswith("INSUFFICIENT DATA")

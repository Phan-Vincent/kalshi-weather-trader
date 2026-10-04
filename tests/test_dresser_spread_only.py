#!/usr/bin/env python3
"""
tests/test_dresser_spread_only.py — Regression for the 2026-07-06 audit finding that
the ensemble dresser baked the RAW per-city bias into every synthetic member
(synthetic = base_mean + bias + noise). That shifted the weighted mean by bias·D/(R+D)
AND contaminated model_temp_forecast, so the learned bias was applied ~1.15-1.5x (once
in the dressed members, again at the KDE recenter kde_center = w_mean + city_bias_f) and
the error-model EWMA learned against its own attenuated output.

Dressing must add SPREAD ONLY, centered on the raw ensemble mean. The single bias
correction lives at the KDE recenter.

Run: python3 -m pytest tests/test_dresser_spread_only.py
"""
import sys
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model.ensemble_dresser import EnsembleDresser


def _dresser(bias, std, n_dress=2000):
    d = EnsembleDresser(n_dress=n_dress)
    d._data = {"HOU": {"bias_f": bias, "error_std_f": std, "n_effective": 50}}
    d._loaded = True
    return d


def test_dressed_members_center_on_raw_mean_not_bias():
    real = [90.0, 90.0, 90.0]          # base_mean = 90
    d = _dresser(bias=5.0, std=2.0)    # a big +5F bias would be obvious if baked in
    dressed = d.dress("HOU", real, seed=123)
    assert dressed, "expected synthetic members"
    m = statistics.mean(dressed)
    # With bias baked in (old bug) the mean would be ~95. Spread-only → ~90.
    assert abs(m - 90.0) < 0.5, f"dressed mean {m:.2f} should center on the raw mean 90, not 90+bias"
    assert abs(statistics.pstdev(dressed) - 2.0) < 0.4, "spread should track error_std_f"


def test_zero_bias_unaffected():
    real = [80.0, 82.0, 84.0]          # base_mean = 82
    d = _dresser(bias=0.0, std=1.5)
    dressed = d.dress("HOU", real, seed=7)
    assert abs(statistics.mean(dressed) - 82.0) < 0.5


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

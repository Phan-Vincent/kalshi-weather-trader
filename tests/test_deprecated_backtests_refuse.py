#!/usr/bin/env python3
"""Quarantined backtests must refuse to run and redirect to the forward tools (quant review #10).

Guards against a future edit accidentally re-enabling a look-ahead / non-live backtest. pytest only
collects tests/**/test_*.py, so this lives here (bin/ is never auto-collected — cf. test_verify_bin).
Hermetic: the guard fires before any import, so nothing is touched (no network, no state, no env).
"""
import subprocess
import sys
from pathlib import Path

import pytest

_DEP = Path(__file__).resolve().parent.parent / "bin" / "deprecated"
_SCRIPTS = ["backtest_v2.py", "backtest_v3.py", "calibrate_backtest.py", "backtest_real.py"]


@pytest.mark.parametrize("name", _SCRIPTS)
def test_quarantined_backtest_refuses_to_run(name):
    path = _DEP / name
    assert path.exists(), f"quarantined script missing: {path}"
    proc = subprocess.run(
        [sys.executable, str(path), "--shrink-sweep", "--days", "5"],   # args a real run would use
        capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"{name} exited 0 — guard did not fire"
    assert "DEPRECATED" in out and "quarantined" in out.lower(), f"{name} missing guard message: {out!r}"
    assert any(t in out for t in ("shadow_score.py", "leakfree_skill.py", "pnl_replay.py")), \
        f"{name} lacks a forward-tool redirect: {out!r}"
    # Regression: backtest_v2 / backtest_real RAN before the guard — they must not execute past it now.
    assert "climatology_loaded" not in out, f"{name} still executed past the guard: {out!r}"

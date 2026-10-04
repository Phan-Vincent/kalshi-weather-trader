#!/usr/bin/env python3
"""Regression guard for the 2026-06-24 live-arm outage.

bin/run_variants.py must import the `trader` package in the SAME context the cron runs it:
`python3 bin/run_variants.py` from the project root with NO PYTHONPATH. A missing sys.path
entry made `from trader.halt import is_halted_safe` raise "No module named 'trader'", and the
fail-safe halt check then skipped the live arm EVERY cycle (0 live orders for a day). Runs
under plain python3."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_run_variants_imports_trader_in_cron_context():
    # Replicate the cron exactly: cwd = project root, NO PYTHONPATH.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    r = subprocess.run(
        [sys.executable, "bin/run_variants.py", "--list"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    out = (r.stdout or "") + (r.stderr or "")
    assert "No module named 'trader'" not in out, f"trader not importable from run_variants:\n{out[:600]}"
    assert "halt check unavailable" not in out, f"halt-check import failed:\n{out[:600]}"
    assert r.returncode == 0, f"run_variants --list exited {r.returncode}:\n{out[:600]}"


if __name__ == "__main__":
    test_run_variants_imports_trader_in_cron_context()
    print("[PASS] run_variants.py imports `trader` in the cron context (root cwd, no PYTHONPATH)")
    print("\nrun_variants import regression test passed.")

#!/usr/bin/env python3
"""Regression guard for the 2026-07-06 live-first split.

run-cycle.sh now places the real order in its own early call — `run_variants.py --only-live`,
right after build #1 — and runs the paper arms afterward with `--exclude-live`. This decouples
the live order from the two GEFS/forecast paper builds and the 7 paper arms that used to run
before it, so a slow/torn-down/stalled paper phase can no longer starve the live order (the
"38/38 built, killed before trading phase" SIGKILL mode). These tests pin the two invariants
that keep that safe:

  1. --only-live selects ONLY live arms (and still self-gates on LIVE=1 + not halted).
  2. --exclude-live NEVER includes a live arm — so the paper pass can't place a second real order.
  3. The two flags are mutually exclusive.

Runs the runner exactly as the cron does: cwd = project root, NO PYTHONPATH, plain python3.
--dry-run means no book fetch and no real orders are placed by this test.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(args, extra_env=None):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # Never let an ambient halt or a real LIVE flag from the caller's shell perturb the test.
    env.pop("LIVE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "bin/run_variants.py", *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def _header_arms(out: str):
    """Parse the 'N arm(s): a, b, c' line the runner prints before executing."""
    for line in out.splitlines():
        if "run_variants:" in line and "arm(s):" in line:
            names = line.split("arm(s):", 1)[1].rstrip("= ").strip()
            return {n.strip() for n in names.split(",") if n.strip()}
    return set()


def test_only_live_selects_only_live_arm():
    # LIVE=1 so the live gate lets the live arm through; --dry-run so nothing actually trades.
    r = _run(["--only-live", "--dry-run"], {"LIVE": "1", "KALSHI_RESEARCH_MODE_ALLOW_RED": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"--only-live exited {r.returncode}:\n{out[:800]}"
    arms = _header_arms(out)
    assert arms, f"no arms header found:\n{out[:800]}"
    assert arms == {"premium-live"}, f"--only-live must run ONLY the live arm, got {arms}"


def test_exclude_live_never_runs_a_live_arm():
    # Even with LIVE=1, --exclude-live must drop every live arm (no second real order).
    r = _run(["--exclude-live", "--dry-run"], {"LIVE": "1", "KALSHI_RESEARCH_MODE_ALLOW_RED": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"--exclude-live exited {r.returncode}:\n{out[:800]}"
    arms = _header_arms(out)
    assert arms, f"no arms header found:\n{out[:800]}"
    assert "premium-live" not in arms, f"--exclude-live leaked a live arm: {arms}"
    # sanity: it DID run paper arms
    assert len(arms) >= 1, f"--exclude-live ran no paper arms:\n{out[:800]}"


def test_only_live_is_a_noop_without_LIVE():
    # Paper :00 cron path: LIVE unset → the live gate removes the live arm → nothing runs.
    r = _run(["--only-live", "--dry-run"])
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"--only-live (no LIVE) exited {r.returncode}:\n{out[:800]}"
    assert "No arms to run." in out, f"--only-live without LIVE must be a no-op:\n{out[:800]}"


def test_flags_are_mutually_exclusive():
    r = _run(["--only-live", "--exclude-live", "--dry-run"])
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode != 0, "combining --only-live and --exclude-live must error"
    assert "not allowed with argument" in out, f"expected argparse mutual-exclusion error:\n{out[:800]}"


if __name__ == "__main__":
    test_only_live_selects_only_live_arm()
    test_exclude_live_never_runs_a_live_arm()
    test_only_live_is_a_noop_without_LIVE()
    test_flags_are_mutually_exclusive()
    print("[PASS] live-first split: --only-live / --exclude-live invariants hold")

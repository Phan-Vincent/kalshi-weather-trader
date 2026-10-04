#!/usr/bin/env python3
"""bin/check_fill_milestone.py — alert ONCE when the live arm crosses N real fills.

`bin/reconcile_fills.py` needs >=30 live fills before its calibration is
trustworthy. This fires a one-time operator alert at that threshold so the moment
the live scale-up decision is unblocked is visible. Uses a persistent sentinel
(not the time-windowed alert de-dupe) so it never repeats. Best-effort; never
raises, so the cycle can't break on it.

  python3 bin/check_fill_milestone.py [--live-dir state/live-premium]
Env: KALSHI_WEATHER_FILL_MILESTONE (default 30).
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling reconcile_fills


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert once at >=N live fills")
    ap.add_argument("--live-dir", default="state/live-premium")
    args = ap.parse_args()

    # Two milestones: 30 = fill model reconcilable; 100 = scale-decision sample.
    thresholds = sorted({int(x) for x in os.environ.get("KALSHI_WEATHER_FILL_MILESTONE", "30,100").split(",")
                         if x.strip().isdigit()})
    if not thresholds:
        thresholds = [30, 100]
    live_dir = Path(args.live_dir)
    if not live_dir.is_absolute():
        live_dir = ROOT / live_dir

    try:
        from reconcile_fills import live_fill_stats
        stats = live_fill_stats(live_dir)
    except Exception as e:
        print(f"[fill-milestone] could not read live fills: {e}", file=sys.stderr)
        return 0

    filled = int(stats.get("filled") or 0)
    posted = int(stats.get("posted") or 0)
    print(f"[fill-milestone] live fills={filled} (posted={posted}); thresholds={thresholds}")

    for threshold in thresholds:
        sentinel = live_dir / f".fill-milestone-{threshold}.done"
        if filled < threshold or sentinel.exists():
            continue
        if threshold >= 100:
            msg = (f"🎯 {filled} live fills (>={threshold}) — SCALE-DECISION sample reached. Run "
                   f"`python3 bin/reconcile_fills.py` + `compare_variants.py`; if live P&L is positive, "
                   f"enable KALSHI_WEATHER_SIZE_RAMP=1 to begin the gated size ramp.")
        else:
            msg = (f"🎯 {filled} live fills (>={threshold}) — fill model reconcilable. Run "
                   f"`python3 bin/reconcile_fills.py` to calibrate it. NOTE: hold size until >=100 fills "
                   f"(30 proves the fill model, not the edge).")
        try:
            from trader.notify import alert
            alert(msg, key=f"fill_milestone_{threshold}")
        except Exception:
            pass
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(f"{filled} fills (threshold {threshold})\n")
        except Exception:
            pass
        print(f"[fill-milestone] 🎯 threshold {threshold} reached — alerted + sentinel written")

    return 0


if __name__ == "__main__":
    sys.exit(main())

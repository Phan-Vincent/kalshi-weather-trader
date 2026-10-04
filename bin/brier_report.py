#!/usr/bin/env python3
"""
bin/brier_report.py — Brier score + calibration report for Kalshi weather paper bot.

Usage:
  python3 bin/brier_report.py
  python3 bin/brier_report.py --json
  python3 bin/brier_report.py --min-trades 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.brier import BrierLogger


def main() -> int:
    parser = argparse.ArgumentParser(description="Brier score + calibration report")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--min-trades", type=int, default=0, help="Minimum settled trades to include")
    args = parser.parse_args()

    brier = BrierLogger()
    summary = brier.summary(min_trades=args.min_trades)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    # Human-readable table
    n_trades = summary["n_trades"]
    n_open = summary["n_open"]
    our_brier = summary["our_brier_mean"]
    market_brier = summary["market_brier_mean"]
    bss = summary["brier_skill_score"]
    gate = summary["gate_status"]
    top_city = summary.get("top_city")
    z_score = summary.get("z_score")
    bss_se = summary.get("bss_se")

    print("=" * 60)
    print("  KALSHI WEATHER PAPER BOT — BRIER + CALIBRATION REPORT")
    print("=" * 60)
    print(f"  Settled trades : {n_trades}")
    print(f"  Open (pending) : {n_open}")
    print(f"  Gate status    : {gate}")
    if our_brier is not None:
        print(f"  Our Brier      : {our_brier:.6f}")
        print(f"  Market Brier   : {market_brier:.6f}")
        print(f"  BSS            : {bss:+.6f}  (positive = we beat market)")
        if bss_se is not None:
            print(f"  BSS SE         : {bss_se:.6f}")
        if z_score is not None:
            sig_marker = "*" if abs(z_score) >= 1.96 else ("~" if abs(z_score) >= 1.28 else "")
            print(f"  z-score        : {z_score:+.3f} {sig_marker}")
        print(f"  Directional Acc: {summary.get('directional_accuracy', 'N/A')}")
    else:
        print("  Our Brier      : N/A (no settled trades)")
    if top_city:
        print(f"  Top city       : {top_city} ({summary['city_counts'].get(top_city, 0)} trades)")
    print("-" * 60)

    print("\n  CALIBRATION BUCKETS (our_prob_for_outcome vs actual outcome)")
    print(f"  {'Range':<12s} {'N':>5s} {'Mean Our':>10s} {'Mean Out':>10s} {'Diff':>10s}")
    print("  " + "-" * 52)
    worst = summary.get("worst_bucket")
    for b in summary["calibration_buckets"]:
        n = b["n"]
        if n == 0:
            marker = "  "
        elif worst and b["range"] == worst["range"]:
            marker = "* "
        else:
            marker = "  "
        mp = f"{b['mean_our_prob']:.4f}" if b["mean_our_prob"] is not None else "-"
        mo = f"{b['mean_outcome']:.4f}" if b["mean_outcome"] is not None else "-"
        diff = f"{b['diff']:+.4f}" if b["diff"] is not None else "-"
        print(f"  {marker}{b['range']:<10s} {n:>5d} {mp:>10s} {mo:>10s} {diff:>10s}")

    print("\n  * = worst calibrated bucket (largest |diff|)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

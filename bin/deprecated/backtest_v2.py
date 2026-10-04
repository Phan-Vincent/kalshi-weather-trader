#!/usr/bin/env python3
# ── DEPRECATED / QUARANTINED 2026-07-02 (quant review #10) — DO NOT RUN. Output is NOT validation. ──
import sys as _sys
_sys.exit(
    "DEPRECATED backtest — quarantined, refuses to run. This script is either look-ahead "
    "(backtest_v3/calibrate_backtest synthesize forecasts centered on the realized truth) or scores a "
    "code path LIVE does not execute (backtest_real scores USE_FORECAST=1; live runs USE_FORECAST=0 "
    "climatology). Use the leak-free FORWARD tools instead: bin/shadow_score.py, bin/leakfree_skill.py, "
    "bin/persistence_leak_probe.py, bin/pnl_replay.py, bin/calibration_oos.py, bin/model_brier.py. "
    "See bin/deprecated/README.md."
)
"""Phase 5: Backtest v2 model against the 153 settled trades.

Replays each settled trade through the new Bayesian fair-value model
and compares calibration, Brier score, and directional accuracy against
the old Gaussian model.

Usage:
    python3 bin/backtest_v2.py [--verbose]

Output:
    - Calibration table (decile buckets)
    - Brier skill score vs market
    - Directional accuracy
    - Holdout set (last 30 trades) results
    - Summary verdict
"""

# from __future__ import annotations  # neutralized: quarantined file, the top guard exits first

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTOMATIONS = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
if _AUTOMATIONS not in sys.path:
    sys.path.insert(0, _AUTOMATIONS)

from kalshi_weather.model.fair_value import estimate_market_prob
from kalshi_weather.model.prior import Climatology
from kalshi_weather.model.error_tracker import ErrorTracker
from kalshi_weather.data.weather_data import STATIONS, parse_market_ticker

ROOT = Path(_AUTOMATIONS) / "kalshi-weather"
BRIER_LOG = ROOT / "state" / "brier-log.jsonl"
CLIMATOLOGY = ROOT / "state" / "climatology.json"
ERROR_MODEL = ROOT / "state" / "error-model.json"

VERBOSE = "--verbose" in sys.argv


def _load_settled_trades() -> list[dict]:
    """Load all settled trades from brier log.

    Returns list of dicts with: ticker, side, our_prob, market_prob,
    outcome (True=YES won), brier, market_brier, timestamp, city_code, etc.
    """
    trades = []
    opens: dict[str, dict] = {}

    if not BRIER_LOG.exists():
        print(f"[error] Brier log not found: {BRIER_LOG}", file=sys.stderr)
        return trades

    with open(BRIER_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rid = rec.get("record_id")
            if not rid:
                continue

            if rec.get("status") == "open":
                opens[rid] = rec
            elif rec.get("status") == "settled":
                # Find matching open record
                o = opens.get(rid, {})
                trades.append({**o, **rec})

    return trades


def _extract_city_code(ticker: str) -> Optional[str]:
    """Extract city code from ticker."""
    import re
    for tag in ("HIGHT", "LOWT"):
        m = re.match(rf"^KX{tag}([A-Z]+)-", ticker)
        if m:
            code = m.group(1)
            return "NYC" if code == "NYCH" else code
    return None


def _extract_date_iso(ticker: str) -> Optional[str]:
    """Extract YYYY-MM-DD from ticker like KXHIGHTHOU-26MAY29-B71.5."""
    import re
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
    if not m:
        return None
    yr = "20" + m.group(1)
    month_map = {
        "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
        "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
        "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
    }
    mon = month_map.get(m.group(2), "01")
    day = m.group(3)
    return f"{yr}-{mon}-{day}"


def _infer_market_type(ticker: str) -> str:
    if "HIGHT" in ticker or "HIGH" in ticker:
        return "daily_high"
    if "LOWT" in ticker or "LOW" in ticker:
        return "daily_low"
    return "unknown"


def _infer_bin_kind(ticker: str) -> str:
    """Infer bin kind from ticker. B = between, T = threshold (above/below)."""
    parts = ticker.split("-")
    if len(parts) >= 3:
        val = parts[2]
        if val.startswith("B"):
            return "between"
        if val.startswith("T"):
            return "below"  # assume below (common case for T-coded)
    return "above"


def _infer_threshold(ticker: str) -> Optional[float]:
    parts = ticker.split("-")
    if len(parts) >= 3:
        val = parts[2]
        try:
            return float(val[1:])
        except (ValueError, IndexError):
            pass
    return None


def _build_market_obj(ticker: str) -> dict:
    """Build a minimal market dict from ticker for estimate_market_prob."""
    mtype = _infer_market_type(ticker)
    city = _extract_city_code(ticker) or "UNKNOWN"
    date_iso = _extract_date_iso(ticker) or "2000-01-01"
    threshold = _infer_threshold(ticker) or 70.0
    bin_kind = _infer_bin_kind(ticker)

    bin_low, bin_high = -float("inf"), float("inf")
    if bin_kind == "between":
        bin_low = threshold - 1.0
        bin_high = threshold + 1.0
    elif bin_kind == "below":
        bin_low = -float("inf")
        bin_high = threshold
    else:
        bin_low = threshold
        bin_high = float("inf")

    # Use today as close_time (backtest: assume close was soon after trade)
    close_time = datetime.now(timezone.utc).isoformat()

    return {
        "ticker": ticker,
        "market_type": mtype,
        "city_code": city,
        "date_iso": date_iso,
        "hour_utc": None,
        "threshold_f": threshold,
        "bin_kind": bin_kind,
        "bin_low": bin_low,
        "bin_high": bin_high,
        "_raw_market": {"close_time": close_time},
    }


def _compute_brier(prob: float, outcome: bool) -> float:
    """Brier score for a single prediction: (prob - outcome)²"""
    return (prob - (1.0 if outcome else 0.0)) ** 2


def backtest() -> None:
    trades = _load_settled_trades()
    print(f"Loaded {len(trades)} settled trades")
    if not trades:
        return

    # Load climatology
    climo = Climatology()
    if CLIMATOLOGY.exists():
        climo.load(str(CLIMATOLOGY))
    print(f"Climatology loaded: {climo.is_loaded()}")

    # Load error model
    tracker = ErrorTracker()
    if ERROR_MODEL.exists():
        tracker.load()
    print(f"Error model loaded: {tracker.n_cities()} cities")

    # Separate into training (first 123) and holdout (last 30)
    # Sort by timestamp
    trades.sort(key=lambda t: t.get("timestamp_utc", ""))
    train = trades[:-30]
    holdout = trades[-30:]

    print(f"\nTraining set: {len(train)} trades")
    print(f"Holdout set:  {len(holdout)} trades")

    # ═══════════════════════════════════════════════════════════════
    # Backtest v2 model on training set
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 65)
    print("  v2 Model Backtest (Training Set)")
    print("=" * 65)

    results = _backtest_on_set(train, climo, tracker, "v2")

    # Print v2 results
    print(f"\n  ├─ Trades:      {results['n']}")
    print(f"  ├─ Brier mean:  {results['brier_mean']:.4f}")
    print(f"  ├─ Market Brier:{results['mkt_brier_mean']:.4f}")
    print(f"  ├─ BSS:         {results['brier_skill']:+.4f}")
    print(f"  ├─ Dir accuracy:{results['directional_accuracy']:.1%}")
    print(f"  ├─ Calibr slope:{results['calibration_slope']:.3f}")
    print(f"  └─ Old model:   Brier={results['old_brier_mean']:.4f} "
          f"BSS={results['old_bss']:+.4f}")

    # Print calibration table
    print("\n  Calibration buckets:")
    for b in results["buckets"]:
        n = b["n"]
        if n > 0:
            our = b["mean_our_prob"]
            actual = b["mean_outcome"]
            diff = b["diff"]
            bar = "█" * int(actual * 20) + "░" * (20 - int(actual * 20))
            print(f"    {b['range']:>8s}: n={n:3d}  our={our:.2f}  actual={actual:.2f}  "
                  f"diff={diff:+.3f}  {bar}")

    # ═══════════════════════════════════════════════════════════════
    # Backtest on holdout
    # ═══════════════════════════════════════════════════════════════
    if holdout:
        print("\n" + "=" * 65)
        print("  v2 Model Backtest (Holdout Set — 30 trades)")
        print("=" * 65)

        hold_results = _backtest_on_set(holdout, climo, tracker, "holdout")
        print(f"\n  ├─ Trades:      {hold_results['n']}")
        print(f"  ├─ Brier mean:  {hold_results['brier_mean']:.4f}")
        print(f"  ├─ Market Brier:{hold_results['mkt_brier_mean']:.4f}")
        print(f"  ├─ BSS:         {hold_results['brier_skill']:+.4f}")
        print(f"  ├─ Dir accuracy:{hold_results['directional_accuracy']:.1%}")
        print(f"  └─ Calibr slope:{hold_results['calibration_slope']:.3f}")

        if hold_results["brier_skill"] is not None and hold_results["brier_skill"] > 0:
            print("\n  ✅ HOLDOUT PASSES: BSS > 0")
        else:
            print(f"\n  ❌ HOLDOUT FAILS: BSS = {hold_results['brier_skill']:+.4f}")

    # ═══════════════════════════════════════════════════════════════
    # Verdict
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 65)
    print("  VERDICT")
    print("=" * 65)

    bss = results.get("brier_skill")
    cal = results.get("calibration_slope")
    da = results.get("directional_accuracy")

    passes = 0
    total = 3

    print(f"  1. Brier Skill Score ≥ 0: {bss:+.4f}  {'✅ PASS' if bss is not None and bss >= 0 else '❌ FAIL'}")
    if bss is not None and bss >= 0:
        passes += 1

    print(f"  2. Calibration slope 0.8-1.2: {cal:.3f}  {'✅ PASS' if 0.8 <= cal <= 1.2 else '❌ FAIL'}")
    if 0.8 <= cal <= 1.2:
        passes += 1

    print(f"  3. Directional accuracy > 60%: {da:.1%}  {'✅ PASS' if da > 0.6 else '❌ FAIL'}")
    if da > 0.6:
        passes += 1

    print(f"\n  Result: {passes}/{total} gates passed")
    if passes < total:
        print("  → Model needs improvement before deployment")
    else:
        print("  → ALL GATES PASSED — model ready for paper trading")


def _backtest_on_set(
    trades: list[dict],
    climo: Climatology,
    tracker: ErrorTracker,
    label: str,
) -> dict:
    """Run v2 model on a list of settled trades and compute metrics."""
    results: list[dict] = []

    for t in trades:
        ticker = t.get("ticker", "")
        if not ticker:
            continue

        # Build market object
        market = _build_market_obj(ticker)

        # Get error model bias if available
        city = _extract_city_code(ticker) or ""
        city_bias = tracker.get_bias(city) if tracker.has_good_data(city) else 0.0
        city_std = tracker.get_error_std(city) if tracker.has_good_data(city) else None

        # Run v2 model
        fv = estimate_market_prob(
            market,
            forecast_nws=None,  # No NWS forecast available for past trades
            forecast_om=None,   # No OM forecast available either
            city_bias_f=city_bias,
            city_error_std_f=city_std,
        )

        v2_prob = fv.get("fair_prob", 0.5)
        if v2_prob is None:
            v2_prob = 0.5

        # Outcome: True = YES won
        outcome = t.get("outcome") == "yes"

        # Market prob at entry
        market_prob = float(t.get("market_prob", 0.5))

        # Old model prob
        old_prob = float(t.get("our_prob", 0.5))

        # Directional correctness
        # v2: does our_prob point the right way?
        v2_correct_dir = (v2_prob > 0.5 and outcome) or (v2_prob < 0.5 and not outcome)
        old_correct_dir = (old_prob > 0.5 and outcome) or (old_prob < 0.5 and not outcome)

        # Brier scores
        v2_brier = _compute_brier(v2_prob, outcome)
        old_brier = _compute_brier(old_prob, outcome)
        mkt_brier = _compute_brier(market_prob, outcome)

        results.append({
            "ticker": ticker,
            "v2_prob": v2_prob,
            "old_prob": old_prob,
            "market_prob": market_prob,
            "outcome": outcome,
            "v2_brier": v2_brier,
            "old_brier": old_brier,
            "mkt_brier": mkt_brier,
            "v2_correct_dir": v2_correct_dir,
            "old_correct_dir": old_correct_dir,
        })

    n = len(results)
    if n == 0:
        return {
            "n": 0, "brier_mean": 0, "mkt_brier_mean": 0,
            "brier_skill": None, "directional_accuracy": 0,
            "calibration_slope": 0, "buckets": [],
            "old_brier_mean": 0, "old_bss": None,
        }

    # Aggregate metrics
    v2_briers = [r["v2_brier"] for r in results]
    old_briers = [r["old_brier"] for r in results]
    mkt_briers = [r["mkt_brier"] for r in results]

    v2_brier_mean = sum(v2_briers) / n
    old_brier_mean = sum(old_briers) / n
    mkt_brier_mean = sum(mkt_briers) / n

    v2_bss = 1.0 - (v2_brier_mean / mkt_brier_mean) if mkt_brier_mean > 0 else None
    old_bss = 1.0 - (old_brier_mean / mkt_brier_mean) if mkt_brier_mean > 0 else None

    v2_correct = sum(1 for r in results if r["v2_correct_dir"])
    old_correct = sum(1 for r in results if r["old_correct_dir"])

    v2_dir_acc = v2_correct / n
    old_dir_acc = old_correct / n

    # Calibration buckets (v2)
    buckets: list[dict] = []
    for i in range(10):
        lo = i / 10.0
        hi = (i + 1) / 10.0
        label_s = f"{int(lo*100)}-{int(hi*100)}%"
        items = [r for r in results if lo <= r["v2_prob"] < hi or (hi == 1.0 and r["v2_prob"] == 1.0)]
        if items:
            mean_our = sum(r["v2_prob"] for r in items) / len(items)
            mean_outcome = sum(1.0 if r["outcome"] else 0.0 for r in items) / len(items)
        else:
            mean_our = None
            mean_outcome = None
        buckets.append({
            "range": label_s,
            "n": len(items),
            "mean_our_prob": round(mean_our, 4) if mean_our is not None else None,
            "mean_outcome": round(mean_outcome, 4) if mean_outcome is not None else None,
            "diff": round(mean_our - mean_outcome, 4) if mean_our is not None else None,
        })

    # Calibration slope: regress actual ~ our_prob through origin
    valid_buckets = [b for b in buckets if b["n"] > 0 and b["mean_our_prob"] is not None]
    if valid_buckets and len(valid_buckets) >= 3:
        # Weighted least squares through origin
        xy = sum(b["mean_our_prob"] * b["mean_outcome"] * b["n"] for b in valid_buckets)
        xx = sum(b["mean_our_prob"] ** 2 * b["n"] for b in valid_buckets)
        slope = xy / xx if xx > 0 else 0.0
    else:
        slope = 0.0

    # Print detailed breakdown for holdout
    if label == "holdout" and VERBOSE:
        print("\n  Individual trades:")
        for r in sorted(results, key=lambda x: abs(x["v2_prob"] - (1.0 if x["outcome"] else 0.0)), reverse=True)[:10]:
            outcome_s = "YES" if r["outcome"] else "NO"
            correct_s = "✓" if r["v2_correct_dir"] else "✗"
            print(f"    {r['ticker'][:30]:30s} "
                  f"v2={r['v2_prob']:.3f} old={r['old_prob']:.3f} "
                  f"market={r['market_prob']:.3f} "
                  f"actual={outcome_s:3s} {correct_s}")

    # Print worst calibration buckets
    if label == "v2":
        worst = max(valid_buckets, key=lambda b: abs(b["diff"])) if valid_buckets else None
        if worst:
            print(f"\n  Worst bucket: {worst['range']} (diff={worst['diff']:+.3f})")

    return {
        "n": n,
        "brier_mean": v2_brier_mean,
        "mkt_brier_mean": mkt_brier_mean,
        "brier_skill": v2_bss,
        "directional_accuracy": v2_dir_acc,
        "calibration_slope": slope,
        "buckets": buckets,
        "old_brier_mean": old_brier_mean,
        "old_bss": old_bss,
        "old_dir_accuracy": old_dir_acc,
    }


if __name__ == "__main__":
    backtest()
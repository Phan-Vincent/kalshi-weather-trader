#!/usr/bin/env python3
"""
bin/model_brier.py — Model calibration tracker for Kalshi weather fair-value model.

Tracks fair_prob vs actual outcome for ALL settled trades (not just the ones
the trader entered). This is the model-level calibration — no selection bias
from the scanner's trading decisions.

Output:
  - Reliability diagram (fair_prob deciles vs actual win rate)
  - Overconfidence ratio (model Brier vs market Brier decomposed)
  - Per-city calibration drift

Usage:
  python3 bin/model_brier.py                    # full report
  python3 bin/model_brier.py --json             # JSON output
  python3 bin/model_brier.py --since 2026-06-01 # filter by date
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# repo root on sys.path so the shared data.weather_data helpers import when run as a CLI
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

_STATE_DIR = Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"
# Fit source (quant review 2026-07-01, QA-14 class): root brier-log froze 6/19 when arms moved
# to state/<arm>/ — default to the settling state/paper book; KALSHI_WEATHER_BRIER_LOG overrides.
# model-calibration.json still WRITES to root (dashboards read it there).
_BRIER_LOG = Path(os.environ.get("KALSHI_WEATHER_BRIER_LOG", str(_STATE_DIR / "paper" / "brier-log.jsonl")))
_MODEL_CALIBRATION = _STATE_DIR / "model-calibration.json"


# ── Core ────────────────────────────────────────────────────────────

def load_settled_trades(brier_path: Path | None = None) -> list[dict]:
    """Load all settled trades from brier log.

    Returns list of dicts with: our_prob, market_prob, outcome, ticker, side,
    settled_at_utc, timestamp_utc.
    """
    path = brier_path or _BRIER_LOG
    if not path.exists():
        return []

    opens: dict[str, dict] = {}
    settled: list[dict] = []

    with open(path) as f:
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
            status = rec.get("status")
            if status == "open":
                opens[rid] = rec
            elif status == "settled":
                settled.append(rec)

    paired = []
    for s in settled:
        rid = s.get("record_id")
        o = opens.get(rid)
        if o:
            paired.append({**o, **s})
        else:
            # In-place settled record
            paired.append(s)

    return [p for p in paired if p.get("our_prob") is not None and p.get("outcome")]


def compute_model_brier(trades: list[dict]) -> dict:
    """Compute model-level calibration metrics from settled trades.

    Model Brier = mean((fair_prob_of_yes - actual_yes)^2)
    Market Brier = mean((market_prob_of_yes - actual_yes)^2)

    Unlike trader Brier (which uses winning-side probabilities),
    this uses YES-space probabilities for direct comparability.

    2026-07-13 (drawdown fix #2): forecast-DECOUPLED fills (the premium-capture arm,
    which prices 100% off the order book and only LOGS fair_prob — scanner.py) are
    excluded, because scoring a probability that never drove the trade reads as a model
    defect when it is not (this is the source of the false "live NO 0.61→0.27
    miscalibration" alarm). A record opts out via fair_prob_decoupled=True or
    mode=="premium"; the default forecast paper book carries neither, so this is a no-op
    there. Results are also split by SELECTED side so a NO-side calibration bias — the
    model runs systematically too low on P(yes) exactly where it selects NO — is visible
    instead of being averaged away in the global diagram.
    """
    if not trades:
        return {"n_trades": 0, "model_brier": None}

    n_decoupled = sum(1 for t in trades if t.get("fair_prob_decoupled") or t.get("mode") == "premium")
    trades = [t for t in trades if not (t.get("fair_prob_decoupled") or t.get("mode") == "premium")]
    if not trades:
        return {"n_trades": 0, "model_brier": None, "n_decoupled_excluded": n_decoupled}

    model_briers = []
    market_briers = []
    for t in trades:
        p_yes = float(t["our_prob"])  # fair_prob is P(yes)
        actual = 1.0 if t["outcome"] == "yes" else 0.0

        # Market probability of YES
        side = t.get("side", "yes")
        market_prob_side = float(t.get("market_prob", 0.5))
        if side == "yes":
            market_p_yes = market_prob_side
        else:
            market_p_yes = 1.0 - market_prob_side

        model_briers.append((p_yes - actual) ** 2)
        market_briers.append((market_p_yes - actual) ** 2)

    n = len(model_briers)
    model_brier = sum(model_briers) / n
    market_brier = sum(market_briers) / n
    bss = 1.0 - model_brier / market_brier if market_brier > 0 else None

    # Reliability diagram: group by fair_prob decile
    buckets = []
    for i in range(10):
        lo = i / 10.0
        hi = (i + 1) / 10.0
        bucket_trades = [
            t for t in trades
            if (lo <= float(t["our_prob"]) < hi)
            or (hi == 1.0 and float(t["our_prob"]) == 1.0)
        ]
        if bucket_trades:
            mean_prob = sum(float(t["our_prob"]) for t in bucket_trades) / len(bucket_trades)
            mean_outcome = sum(
                1.0 if t["outcome"] == "yes" else 0.0
                for t in bucket_trades
            ) / len(bucket_trades)
        else:
            mean_prob = None
            mean_outcome = None
        buckets.append({
            "decile": f"{int(lo*100)}-{int(hi*100)}%",
            "n": len(bucket_trades),
            "mean_prob": round(mean_prob, 4) if mean_prob is not None else None,
            "mean_outcome": round(mean_outcome, 4) if mean_outcome is not None else None,
            "bias": round(mean_prob - mean_outcome, 4)
                if mean_prob is not None and mean_outcome is not None else None,
        })

    # Overconfidence metric: avg |prob - 0.5| vs actual accuracy -> 0.5
    overconfidence = sum(abs(float(t["our_prob"]) - 0.5) for t in trades) / n
    accuracy_from_50 = abs(
        sum(1.0 if t["outcome"] == "yes" else 0.0 for t in trades) / n - 0.5
    )
    overconfidence_ratio = overconfidence / accuracy_from_50 if accuracy_from_50 > 0 else None

    # Per-city model Brier
    city_brier = {}
    for t in trades:
        city = _extract_city(t.get("ticker", ""))
        if city:
            p_yes = float(t["our_prob"])
            actual = 1.0 if t["outcome"] == "yes" else 0.0
            city_brier.setdefault(city, []).append((p_yes - actual) ** 2)
    city_stats = {}
    for city, vals in city_brier.items():
        city_stats[city] = {
            "n": len(vals),
            "model_brier": round(sum(vals) / len(vals), 6),
        }

    # Per-SELECTED-SIDE calibration (drawdown fix #2). For each side we compare the model's
    # predicted win probability ON THE SELECTED SIDE (yes→fair_prob, no→1-fair_prob) to the
    # realized win rate on that side. A large positive calibration_gap on the NO subset is the
    # "model too confident on its NO picks" signature the global diagram hides.
    def _side_stats(subset: list[dict]) -> dict | None:
        if not subset:
            return None
        m = len(subset)
        model_b = sum(
            (float(t["our_prob"]) - (1.0 if t["outcome"] == "yes" else 0.0)) ** 2 for t in subset
        ) / m
        pred_win = sum(
            (float(t["our_prob"]) if t.get("side", "yes") == "yes" else 1.0 - float(t["our_prob"]))
            for t in subset
        ) / m
        realized_win = sum(
            1.0 if ((t["outcome"] == "yes") == (t.get("side", "yes") == "yes")) else 0.0
            for t in subset
        ) / m
        return {
            "n": m,
            "model_brier": round(model_b, 6),
            "pred_win_prob": round(pred_win, 4),
            "realized_win_rate": round(realized_win, 4),
            "calibration_gap": round(pred_win - realized_win, 4),
        }

    by_side = {
        "yes": _side_stats([t for t in trades if t.get("side", "yes") == "yes"]),
        "no": _side_stats([t for t in trades if t.get("side") == "no"]),
    }

    return {
        "n_trades": n,
        "n_decoupled_excluded": n_decoupled,
        "model_brier": round(model_brier, 6),
        "market_brier": round(market_brier, 6),
        "brier_skill_score": round(bss, 6) if bss is not None else None,
        "overconfidence_ratio": round(overconfidence_ratio, 4) if overconfidence_ratio is not None else None,
        "reliability": buckets,
        "by_side": by_side,
        "per_city": city_stats,
    }


def save_calibration(calib: dict, path: Path | None = None) -> None:
    """Persist model calibration to state/model-calibration.json."""
    path = path or _MODEL_CALIBRATION
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        **calib,
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    tmp.rename(path)


# ── Helpers ──────────────────────────────────────────────────────────

def _extract_city(ticker: str) -> str:
    """QA-12 (2026-07-01): delegate to the shared data.weather_data.extract_city — this was a
    third divergent copy (brier/calibrate were unified earlier)."""
    from data.weather_data import extract_city
    return extract_city(ticker)


# ── CLI ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Model calibration tracker")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--since", type=str, help="Filter by date (YYYY-MM-DD)")
    parser.add_argument("--save", action="store_true", help="Save to state/model-calibration.json")
    args = parser.parse_args()

    trades = load_settled_trades()
    if args.since:
        trades = [
            t for t in trades
            if (t.get("settled_at_utc", "")[:10] >= args.since
                or t.get("timestamp_utc", "")[:10] >= args.since)
        ]

    result = compute_model_brier(trades)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=" * 60)
        print("  MODEL CALIBRATION REPORT (fair_prob vs actual)")
        print("=" * 60)
        print(f"  Settled trades  : {result['n_trades']}")
        if result.get("n_decoupled_excluded"):
            print(f"  Excluded (forecast-decoupled/premium): {result['n_decoupled_excluded']}")
        if result["model_brier"] is not None:
            print(f"  Model Brier     : {result['model_brier']:.6f}")
            print(f"  Market Brier    : {result['market_brier']:.6f}")
            bss = result["brier_skill_score"]
            print(f"  Model BSS       : {bss:+.6f}" if bss is not None else "  Model BSS       : N/A")
            print(f"  Overconf. ratio : {result['overconfidence_ratio']:.4f}" if result["overconfidence_ratio"] is not None else "")
            print()
            print("  Reliability Diagram:")
            print(f"  {'Decile':>10s}  {'N':>4s}  {'Model Prob':>10s}  {'Actual Rate':>12s}  {'Bias':>8s}")
            print("  " + "-" * 54)
            for b in result["reliability"]:
                n = b["n"]
                if n > 0:
                    prob = f"{b['mean_prob']:.4f}" if b["mean_prob"] is not None else "?"
                    actual = f"{b['mean_outcome']:.4f}" if b["mean_outcome"] is not None else "?"
                    bias = f"{b['bias']:+.4f}" if b["bias"] is not None else "?"
                    bar = "█" * min(n, 40)
                    print(f"  {b['decile']:>10s}  {n:>4d}  {prob:>10s}  {actual:>12s}  {bias:>8s}  {bar}")
            print()
            print("  Calibration by selected side (pred win prob vs realized win rate):")
            print(f"  {'Side':>5s}  {'N':>4s}  {'PredWin':>8s}  {'Realized':>9s}  {'Gap':>8s}")
            print("  " + "-" * 40)
            for _sd in ("yes", "no"):
                st = (result.get("by_side") or {}).get(_sd)
                if st:
                    flag = "  ⚠️ overconfident" if st["calibration_gap"] >= 0.15 else ""
                    print(f"  {_sd.upper():>5s}  {st['n']:>4d}  {st['pred_win_prob']:>8.4f}  "
                          f"{st['realized_win_rate']:>9.4f}  {st['calibration_gap']:>+8.4f}{flag}")
            print()
            print("  Per-City Model Brier:")
            for city, stats in sorted(result["per_city"].items(), key=lambda x: -x[1]["n"]):
                print(f"    {city:>4s}: n={stats['n']:>3d}  model_brier={stats['model_brier']:.4f}")

    if args.save:
        save_calibration(result)
        print(f"\n  Saved to {_MODEL_CALIBRATION}")

    if result["n_trades"] == 0:
        print("\n  No settled trades found.")
        sys.exit(0)

    # Exit with warning if model is overconfident
    if result.get("overconfidence_ratio") and result["overconfidence_ratio"] > 2.0:
        print("\n  ⚠️  Model is overconfident (ratio > 2.0). Review bias correction.")
        sys.exit(1)

    sys.exit(0)

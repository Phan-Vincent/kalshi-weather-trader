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
"""
bin/backtest_v3.py — Fast model iteration via historical backtesting.

Generates thousands of synthetic Kalshi-style markets from historical
weather data, runs the FULL model pipeline (climatology + persistence +
ensemble likelihood + NBM blend + calibration), and scores against
actual observed outcomes.

Key insight: we can't wait 2-3 days per settlement cycle to measure
model improvements. With 5 years of historical data across 20 cities,
we get ~36,500 market simulations in under a minute.

Approach:
  1. For each city/date in historical record, generate realistic
     Kalshi thresholds around the climatological mean
  2. Create synthetic forecasts by adding realistic noise to the
     ACTUAL observation (simulates an ensemble forecast)
  3. Run the full fair_value pipeline + calibration
  4. Score Brier, calibration, discrimination against actual outcomes

Noise model: calibrated to match real ensemble error characteristics
  - Ensemble members: actual ± N(0, σ_ens) where σ_ens ≈ 2-3°F
  - NBM forecast: actual ± N(0, σ_nbm) where σ_nbm ≈ 1.5-2°F
  - Bias per city from error_model.json

Usage:
  python3 bin/backtest_v3.py                    # full backtest
  python3 bin/backtest_v3.py --years 3           # 3 years only
  python3 bin/backtest_v3.py --cities PHX,SEA    # specific cities
  python3 bin/backtest_v3.py --compare           # compare v2 vs v3 model
"""

# from __future__ import annotations  # neutralized: quarantined file, the top guard exits first

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_weather.data.weather_data import STATIONS
from kalshi_weather.model.prior import Climatology
from kalshi_weather.model.persistence import (
    persistence_exceedance_prob, persistence_below_prob,
    persistence_weight as persist_weight_fn,
)
from kalshi_weather.model.likelihood import (
    ensemble_likelihood_above, ensemble_likelihood_below,
    resolve_scale, prob_between_studentt,
)
from kalshi_weather.model.posterior import (
    blend_posterior_above, blend_posterior_below,
)
from kalshi_weather.model.calibrate import IsotonicCalibrator, BetaCalibrator
from kalshi_weather.model.error_tracker import ErrorTracker


# ── Config ──────────────────────────────────────────────────────────

# How much noise to add to actual → synthetic forecast
# Calibrated from 207 real settlements: fair_prob mean=0.285, p10=0.11, p90=0.46
# The total forecast error (ens_mean - actual) needs to be ~3-5°F to 
# produce fair_probs in the 10-50% range we see in real trading.
ENSEMBLE_NOISE_STD = 4.0   # °F — total ensemble member error vs actual
NBM_NOISE_STD = 2.5        # °F — NBM is more accurate but still imperfect
N_ENSEMBLE_MEMBERS = 3     # match our real ensemble size

# How many thresholds to test per city/date
N_THRESHOLDS = 5

# Threshold offsets from climatological mean (°F)
# Wider offsets → more realistic probability distribution
THRESHOLD_OFFSETS = [-6, -3, 0, +3, +6]


def _gaussian_sf(z: float) -> float:
    """Standard normal survival function."""
    return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))


def generate_synthetic_forecasts(
    actual_f: float,
    city: str,
    mtype: str,
    n_members: int = N_ENSEMBLE_MEMBERS,
    seed: Optional[int] = None,
) -> Tuple[List[float], float]:
    """Generate synthetic ensemble + NBM forecasts around an actual observation.
    
    Returns (member_forecasts, nbm_forecast).
    The noise is calibrated to match real forecast error characteristics.
    """
    if seed is not None:
        random.seed(seed)
    
    # Ensemble members: actual + noise
    members = [
        round(actual_f + random.gauss(0, ENSEMBLE_NOISE_STD), 1)
        for _ in range(n_members)
    ]
    
    # NBM is more accurate (less noise)
    nbm = round(actual_f + random.gauss(0, NBM_NOISE_STD), 1)
    
    return members, nbm


def generate_markets(
    city_code: str,
    date_iso: str,
    mtype: str,
    climo: Climatology,
    actual_f: float,
    n_thresholds: int = N_THRESHOLDS,
) -> List[Dict]:
    """Generate realistic Kalshi-style markets for a city/date.
    
    Thresholds are placed at climatological percentiles to ensure
    a mix of easy/hard predictions.
    """
    mmdd = date_iso[5:10]
    
    # Get climatological mean for threshold placement
    day_stats = climo.summary(city_code, mmdd, mtype)
    if not day_stats:
        return []
    
    sorted_temps = day_stats["sorted"]
    if not sorted_temps:
        return []
    
    # Climatological mean (median)
    n = len(sorted_temps)
    clim_mean = sorted(sorted_temps)[n // 2]
    
    markets = []
    for offset in THRESHOLD_OFFSETS:
        thresh = round((clim_mean + offset) * 2) / 2  # round to nearest 0.5
        
        # Don't generate duplicate thresholds
        if markets and abs(thresh - markets[-1]["threshold_f"]) < 1.0:
            continue
        
        # Generate both above and below markets
        for bin_kind in ["above", "below"]:
            # Determine outcome
            if bin_kind == "above":
                outcome = 1.0 if actual_f > thresh else 0.0
            else:
                outcome = 1.0 if actual_f < thresh else 0.0
            
            ticker_prefix = "KXHIGHT" if mtype == "daily_high" else "KXLOWT"
            date_code = date_iso[2:4] + ["JAN","FEB","MAR","APR","MAY","JUN",
                        "JUL","AUG","SEP","OCT","NOV","DEC"][int(date_iso[5:7])-1] + date_iso[8:10]
            bin_suffix = f"B{thresh:.1f}".rstrip('0').rstrip('.') if '.' in f"{thresh:.1f}" else f"B{thresh:.0f}"
            ticker = f"{ticker_prefix}{city_code}-{date_code}-{bin_suffix}"
            
            markets.append({
                "ticker": ticker,
                "market_type": mtype,
                "city_code": city_code,
                "date_iso": date_iso,
                "threshold_f": thresh,
                "bin_kind": bin_kind,
                "bin_low": thresh - 1.0,
                "bin_high": thresh + 1.0,
                "_outcome": outcome,
                "_actual_f": actual_f,
            })
    
    return markets


def run_model(
    market: Dict,
    climo: Climatology,
    et: ErrorTracker,
    iso_cal: IsotonicCalibrator,
    beta_cal: Optional[BetaCalibrator] = None,
    use_nbm: bool = True,
    use_persistence: bool = True,
    use_calibration: bool = True,
    calibration_method: str = "isotonic",
) -> Dict:
    """Run the full fair-value model on a synthetic market.
    
    Returns dict with predicted probabilities and metadata.
    """
    ticker = market["ticker"]
    mtype = market["market_type"]
    city_code = market["city_code"]
    date_iso = market["date_iso"]
    threshold_f = market["threshold_f"]
    bin_kind = market["bin_kind"]
    bin_low = market["bin_low"]
    bin_high = market["bin_high"]
    actual_f = market["_actual_f"]
    
    mmdd = date_iso[5:10]
    
    # ── Climatological prior ──
    prior_prob = None
    if climo.has_city(city_code):
        if bin_kind == "above":
            prior_prob = climo.exceedance_prob(city_code, mmdd, mtype, threshold_f)
        elif bin_kind == "below":
            prior_prob = climo.below_prob(city_code, mmdd, mtype, threshold_f)
    
    prior_weight = Climatology.prior_weight(24)  # assume ~24h horizon
    
    # ── Persistence prior ──
    if use_persistence and prior_prob is not None:
        # For backtesting: yesterday's temp is the PREVIOUS day's actual
        # This is realistic — persistence uses yesterday's observation
        # to predict today. In the backtest, we simulate this by adding
        # noise to today's actual (simulating day-to-day change).
        yesterday_f = actual_f + random.gauss(0, 2.5)  # day-to-day volatility
        
        try:
            if bin_kind == "above":
                persist_p = persistence_exceedance_prob(threshold_f, yesterday_f, mtype)
            elif bin_kind == "below":
                persist_p = persistence_below_prob(threshold_f, yesterday_f, mtype)
            else:
                persist_p = None
            
            if persist_p is not None:
                p_weight = persist_weight_fn(24)
                prior_prob = p_weight * persist_p + (1.0 - p_weight) * prior_prob
        except Exception:
            pass
    
    # ── Generate synthetic forecasts ──
    seed = hash(ticker) % (2**31)
    member_forecasts, nbm_f = generate_synthetic_forecasts(actual_f, city_code, mtype, seed=seed)
    
    # ── NBM blend ──
    if use_nbm and nbm_f is not None and member_forecasts:
        ens_mean = sum(member_forecasts) / len(member_forecasts)
        blended = 0.7 * nbm_f + 0.3 * ens_mean
        shift = blended - ens_mean
        member_forecasts = [f + shift for f in member_forecasts]
    
    # ── Scale ──
    ens_spread = max(member_forecasts) - min(member_forecasts) if len(member_forecasts) >= 2 else 2.0
    ens_spread *= 0.8  # range → approximate std
    city_bias = et.get_bias(city_code)
    city_std = et.get_error_std(city_code)
    scale = resolve_scale(ens_spread, city_std, 24, ticker=ticker)
    
    # ── Likelihood + Posterior ──
    fair_prob_raw = None
    likelihood_prob = None
    
    if member_forecasts:
        if bin_kind == "above":
            likelihood_prob = ensemble_likelihood_above(
                threshold_f, member_forecasts, scale, bias_f=city_bias, error_std_f=city_std or 2.0
            )
            fair_prob_raw = blend_posterior_above(
                threshold_f, prior_prob, member_forecasts, scale,
                hours_to_close=24, bias_f=city_bias, error_std_f=city_std or 2.0, ticker=ticker,
            )
        elif bin_kind == "below":
            likelihood_prob = ensemble_likelihood_below(
                threshold_f, member_forecasts, scale, bias_f=city_bias, error_std_f=city_std or 2.0
            )
            fair_prob_raw = blend_posterior_below(
                threshold_f, prior_prob, member_forecasts, scale,
                hours_to_close=24, bias_f=city_bias, error_std_f=city_std or 2.0, ticker=ticker,
            )
    else:
        fair_prob_raw = prior_prob
        likelihood_prob = prior_prob
    
    # ── Calibration ──
    fair_prob = fair_prob_raw
    if use_calibration and fair_prob is not None:
        if calibration_method == "isotonic":
            fair_prob = iso_cal.calibrate(fair_prob)
        elif calibration_method == "beta" and beta_cal:
            fair_prob = beta_cal.calibrate(fair_prob)
    
    return {
        "ticker": ticker,
        "fair_prob_raw": fair_prob_raw,
        "fair_prob": fair_prob,
        "likelihood_prob": likelihood_prob,
        "prior_prob": prior_prob,
        "outcome": market["_outcome"],
        "actual_f": actual_f,
        "threshold_f": threshold_f,
        "city": city_code,
        "mtype": mtype,
        "ens_mean": sum(member_forecasts)/len(member_forecasts) if member_forecasts else None,
        "scale": scale,
    }


def load_historical_data(
    stations: List[str],
    years: int = 5,
) -> List[Dict]:
    """Load historical daily max/min from Open-Meteo Archive.
    
    Returns list of {city, date_iso, daily_high, daily_low} records.
    
    NOTE: This can be slow (many API calls). For rapid iteration,
    we generate synthetic historical data from climatology instead.
    """
    # For speed: generate from climatology (instant, no API calls)
    # This is a good approximation — we're testing the MODEL,
    # not the data source
    records = []
    prev_temps = {}  # for AR(1) autocorrelation
    climo = Climatology()
    climo.load(ROOT / "state" / "climatology.json")
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365 * years)
    
    current = start_date
    while current < end_date:
        date_iso = current.strftime("%Y-%m-%d")
        mmdd = current.strftime("%m-%d")
        
        for city in stations:
            if not climo.has_city(city):
                continue
            
            for mtype in ["daily_high", "daily_low"]:
                stats = climo.summary(city, mmdd, mtype)
                if not stats:
                    continue
                
                sorted_t = stats.get("sorted", [])
                if not sorted_t:
                    continue
                
                # Sample from climatological distribution with autocorrelation
                # AR(1) model: creates realistic heat waves and cold snaps
                prev_key = f"{city}_{mtype}"
                import statistics as st
                clim = sum(sorted_t) / len(sorted_t)
                if prev_temps.get(prev_key) is not None:
                    prev = prev_temps[prev_key]
                    actual = 0.3 * clim + 0.5 * prev + 0.2 * random.choice(sorted_t)
                else:
                    actual = random.choice(sorted_t)
                prev_temps[prev_key] = actual
                
                records.append({
                    "city": city,
                    "date_iso": date_iso,
                    "mtype": mtype,
                    "actual_f": round(actual, 1),
                })
        
        current += timedelta(days=1)
    
    return records


def compute_metrics(results: List[Dict]) -> Dict:
    """Compute Brier, calibration, discrimination from backtest results."""
    n = len(results)
    if n == 0:
        return {}
    
    # Brier
    brier = sum((r["fair_prob"] - r["outcome"])**2 for r in results) / n
    brier_raw = sum((r["fair_prob_raw"] - r["outcome"])**2 for r in results) / n
    
    # Calibration by decile
    buckets = defaultdict(lambda: {"n": 0, "fair_sum": 0, "outcome_sum": 0})
    for r in results:
        p = r["fair_prob"]
        decile = min(int(p * 10), 9)
        buckets[decile]["n"] += 1
        buckets[decile]["fair_sum"] += p
        buckets[decile]["outcome_sum"] += r["outcome"]
    
    calibration = {}
    for decile in range(10):
        b = buckets[decile]
        if b["n"] > 0:
            mean_fair = b["fair_sum"] / b["n"]
            mean_out = b["outcome_sum"] / b["n"]
            calibration[f"{decile*10}-{(decile+1)*10}%"] = {
                "n": b["n"],
                "mean_fair": round(mean_fair, 4),
                "mean_outcome": round(mean_out, 4),
                "diff": round(mean_fair - mean_out, 4),
            }
    
    # Brier decomposition
    grand_mean = sum(r["outcome"] for r in results) / n
    uncertainty = grand_mean * (1 - grand_mean)
    
    # Sort by calibrated prob, bucket for decomposition
    sorted_results = sorted(results, key=lambda r: r["fair_prob"])
    bucket_size = max(n // 10, 1)
    
    cal_error = 0
    resolution = 0
    for i in range(0, n, bucket_size):
        bucket = sorted_results[i:i+bucket_size]
        if len(bucket) < 3:
            continue
        mean_fair = sum(r["fair_prob"] for r in bucket) / len(bucket)
        mean_out = sum(r["outcome"] for r in bucket) / len(bucket)
        weight = len(bucket) / n
        cal_error += weight * (mean_fair - mean_out)**2
        resolution += weight * (mean_out - grand_mean)**2
    
    # Directional accuracy
    correct = sum(
        1 for r in results
        if (r["fair_prob"] > 0.5 and r["outcome"] == 1) or
           (r["fair_prob"] <= 0.5 and r["outcome"] == 0)
    )
    dir_acc = correct / n if n > 0 else 0
    
    # Win rate (for "trades" where we'd bet)
    # Simulate: bet when |fair_prob - 0.5| > 0.05
    bets = [r for r in results if abs(r["fair_prob"] - 0.5) > 0.05]
    bet_wins = sum(1 for r in bets if (r["fair_prob"] > 0.5) == (r["outcome"] == 1))
    bet_win_rate = bet_wins / len(bets) if bets else 0
    
    return {
        "n": n,
        "brier_raw": round(brier_raw, 6),
        "brier": round(brier, 6),
        "brier_improvement_pct": round((1 - brier/brier_raw) * 100, 2) if brier_raw > 0 else 0,
        "uncertainty": round(uncertainty, 4),
        "calibration_error": round(cal_error, 4),
        "resolution": round(resolution, 4),
        "resolution_pct": round(resolution / uncertainty * 100, 1) if uncertainty > 0 else 0,
        "directional_accuracy": round(dir_acc, 4),
        "bet_win_rate": round(bet_win_rate, 4),
        "n_bets": len(bets),
        "calibration_deciles": calibration,
        "outcome_mean": round(grand_mean, 4),
        "fair_prob_mean": round(sum(r["fair_prob"] for r in results) / n, 4),
    }


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest v3 model on historical data")
    parser.add_argument("--years", type=int, default=5, help="Years of history (default: 5)")
    parser.add_argument("--cities", type=str, default=None, help="Comma-separated city codes")
    parser.add_argument("--compare", action="store_true", help="Compare model variants")
    parser.add_argument("--sample-days", type=int, default=0, 
                       help="Sample N days randomly (0=all, for fast iteration)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()
    
    random.seed(args.seed)
    
    city_list = args.cities.split(",") if args.cities else sorted(STATIONS.keys())
    city_list = [c for c in city_list if c in STATIONS]
    
    print("=" * 60)
    print("  KALSHI WEATHER MODEL — HISTORICAL BACKTEST")
    print("=" * 60)
    print(f"  Cities: {len(city_list)} | Years: {args.years} | Markets per day: up to 10")
    print()
    
    # Load data and calibrator
    print("Loading climatology + calibration...")
    climo = Climatology()
    climo.load(ROOT / "state" / "climatology.json")
    
    et = ErrorTracker()
    et.load()
    
    iso_cal = IsotonicCalibrator()
    iso_cal_path = ROOT / "state" / "calibration.json"
    if iso_cal_path.exists():
        iso_cal.load()
    else:
        iso_cal.fit()
    
    beta_cal = BetaCalibrator()
    beta_cal_path = ROOT / "state" / "beta-calibration.json"
    if beta_cal_path.exists():
        beta_cal.load()
    
    # Generate historical data
    print(f"Generating {args.years} years of synthetic historical data...")
    records = load_historical_data(city_list, years=args.years)
    
    if args.sample_days > 0:
        records = random.sample(records, min(args.sample_days, len(records)))
        print(f"  Sampled {len(records)} records for fast iteration")
    
    # Generate markets and run model
    print(f"Generating markets and running model...")
    results = []
    for i, rec in enumerate(records):
        if i % 5000 == 0 and i > 0:
            print(f"  ... processed {i}/{len(records)} records")
        
        markets = generate_markets(
            rec["city"], rec["date_iso"], rec["mtype"],
            climo, rec["actual_f"]
        )
        
        for market in markets:
            result = run_model(
                market, climo, et, iso_cal, beta_cal,
                use_nbm=True, use_persistence=True, use_calibration=True,
            )
            results.append(result)
    
    # Compute metrics
    metrics = compute_metrics(results)
    
    print(f"\n{'='*60}")
    print(f"  RESULTS — {len(results):,} markets simulated")
    print(f"{'='*60}")
    print(f"  Outcome mean:   {metrics['outcome_mean']:.4f}")
    print(f"  Fair prob mean: {metrics['fair_prob_mean']:.4f}")
    print(f"")
    print(f"  Brier (raw):    {metrics['brier_raw']:.6f}")
    print(f"  Brier (cal):    {metrics['brier']:.6f} ({metrics['brier_improvement_pct']:.1f}% improvement)")
    print(f"")
    print(f"  Uncertainty:    {metrics['uncertainty']:.4f}")
    print(f"  Calibration:    {metrics['calibration_error']:.4f}")
    print(f"  Resolution:     {metrics['resolution']:.4f} ({metrics['resolution_pct']}% of max)")
    print(f"  Directional:    {metrics['directional_accuracy']:.4f}")
    print(f"  Bet win rate:   {metrics['bet_win_rate']:.4f} ({metrics['n_bets']} bets)")
    
    # Calibration deciles
    print(f"\n  CALIBRATION DECILES:")
    print(f"  {'Range':<12s} {'N':>6s} {'Fair':>8s} {'Out':>8s} {'Diff':>8s}")
    print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*8} {'-'*8}")
    for decile in sorted(metrics["calibration_deciles"].keys()):
        d = metrics["calibration_deciles"][decile]
        marker = " *" if abs(d["diff"]) > 0.10 else ""
        print(f"  {decile:<12s} {d['n']:>6d} {d['mean_fair']:>8.4f} {d['mean_outcome']:>8.4f} {d['diff']:>+8.4f}{marker}")
    
    # Ablation study if --compare
    if args.compare:
        print(f"\n{'='*60}")
        print(f"  ABLATION STUDY")
        print(f"{'='*60}")
        
        variants = [
            ("Baseline (no fixes)", False, False, False, "none"),
            ("+Calibration only", False, False, True, "isotonic"),
            ("+NBM only", True, False, False, "none"),
            ("+Persistence only", False, True, False, "none"),
            ("+NBM+Persistence", True, True, False, "none"),
            ("Full (all fixes)", True, True, True, "isotonic"),
        ]
        
        print(f"  {'Variant':<30s} {'Brier':>10s} {'Res%':>6s} {'DirAcc':>7s}")
        print(f"  {'-'*30} {'-'*10} {'-'*6} {'-'*7}")
        
        for name, use_nbm, use_persist, use_cal, cal_method in variants:
            var_results = []
            for rec in random.sample(records, min(2000, len(records))):
                markets = generate_markets(rec["city"], rec["date_iso"], rec["mtype"], climo, rec["actual_f"])
                for market in markets:
                    result = run_model(
                        market, climo, et, iso_cal, beta_cal,
                        use_nbm=use_nbm, use_persistence=use_persist,
                        use_calibration=use_cal, calibration_method=cal_method,
                    )
                    var_results.append(result)
            
            m = compute_metrics(var_results)
            print(f"  {name:<30s} {m['brier']:>10.6f} {m['resolution_pct']:>5.1f}% {m['directional_accuracy']:>6.4f}")
    
    print(f"\nDone. {len(results):,} total market simulations.")


if __name__ == "__main__":
    main()

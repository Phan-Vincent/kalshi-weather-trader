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
"""Calibrate backtest noise model against real fair_prob distribution."""

import json, math, random, sys, statistics, re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_weather.model.prior import Climatology
from kalshi_weather.model.error_tracker import ErrorTracker
from kalshi_weather.model.persistence import persistence_exceedance_prob
from kalshi_weather.model.likelihood import ensemble_likelihood_above, resolve_scale
from kalshi_weather.model.posterior import blend_posterior_above

climo = Climatology()
climo.load(ROOT / "state" / "climatology.json")
et = ErrorTracker(); et.load()

with open(ROOT / "state" / "archival-temps.json") as f:
    archival = json.load(f)

random.seed(42)

def quick_backtest(noise_std, n_samples=800):
    fps = []
    cities = random.sample(list(archival.keys()), min(8, len(archival)))
    for city in cities:
        records = archival[city]
        for rec in random.sample(records, min(50, len(records))):
            for mtype in ['daily_high']:
                actual = rec.get(f"{mtype}_f")
                if actual is None: continue
                mmdd = rec['date'][5:10]
                day = climo.summary(city, mmdd, mtype)
                if not day or not day.get('sorted'): continue
                sorted_t = day['sorted']
                clim_mean = sorted(sorted_t)[len(sorted_t)//2]
                
                for offset in [-8, -5, -2, +1, +4, +7]:
                    thresh = round((clim_mean + offset) * 2) / 2
                    prior_p = climo.exceedance_prob(city, mmdd, mtype, thresh)
                    if prior_p is None: prior_p = 0.5
                    
                    seed = hash(f"{city}{rec['date']}{thresh}") % (2**31)
                    random.seed(seed)
                    members = [round(actual + random.gauss(0, noise_std), 1) for _ in range(3)]
                    nbm = round(actual + random.gauss(0, noise_std * 0.65), 1)
                    
                    ens_mean = sum(members)/len(members)
                    blended = 0.7 * nbm + 0.3 * ens_mean
                    members = [f + (blended - ens_mean) for f in members]
                    
                    spread = max(members) - min(members)
                    scale = resolve_scale(spread * 0.8, et.get_error_std(city) or 2.0, 24)
                    
                    raw_p = blend_posterior_above(thresh, prior_p, members, scale, hours_to_close=24, 
                                                   bias_f=et.get_bias(city), 
                                                   error_std_f=et.get_error_std(city) or 2.0)
                    if raw_p: fps.append(raw_p)
                    if len(fps) >= n_samples: break
                if len(fps) >= n_samples: break
            if len(fps) >= n_samples: break
        if len(fps) >= n_samples: break
    
    fps_sorted = sorted(fps)
    return {'mean': statistics.mean(fps), 'p10': fps_sorted[len(fps_sorted)//10],
            'p50': fps_sorted[len(fps_sorted)//2], 'p90': fps_sorted[9*len(fps_sorted)//10], 'n': len(fps)}

# Target from 347 real settlements: mean=0.296, p10=0.139, p50=0.269, p90=0.463
print("Calibrating backtest noise to match real fair_prob distribution...")
print(f"Target: mean≈0.30, p10≈0.14, p50≈0.27, p90≈0.46")
print()

best, best_err = None, float('inf')
for sigma in [4, 5, 6, 7, 8, 9, 10, 12, 15]:
    d = quick_backtest(sigma, 600)
    err = abs(d['mean'] - 0.30) * 3 + abs(d['p50'] - 0.27) * 5 + abs(d['p10'] - 0.14) + abs(d['p90'] - 0.46)
    mark = ""
    if err < best_err:
        best_err, best = err, sigma
        mark = " ← best"
    print(f"  σ={sigma:>3.0f}°F  mean={d['mean']:.3f}  p10={d['p10']:.3f}  p50={d['p50']:.3f}  p90={d['p90']:.3f}{mark}")

print(f"\n✓ Calibrated noise: σ_ens={best}°F, σ_nbm={best*0.65:.0f}°F")

# Save for backtest_v3
with open(ROOT / "state" / "backtest-noise-calibration.json", "w") as f:
    json.dump({"sigma_ens": best, "sigma_nbm": round(best*0.65), "calibrated_utc": "2026-06-16T06:00:00Z"}, f, indent=2)
print(f"Saved calibration → state/backtest-noise-calibration.json")

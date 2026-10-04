#!/usr/bin/env python3
# ── DEPRECATED / QUARANTINED 2026-07-02 (quant review #10) — DO NOT RUN. Output is NOT validation. ──
# NOTE: backtest_real is NOT look-ahead (it uses real archived forecasts). It is quarantined only
# because it scores the USE_FORECAST=1 KDE path while LIVE runs USE_FORECAST=0 climatology, so its
# Brier describes the PAPER build, not what live trades. See bin/deprecated/README.md.
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
bin/backtest_real.py — REAL-data historical backtest of the weather model.

Unlike bin/backtest_v3.py (synthetic forecasts = noise around the actual, which
cheats by centering on the truth), this uses the REAL archived forecast
(data/historical_forecast.py, mean |err| ≈ 1.3°F) vs REAL observations
(model.error_tracker.fetch_actual_temp), over hundreds of city/days.

It scores the production FIX directly — the KDE shifted to the forecast mean
(model.prior.Climatology.kde_exceedance_prob, the KALSHI_WEATHER_USE_FORECAST path)
vs the legacy climatology (Climatology.exceedance_prob) — answering at scale:
  • does the forecast-informed model beat climatology? (Brier + point skill)
  • what KDE shrink (bandwidth) minimises error?  (sweep)

Read-only: no Kalshi calls, no orders, no live/paper state touched. Cached, so
re-runs (for tuning) are instant.

Usage:
  python3 bin/backtest_real.py --days 120
  python3 bin/backtest_real.py --days 120 --cities MIA,CHI,PHX,SEA,DEN
  python3 bin/backtest_real.py --days 120 --shrink-sweep
"""
# from __future__ import annotations  # neutralized: quarantined file, the top guard exits first

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.historical_forecast import fetch_historical_forecast, CACHE
from data.weather_data import STATIONS
from model.prior import Climatology
from model.error_tracker import fetch_actual_temp

ACT_CACHE = CACHE / "actuals-localday.json"  # versioned after the 2026-07-03 UTC→local actuals fix
OFFSETS = [-8, -6, -4, -2, 0, 2, 4, 6, 8]  # °F threshold offsets from climo mean


def load_actuals() -> dict:
    if ACT_CACHE.exists():
        try:
            return json.load(open(ACT_CACHE))
        except Exception:
            return {}
    return {}


def _save_actuals_cache(cache: dict) -> None:
    """Atomic write (tmp + os.replace) so a crash mid-dump can't truncate the shared cache."""
    import os
    try:
        tmp = f"{ACT_CACHE}.tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, ACT_CACHE)
    except Exception:
        pass


def get_actual(lat, lon, date_iso, mtype, cache) -> float | None:
    k = f"{lat:.3f}|{lon:.3f}|{date_iso}|{mtype}"
    if k in cache:
        return cache[k]
    v = fetch_actual_temp(lat, lon, date_iso, mtype)
    if v is not None:   # don't cache failures → a transient 429/None is retried next run, not pinned
        cache[k] = v
    return v


def crps_kde(climo, city, mmdd, mtype, loc, actual, shrink, lo, hi) -> float | None:
    """CRPS of the KDE-shifted-to-forecast predictive distribution vs actual,
    integrated numerically. F(x) = 1 - kde_exceedance_prob(x)."""
    n = 240
    step = (hi - lo) / n
    total = 0.0
    for i in range(n):
        x = lo + (i + 0.5) * step
        sf = climo.kde_exceedance_prob(city, mmdd, mtype, x, loc, shrink)
        if sf is None:
            return None
        F = 1.0 - sf
        total += (F - (1.0 if x >= actual else 0.0)) ** 2
    return total * step


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="days of history to replay")
    ap.add_argument("--cities", default="", help="comma list of city codes (default: all)")
    ap.add_argument("--shrink", type=float, default=1.0, help="KDE shrink for the main run")
    ap.add_argument("--shrink-sweep", action="store_true")
    ap.add_argument("--end-offset", type=int, default=3, help="end the window N days ago (obs lag)")
    args = ap.parse_args()

    climo = Climatology()
    climo.load(str(ROOT / "state" / "climatology.json"))
    cache = load_actuals()

    cities = [c.strip().upper() for c in args.cities.split(",") if c.strip()] or [
        c for c in STATIONS if climo.has_city(c)]
    end = date.today() - timedelta(days=args.end_offset)
    dates = [(end - timedelta(days=d)).isoformat() for d in range(args.days)]

    # samples: list of (mtype, loc, climo_mean, actual, [(thr, fc_fair, cl_fair, outcome)...])
    samples = []
    n_days = 0
    for code in cities:
        st = STATIONS.get(code)
        if not st or not climo.has_city(code):
            continue
        lat, lon = st["lat"], st["lon"]
        for d in dates:
            mmdd = d[5:]
            fc_hi, fc_lo = fetch_historical_forecast(lat, lon, d)
            for mtype, loc in (("daily_high", fc_hi), ("daily_low", fc_lo)):
                if loc is None:
                    continue
                actual = get_actual(lat, lon, d, mtype, cache)
                cm = climo.get_climo_mean(code, mmdd, mtype)
                if actual is None or cm is None:
                    continue
                mkts = []
                for off in OFFSETS:
                    thr = round((cm + off) * 2) / 2
                    fc = climo.kde_exceedance_prob(code, mmdd, mtype, thr, loc, args.shrink)
                    cl = climo.exceedance_prob(code, mmdd, mtype, thr)
                    if fc is None or cl is None:
                        continue
                    mkts.append((thr, fc, cl, 1 if actual > thr else 0))
                if mkts:
                    samples.append((code, mmdd, mtype, loc, cm, actual, mkts))
                    n_days += 1
        # Checkpoint the actuals cache after each city so a long run's progress survives an
        # interruption — a re-run then resumes cache-first instead of re-fetching from scratch.
        _save_actuals_cache(cache)

    _save_actuals_cache(cache)

    if not samples:
        print("No samples (check network / climatology coverage).")
        return 1

    # ── Brier: forecast-informed (fix) vs climatology (legacy) ──
    fc_b = [(fc - o) ** 2 for _, _, _, _, _, _, mk in samples for _, fc, _, o in mk]
    cl_b = [(cl - o) ** 2 for _, _, _, _, _, _, mk in samples for _, _, cl, o in mk]
    fcB, clB = sum(fc_b) / len(fc_b), sum(cl_b) / len(cl_b)
    # point skill
    fe = [abs(loc - a) for _, _, _, loc, _, a, _ in samples]
    ce = [abs(cm - a) for _, _, _, _, cm, a, _ in samples]
    fr = math.sqrt(sum(x * x for x in fe) / len(fe)); cr = math.sqrt(sum(x * x for x in ce) / len(ce))

    print(f"REAL backtest — {len(cities)} cities, {args.days}d, {n_days} city-day-types, "
          f"{len(fc_b)} thresholds scored\n")
    print(f"BRIER   forecast-informed={fcB:.4f}   climatology={clB:.4f}   "
          f"skill_vs_climo={((clB-fcB)/clB):+.1%}  "
          f"({'forecast WINS' if fcB < clB else 'climatology wins'})")
    print(f"POINT   forecast RMSE={fr:.2f}°F   climo RMSE={cr:.2f}°F   "
          f"skill={1-fr/cr:+.1%}")

    # ── Prob-floor sweep: the 12% GLOBAL_PROB_FLOOR was a band-aid for the broken
    #    model; for the good model it just clamps accurate low-prob tails upward.
    #    (Trading price-floor is handled separately by risk min_price_cents=20.)
    flat = [(fc, o) for _, _, _, _, _, _, mk in samples for _, fc, _, o in mk]
    print("\nPROB-FLOOR SWEEP (Brier of forecast model with floor applied):")
    bestf = (None, 1e9)
    for fl in [0.0, 0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15]:
        b = sum((max(fc, fl) - o) ** 2 for fc, o in flat) / len(flat)
        if b < bestf[1]:
            bestf = (fl, b)
        print(f"  floor={fl:<4}: Brier={b:.4f}")
    print(f"  → Brier-optimal floor ≈ {bestf[0]} (vs current 0.12). Lower trusts the model's tails.")

    if args.shrink_sweep:
        print("\nKDE SHRINK SWEEP (CRPS of the forecast-shifted distribution):")
        best = (None, 1e9)
        for sh in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            cs = []
            for code, mmdd, mtype, loc, cm, a, _ in samples:
                c = crps_kde(climo, code, mmdd, mtype, loc, a, sh, cm - 25, cm + 25)
                if c is not None:
                    cs.append(c)
            m = sum(cs) / len(cs)
            if m < best[1]:
                best = (sh, m)
            print(f"  shrink={sh:<4}: CRPS={m:.3f}")
        print(f"  → CRPS-optimal KDE shrink ≈ {best[0]} (CRPS={best[1]:.3f})")
    print("\n(Forecast = real archived forecast ~12-18h lead; obs = Open-Meteo archive. "
          "No market prices here — edge-vs-market is bin/shadow_score.py forward.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

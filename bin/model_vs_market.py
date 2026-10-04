#!/usr/bin/env python3
"""
bin/model_vs_market.py — does the FIXED model beat the MARKET's Brier?

Recomputes the forecast-informed model's probability on real settled markets where
we have the recorded market price (state/brier-log.jsonl, 81 settled w/ market_prob),
using the REAL historical forecast (data/historical_forecast.py) + the tuned KDE, and
scores it head-to-head against the market's recorded Brier — overall and BY EDGE
BUCKET (does the model beat the market where it disagrees most? — the selectivity
question that matters for P&L, not the average).

Convention (verified): brier-log `our_prob` = P(YES); outcome ∈ {yes,no} is the YES
resolution; `market_brier` = (market_prob_for_outcome − 1)². The brier-log `actual`
field is unreliable, so we fetch real actuals (cached) and infer each market's
YES-definition (above/below/between) from (actual, threshold, outcome).

BIAS CAVEAT: brier-log only has markets the (broken) bot TRADED — a biased, hard
subset. The unbiased read is bin/shadow_score.py forward (all priced markets).
Read-only; no Kalshi calls, no orders, no state writes.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # crps_report helpers

from crps_report import parse_ticker
from data.historical_forecast import fetch_historical_forecast
from data.weather_data import STATIONS
from model.prior import Climatology
from model.error_tracker import fetch_actual_temp

STATE = ROOT / "state"
# Cache filename is VERSIONED so a semantics change starts fresh: -localday (2026-07-03 station-local
# fix) then -stationcorr (2026-07-06 per-city grid→station correction; fetch_actual_temp now gets a
# correction_city=). A fresh file re-fetches corrected values rather than serving a raw/corrected mix.
ACT_CACHE = STATE / "backtest-cache" / "actuals-localday-stationcorr.json"
_TNUM = re.compile(r"-([BT])([\d.]+)$")
SHRINK = 0.5  # tuned paper config


def main() -> int:
    climo = Climatology()
    climo.load(str(STATE / "climatology.json"))
    cache = json.load(open(ACT_CACHE)) if ACT_CACHE.exists() else {}

    recs = [json.loads(l) for l in open(STATE / "brier-log.jsonl") if l.strip()]
    recs = [r for r in recs if r.get("status") == "settled"
            and r.get("market_prob") is not None and r.get("market_brier") is not None]

    samples = []  # (model_brier, market_brier, edge_cents)
    skipped = 0
    for r in recs:
        tp = parse_ticker(r.get("ticker", ""))
        km = _TNUM.search(r.get("ticker", ""))
        if not tp or not km:
            skipped += 1; continue
        city, mtype, date_iso = tp
        kind_char, num = km.group(1), float(km.group(2))
        st = STATIONS.get(city)
        if not st or not climo.has_city(city):
            skipped += 1; continue
        outcome_yes = 1 if r.get("outcome") == "yes" else 0

        key = f"{st['lat']:.3f}|{st['lon']:.3f}|{date_iso}|{mtype}"
        actual = cache.get(key)
        if actual is None:
            # correction_city: station-consistent actual — also sharpens the below/above direction
            # inference at line ~88 (a grid actual on the wrong side of the strike flipped it).
            actual = fetch_actual_temp(st["lat"], st["lon"], date_iso, mtype, correction_city=city)
            cache[key] = actual
        if actual is None:
            skipped += 1; continue

        hi, lo = fetch_historical_forecast(st["lat"], st["lon"], date_iso)
        loc = hi if mtype == "daily_high" else lo
        if loc is None:
            skipped += 1; continue

        mmdd = date_iso[5:]
        if kind_char == "B":
            p_yes = climo.kde_between_prob(city, mmdd, mtype, num - 1, num + 1, loc, SHRINK)
        else:  # T — infer above/below from (actual, threshold, outcome)
            above = (actual > num) == (outcome_yes == 1)
            ex = climo.kde_exceedance_prob(city, mmdd, mtype, num, loc, SHRINK)
            p_yes = ex if (ex is None or above) else 1.0 - ex
        if p_yes is None:
            skipped += 1; continue

        pfo = p_yes if outcome_yes else (1.0 - p_yes)
        model_brier = (pfo - 1.0) ** 2
        market_brier = r["market_brier"]
        # market P(YES) for edge bucket
        mpfo = r.get("market_prob_for_outcome")
        market_yes = mpfo if outcome_yes else (1.0 - mpfo) if mpfo is not None else None
        edge = abs(p_yes - market_yes) * 100 if market_yes is not None else None
        samples.append((model_brier, market_brier, edge))

    try:
        json.dump(cache, open(ACT_CACHE, "w"))
    except Exception:
        pass

    if not samples:
        print("No scorable samples.")
        return 1

    mo = sum(s[0] for s in samples) / len(samples)
    ma = sum(s[1] for s in samples) / len(samples)
    print("⚠️  UNRELIABLE — the historical estimate swings from -120% to +34% depending on\n"
          "    reconstruction assumptions (proxy forecast, market-definition kind, proxy actuals,\n"
          "    corrupted-city data) + traded-set bias. Do NOT trust the magnitude. The only clean\n"
          "    test is bin/shadow_score.py forward (real fair_prob + real market price + real Kalshi\n"
          "    outcome). Kept for reference / the edge-bucket pattern only.\n")
    print(f"FIXED model vs MARKET — {len(samples)} settled markets (skipped {skipped})\n")
    print(f"  MODEL Brier  = {mo:.4f}")
    print(f"  MARKET Brier = {ma:.4f}")
    print(f"  skill_vs_market = {(ma-mo)/ma:+.1%}  "
          f"({'MODEL beats market' if mo < ma else 'market beats model'})\n")

    print("By edge bucket |P_model(YES) − market(YES)| — the selectivity test:")
    buckets = [(0, 5), (5, 15), (15, 30), (30, 1e9)]
    names = ["0-5¢", "5-15¢", "15-30¢", "30¢+"]
    for (lo, hi), nm in zip(buckets, names):
        b = [s for s in samples if s[2] is not None and lo <= s[2] < hi]
        if b:
            bmo = sum(s[0] for s in b) / len(b); bma = sum(s[1] for s in b) / len(b)
            verdict = "model wins" if bmo < bma else "market wins"
            print(f"  {nm:>6}: n={len(b):>3}  model={bmo:.4f}  market={bma:.4f}  "
                  f"skill={(bma-bmo)/bma:+.1%}  ({verdict})")
    print("\nCAVEAT: brier-log = markets the bot TRADED (biased, hard subset). "
          "Unbiased forward read = bin/shadow_score.py once it has settled data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

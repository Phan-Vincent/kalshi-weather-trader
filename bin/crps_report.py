#!/usr/bin/env python3
"""
bin/crps_report.py — CRPS diagnostic for the weather forecast *distribution*.

Why: the bot only measures Brier of *bets* (downstream, contaminated by pricing
and bin selection). CRPS (Continuous Ranked Probability Score) scores the raw
predictive distribution against the realised temperature, isolating model quality.
Lower CRPS = better. It directly answers "is our kernel bandwidth right?".

Data source: historical forecast distributions are recovered from the
`state/settlement-log.jsonl` rationale strings of the form
    "Daily high ~ N(79.00, 2.26^2); P(...)"
(153+ records carry this). Realised temps are fetched from the Open-Meteo archive
via the existing `fetch_actual_temp()` and cached.

Headline feature — BANDWIDTH SWEEP: recompute CRPS with the model scale multiplied
by a grid of factors. If the CRPS-optimal factor is < 1.0, the model is
over-smoothing (bandwidth wider than it should be) — which blurs adjacent 2°F bins
toward 0.5 and is the suspected cause of weak bin discrimination.

Usage:
    python3 bin/crps_report.py                 # full report (fetches actuals, cached)
    python3 bin/crps_report.py --limit 80      # cap unique-day fetches for a quick run
    python3 bin/crps_report.py --no-fetch      # only use cached actuals
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model.likelihood import _studentt_cdf  # exact kernel the model uses
from model.error_tracker import fetch_actual_temp
from data.weather_data import STATIONS

STATE = Path(__file__).resolve().parent.parent / "state"
SETTLE_LOG = STATE / "settlement-log.jsonl"
# Cache filename is VERSIONED so a semantics change starts a fresh file instead of serving a mix:
#   -localday   : the 2026-07-03 UTC→station-local fix (timezone=auto)
#   -stationcorr: the 2026-07-06 per-city grid→station actual correction (get_actual passes
#                 correction_city=). The old -localday file holds RAW grid values; a fresh file
#                 re-fetches corrected ones rather than serving a raw/corrected mix.
# Consumers (crps_report, leakfree_skill, validate_forecast_skill) all import this constant, so the
# switch is single-point. Old files are left in place, harmless.
ACTUALS_CACHE = STATE / "crps-actuals-cache-localday-stationcorr.json"

_DF = 3.0  # model uses Student-t df=3

# ── ticker / rationale parsing ────────────────────────────────────────────

_TICKER_RE = re.compile(r"KX(HIGH|LOW)T?([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_NORM_DIST_RE = re.compile(r"N\(([\-\d.]+),\s*([\d.]+)\^2\)")
_KIND_RE = re.compile(r"Daily (high|low)", re.I)


def parse_ticker(ticker: str):
    """Return (city_code, mtype, date_iso) or None."""
    m = _TICKER_RE.match(ticker)
    if not m:
        return None
    hl, city, yy, mon, dd = m.groups()
    mon_n = _MONTHS.get(mon.upper())
    if not mon_n:
        return None
    date_iso = f"20{yy}-{mon_n:02d}-{int(dd):02d}"
    mtype = "daily_high" if hl == "HIGH" else "daily_low"
    return city, mtype, date_iso


def parse_dist(rationale: str):
    """Return (loc, scale) from 'N(loc, scale^2)' or None."""
    m = _NORM_DIST_RE.search(rationale or "")
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


# ── CRPS via numerical integration of the model's own CDF ─────────────────

def crps_studentt(loc: float, scale: float, y: float, lo=None, hi=None) -> float:
    """CRPS = ∫ (F(x) - 1{x>=y})^2 dx for the location-scale Student-t(df=3).

    Integrated numerically on a fine grid wide enough to cover the tails.
    """
    scale = max(scale, 0.25)
    lo = lo if lo is not None else min(loc, y) - 14 * scale
    hi = hi if hi is not None else max(loc, y) + 14 * scale
    n = 1400
    step = (hi - lo) / n
    total = 0.0
    for i in range(n):
        x = lo + (i + 0.5) * step
        F = _studentt_cdf((x - loc) / scale, _DF)
        ind = 1.0 if x >= y else 0.0
        total += (F - ind) ** 2
    return total * step


# ── actuals (cached) ──────────────────────────────────────────────────────

def load_cache() -> dict:
    if ACTUALS_CACHE.exists():
        try:
            return json.load(open(ACTUALS_CACHE))
        except Exception:
            return {}
    return {}


def get_actual(city: str, mtype: str, date_iso: str, cache: dict, allow_fetch: bool):
    st = STATIONS.get(city)
    if not st:
        return None
    key = f"{city}|{mtype}|{date_iso}"
    if key in cache:
        return cache[key]
    if not allow_fetch:
        return None
    try:
        # correction_city: score against the STATION-consistent actual (grid + learned per-city
        # correction), consistent with what Kalshi settles on (audit 2026-07-06 #7). Single point
        # for crps_report / leakfree_skill / validate_forecast_skill (all call get_actual). These
        # are informational reports (no trading gate), and the correction adjusts the ACTUAL not the
        # forecast, so the leak-free property is untouched.
        v = fetch_actual_temp(st["lat"], st["lon"], date_iso, mtype, correction_city=city)
    except Exception:
        v = None
    cache[key] = v  # cache misses too (None) to avoid refetch storms
    return v


def lead_bucket(hours):
    if hours is None:
        return "unknown"
    for lo, hi, name in [(0, 6, "0-6h"), (6, 12, "6-12h"), (12, 18, "12-18h"),
                         (18, 24, "18-24h"), (24, 1e9, "24h+")]:
        if lo <= hours < hi:
            return name
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap unique (city,date,mtype) fetches")
    ap.add_argument("--no-fetch", action="store_true", help="use cached actuals only")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(SETTLE_LOG) if l.strip()]
    cache = load_cache()
    allow_fetch = not args.no_fetch

    samples = []  # (loc, scale, actual, lead_bucket, city, mtype)
    fetches = 0
    for r in rows:
        dist = parse_dist(r.get("rationale", ""))
        tp = parse_ticker(r.get("ticker", ""))
        if not dist or not tp:
            continue
        loc, scale = dist
        city, mtype, date_iso = tp
        # lead time at open
        hours = None
        try:
            from datetime import datetime
            o = datetime.fromisoformat(r["opened_utc"].replace("Z", "+00:00"))
            c = datetime.fromisoformat((r.get("market_close_time") or "").replace("Z", "+00:00"))
            hours = (c - o).total_seconds() / 3600.0
        except Exception:
            pass
        if args.limit and fetches >= args.limit and f"{city}|{mtype}|{date_iso}" not in cache:
            continue
        before = len(cache)
        actual = get_actual(city, mtype, date_iso, cache, allow_fetch)
        if len(cache) > before:
            fetches += 1
        if actual is None:
            continue
        samples.append((loc, scale, actual, lead_bucket(hours), city, mtype))

    # Forward source: the durable forecast-log (loc/scale per cycle), written by
    # build_fair_values.py. Keeps CRPS fed as old-format settlement rationales age out.
    FLOG = STATE / "forecast-log.jsonl"
    if FLOG.exists():
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone.utc).date().isoformat()
        seen = set()
        for line in open(FLOG):
            try:
                e = json.loads(line)
            except Exception:
                continue
            tp = parse_ticker(e.get("ticker", ""))
            loc, scale = e.get("loc"), e.get("scale")
            if not tp or loc is None or scale is None:
                continue
            city, mtype, date_iso = tp
            # Only score target days that are STRICTLY in the past — today's
            # daily high/low isn't final, so its archive "actual" is unreliable.
            if date_iso >= today:
                continue
            k = (e.get("ticker"), e.get("asof_utc"))
            if k in seen:
                continue
            seen.add(k)
            hours = None
            try:
                a = datetime.fromisoformat(e["asof_utc"].replace("Z", "+00:00"))
                # Kalshi weather markets close ~06:00Z the day AFTER the target date
                # (e.g. a 26JUN15 market closes 2026-06-16T05:59Z).
                close = datetime.fromisoformat(date_iso + "T06:00:00+00:00") + timedelta(days=1)
                hours = (close - a).total_seconds() / 3600.0
                if hours < 0:
                    continue
            except Exception:
                pass
            if args.limit and fetches >= args.limit and f"{city}|{mtype}|{date_iso}" not in cache:
                continue
            before = len(cache)
            actual = get_actual(city, mtype, date_iso, cache, allow_fetch)
            if len(cache) > before:
                fetches += 1
            if actual is None:
                continue
            samples.append((float(loc), float(scale), actual, lead_bucket(hours), city, mtype))

    try:
        json.dump(cache, open(ACTUALS_CACHE, "w"))
    except Exception:
        pass

    if not samples:
        print("No scorable samples (need cached/fetchable actuals + N(loc,scale) rationale).")
        return 1

    # Overall + by-bucket CRPS at the model's actual bandwidth (mult=1.0)
    def mean_crps(subset, mult=1.0):
        if not subset:
            return None
        return sum(crps_studentt(loc, scale * mult, y) for loc, scale, y, *_ in subset) / len(subset)

    print(f"CRPS report — {len(samples)} scored forecasts "
          f"(parsed N(loc,scale) rationales with fetched actuals)\n")
    print(f"Overall CRPS @ model bandwidth: {mean_crps(samples):.3f} °F  (lower=better)\n")

    print("By lead-time bucket:")
    by_lead = defaultdict(list)
    for s in samples:
        by_lead[s[3]].append(s)
    for k in ["0-6h", "6-12h", "12-18h", "18-24h", "24h+", "unknown"]:
        if by_lead[k]:
            print(f"  {k:>8}: n={len(by_lead[k]):>3}  CRPS={mean_crps(by_lead[k]):.3f}")

    # The sweep is run on the 12-24h TRADING band only. The aggregate is polluted
    # by 0-6h near-close forecasts where the model busts (CRPS≈3.5) and you want
    # MORE width, not less — but the bot doesn't trade that band. Optimal bandwidth
    # is lead-time-dependent; this answers "what width for the markets we trade?".
    band = [s for s in samples if s[3] in ("12-18h", "18-24h")]
    band = band or samples
    print(f"\nBANDWIDTH SWEEP on the 12-24h TRADING band (n={len(band)}) — CRPS-optimal width:")
    best = (None, 1e9)
    for mult in [0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
        c = mean_crps(band, mult)
        flag = ""
        if c < best[1]:
            best = (mult, c)
        print(f"  ×{mult:<4}: CRPS={c:.3f}")
    print(f"\n  → CRPS-optimal bandwidth factor ≈ ×{best[0]}  (CRPS={best[1]:.3f})")
    if best[0] < 1.0:
        print(f"  ⇒ Model is OVER-smoothing by ~{(1-best[0])*100:.0f}%. Narrowing the kernel "
              f"(lower KALSHI_WEATHER_SCALE_FLOOR / add KALSHI_WEATHER_SCALE_MULT≈{best[0]}) "
              f"should sharpen bin discrimination.")
    elif best[0] > 1.0:
        print(f"  ⇒ Model is UNDER-dispersed; widen the kernel by ~{(best[0]-1)*100:.0f}%.")
    else:
        print("  ⇒ Bandwidth is about right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

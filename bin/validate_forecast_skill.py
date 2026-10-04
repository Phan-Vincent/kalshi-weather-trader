#!/usr/bin/env python3
"""bin/validate_forecast_skill.py — Does the CURRENT forecast model beat CLIMATOLOGY?

Question: is the forecast MEAN a better point predictor of the realised daily high/low than
the 25-year climatological mean?  skill = 1 - forecast_rmse / climo_rmse  (>0 ⇒ forecast wins,
which is what justifies KALSHI_WEATHER_USE_FORECAST).

REWRITTEN 2026-07-03 (weather-model QA). The old implementation read state/settlement-log.jsonl
and parsed `N(loc, scale^2)` out of the rationale — a string ONLY the legacy Gaussian arm ever
emitted — and had four defects that made its headline "+61% skill" untrustworthy:

  1. WRONG MODEL — the current KDE model logs no `N(loc,scale^2)` rationale, so ZERO current-model
     records were scored; the number described a retired model on a stale, trade-only sample.
  2. WRONG LEAD — lead buckets were (market_close − opened), i.e. lead-to-close. For a daily-low
     that resolves at dawn but closes hours later, a post-resolution snapshot was mislabelled the
     "0-6h shortest lead" bucket.
  3. PSEUDO-REPLICATION — every intraday snapshot of the same market counted as an independent
     sample, inflating n and shrinking apparent variance.
  4. NO OUTLIER GUARD — a single corrupt daily-low record (loc=90 °F for a Houston LOW, ~5.8σ
     above and above the climo HIGH) mechanically inverted the 0-6h bucket to negative skill.

This version scores the CURRENT model from state/forecast-log.jsonl (mode='forecast'; loc =
model_temp_forecast = the raw ensemble mean), applying the same leak-free discipline as
bin/leakfree_skill.py:
  * leak-free window (asof_utc after the LEAK-1 guard became effective) + same-day station-local
    exclusion, so no elapsed-day observation is scored as a "forecast";
  * ONE forecast per settled EVENT (city, mtype, date) for the headline — the last leak-free
    forecast — so events are independent (no pseudo-replication); a seeded bootstrap gives an
    honest CI on the MSE reduction;
  * a 4σ-from-climatology outlier guard that drops corrupt loc values;
  * lead buckets measured as DAYS AHEAD of the target LOCAL day (deduped one-per-event-per-bucket).

Actuals come from the shared crps actuals cache (UTC-day aggregated — a known separate issue,
fix #4; consistent with every other scorer here). Read-only: uses cached actuals by default,
--fetch to fill gaps in memory, and NEVER writes the shared cache.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from crps_report import parse_ticker, get_actual, load_cache  # noqa: E402
from leakfree_skill import _asof_local_date, _DEFAULT_CUTOFF, FLOG  # noqa: E402
from data.weather_data import is_weather_ticker, CITY_ALIASES, STATION_TZ  # noqa: E402
from model.prior import Climatology  # noqa: E402

# A forecast loc this many climatological σ from the climo mean is treated as corrupt and dropped
# (the 2026-05-31 HOU daily-low loc=90 record sat ~5.8σ out). σ is estimated from the interdecile
# range; P90−P10 = 2.5631σ for a normal.
OUTLIER_SIGMA = 4.0
_IDR_TO_SIGMA = 1.0 / 2.5631
_SIGMA_FLOOR = 1.5  # never let a tight-climo day make the guard hair-trigger
_BUCKETS = [(0, 1, "0-1d"), (1, 2, "1-2d"), (2, 3, "2-3d"), (3, 1e9, "3d+")]


def _target_day_start_utc(date_iso: str, city: str) -> datetime:
    """UTC instant of 00:00 on the target STATION-LOCAL day — the lead reference. Using the local
    day (not the market close) keeps lead honest for daily-low markets that close after resolution."""
    tz = STATION_TZ.get(city)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return (datetime.fromisoformat(f"{date_iso}T00:00:00")
                    .replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc))
        except Exception:
            pass
    return datetime.fromisoformat(f"{date_iso}T00:00:00+00:00")


def _bucket(lead_h) -> str:
    if lead_h is None or lead_h < 0:
        return "unknown"
    d = lead_h / 24.0
    for lo, hi, name in _BUCKETS:
        if lo <= d < hi:
            return name
    return "unknown"


def collect(rows, actual_of, climo, cutoff_dt, today_iso):
    """Reduce forecast-log rows to deduped, leak-free, settled samples.

    sample = (city, mtype, date_iso, loc, actual, climo_mean, lead_h, bucket)
    Returns (per_event_samples, per_event_per_bucket_samples, dropped_counts)."""
    best_event: dict = {}   # (city,mtype,date)        -> (asof, sample)  — last leak-free forecast
    best_bucket: dict = {}  # (city,mtype,date,bucket)  -> (asof, sample)
    dropped: dict = defaultdict(int)

    for e in rows:
        if e.get("mode") != "forecast":
            continue
        tk = e.get("ticker", "")
        tp = parse_ticker(tk)
        if not tp or not is_weather_ticker(tk):
            continue
        city, mtype, date_iso = tp
        city = CITY_ALIASES.get(city, city)  # KXHIGHNY parses "NY"; canonicalize for tz + actuals
        loc = e.get("loc")
        if loc is None or not math.isfinite(loc):
            dropped["bad_loc"] += 1
            continue
        if date_iso >= today_iso:
            continue  # unsettled target day
        try:
            asof = datetime.fromisoformat(str(e.get("asof_utc")).replace("Z", "+00:00"))
        except Exception:
            continue
        if asof <= cutoff_dt:
            dropped["pre_window"] += 1
            continue
        if date_iso <= _asof_local_date(asof, city):  # elapsed day → the "forecast" saw the answer
            dropped["same_day_leak"] += 1
            continue
        mmdd = date_iso[5:10]
        cmean = climo.get_climo_mean(city, mmdd, mtype)
        if cmean is None:
            dropped["no_climo"] += 1
            continue
        sigma = max((climo.get_climo_spread(city, mmdd, mtype) or 0.0) * _IDR_TO_SIGMA, _SIGMA_FLOOR)
        if abs(loc - cmean) > OUTLIER_SIGMA * sigma:
            dropped["outlier_loc"] += 1
            continue
        actual = actual_of(city, mtype, date_iso)
        if actual is None:
            dropped["awaiting_actual"] += 1
            continue
        if not math.isfinite(actual):
            dropped["bad_actual"] += 1
            continue
        lead_h = (_target_day_start_utc(date_iso, city) - asof).total_seconds() / 3600.0
        s = (city, mtype, date_iso, float(loc), float(actual), float(cmean), lead_h, _bucket(lead_h))
        ek = (city, mtype, date_iso)
        if ek not in best_event or asof > best_event[ek][0]:
            best_event[ek] = (asof, s)
        bk = (city, mtype, date_iso, s[7])
        if bk not in best_bucket or asof > best_bucket[bk][0]:
            best_bucket[bk] = (asof, s)

    return ([s for _, s in best_event.values()],
            [s for _, s in best_bucket.values()], dict(dropped))


def _stats(samples):
    n = len(samples)
    if n == 0:
        return None
    f_se = [(loc - a) ** 2 for _, _, _, loc, a, _, _, _ in samples]
    c_se = [(cm - a) ** 2 for _, _, _, _, a, cm, _, _ in samples]
    f_mae = sum(abs(loc - a) for _, _, _, loc, a, _, _, _ in samples) / n
    c_mae = sum(abs(cm - a) for _, _, _, _, a, cm, _, _ in samples) / n
    f_rmse, c_rmse = math.sqrt(sum(f_se) / n), math.sqrt(sum(c_se) / n)
    return {"n": n, "f_mae": f_mae, "c_mae": c_mae, "f_rmse": f_rmse, "c_rmse": c_rmse,
            "skill": (1 - f_rmse / c_rmse) if c_rmse > 0 else 0.0}


def _bootstrap_mse_gain_ci(samples, iters=5000):
    """Percentile CI on mean(climo_se − forecast_se) — the per-event MSE reduction. >0 ⇒ the
    forecast genuinely reduces squared error. Events are already independent (one per event).
    Seeded for reproducibility (no wall-clock dependence)."""
    d = [(cm - a) ** 2 - (loc - a) ** 2 for _, _, _, loc, a, cm, _, _ in samples]
    n = len(d)
    if n < 3:
        return None
    rng = random.Random(1234)
    means = []
    for _ in range(iters):
        s = sum(d[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    return (sum(d) / n, means[int(0.025 * iters)], means[int(0.975 * iters)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="fetch missing actuals into memory (default: cached only; never writes cache)")
    ap.add_argument("--cutoff", default=None, help="override leak-free asof cutoff (ISO)")
    args = ap.parse_args()

    if not FLOG.exists():
        print("[forecast-skill] no forecast-log.jsonl yet")
        return 1
    climo = Climatology()
    climo.load(str(ROOT / "state" / "climatology.json"))
    cache = load_cache()
    cutoff_dt = datetime.fromisoformat(args.cutoff or _DEFAULT_CUTOFF)
    today_iso = datetime.now(timezone.utc).date().isoformat()

    def actual_of(city, mtype, date_iso):
        return get_actual(city, mtype, date_iso, cache, args.fetch)

    rows = []
    for line in open(FLOG):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    events, by_bucket, dropped = collect(rows, actual_of, climo, cutoff_dt, today_iso)
    overall = _stats(events)

    print("Forecast vs Climatology skill — CURRENT model (forecast-log, mode=forecast), "
          "leak-free, one forecast per event")
    print(f"  leak-free window: asof > {cutoff_dt.isoformat()}   |   scored through {today_iso}")
    print(f"  dropped: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())) + "\n")

    if not overall:
        awaiting = dropped.get("awaiting_actual", 0)
        print("No scorable events yet."
              + (f" ({awaiting} leak-free forecasts awaiting settled archive actuals ~6-7d lag)"
                 if awaiting else " Run bin/crps_report.py / leakfree_skill.py to warm the actuals cache."))
        return 0

    print(f"OVERALL (n={overall['n']} events):  forecast MAE={overall['f_mae']:.2f}°F  "
          f"climo MAE={overall['c_mae']:.2f}°F   |   forecast RMSE={overall['f_rmse']:.2f}  "
          f"climo RMSE={overall['c_rmse']:.2f}")
    print(f"          SKILL = 1 - {overall['f_rmse']:.2f}/{overall['c_rmse']:.2f} = "
          f"{overall['skill']:+.1%}  "
          f"({'forecast WINS' if overall['skill'] > 0 else 'climatology wins'})")
    ci = _bootstrap_mse_gain_ci(events)
    if ci:
        verdict = ("forecast skill REAL (CI > 0)" if ci[1] > 0 else
                   "climatology better (CI < 0)" if ci[2] < 0 else
                   "WITHIN NOISE (CI spans 0)")
        print(f"          MSE reduction (climo−fcst) per event: {ci[0]:+.2f} °F²  "
              f"95% CI [{ci[1]:+.2f}, {ci[2]:+.2f}]  →  {verdict}")

    print("\nBy lead bucket (days ahead of the target local day; one forecast per event per bucket):")
    grouped = defaultdict(list)
    for s in by_bucket:
        grouped[s[7]].append(s)
    for _, _, name in _BUCKETS:
        st = _stats(grouped.get(name, []))
        if st:
            print(f"  {name:>6}: n={st['n']:>4}  fcst RMSE={st['f_rmse']:.2f}  "
                  f"climo RMSE={st['c_rmse']:.2f}  skill={st['skill']:+.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

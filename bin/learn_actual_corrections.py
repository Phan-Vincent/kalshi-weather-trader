#!/usr/bin/env python3
"""bin/learn_actual_corrections.py — learn per-city grid→station actual corrections (audit 2026-07-06 #7).

Model scoring/calibration reads the Open-Meteo grid REANALYSIS as "the actual", but Kalshi settles
on the NWS CLI STATION sensor. reconcile_actuals showed the grid is systematically ~1.5°F colder
than the station on highs (66:20 cold:warm). Rather than switch the actuals SOURCE, we learn a
per-city, per-metric offset and add it to the grid at scoring time (fetch_actual_temp correction_city=).

How the station actual is estimated WITHOUT a station feed: for every settled EVENT (city+date+metric)
exactly one 2°F 'between' band settles YES — the one containing the station's realized extreme. Its
band CENTER (threshold_f) pins the station actual to ±1°F. We compare that to the RAW grid actual for
the same city/date and average (station_est − grid) per (city, metric):

    correction[city][metric] = mean_over_events( band_center − grid_actual )

Applied downstream as: station_consistent_actual = grid_actual + correction. This shifts the bias
EWMA (and thus the KDE price via city_bias_f), CRPS, and skill scoring onto the settlement truth.
The climatology needs NO correction — its absolute offset washes out when the KDE recenters.

Writes state/actual-corrections.json. Requires >= MIN_EVENTS per (city, metric); clamps to ±MAX_ABS.
Read-only w.r.t. trading; fetches raw grid actuals (persistent cache at state/actuals-cache.json).

Usage:  python3 bin/learn_actual_corrections.py [--live-dir state/paper] [--json] [--min N]
"""
import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import STATIONS               # noqa: E402
from model.error_tracker import fetch_actual_temp     # noqa: E402
from reconcile_actuals import _read_jsonl, _parsed_for  # noqa: E402  (shared parse)

CORRECTIONS_PATH = ROOT / "state" / "actual-corrections.json"
CACHE_PATH = ROOT / "state" / "actuals-cache.json"
MIN_EVENTS_DEFAULT = 4
MAX_ABS_CORRECTION_F = 5.0
# Small-sample shrinkage: the stored correction is raw_mean * n/(n+SHRINK_K), so a noisy
# few-event estimate is damped toward 0 (a wrong correction shifts the live price directly).
# n=4→0.5, n=8→0.67, n=20→0.83; approaches the full correction as events accumulate.
SHRINK_K = 4.0


def _grid_cached(city: str, date_iso: str, mtype: str, cache: dict) -> Optional[float]:
    meta = STATIONS.get(city)
    if not meta:
        return None
    key = f"{city}|{date_iso}|{mtype}"
    if key in cache:
        return cache[key]
    # RAW grid (no correction_city) — the learner must never see a corrected value or it
    # would learn the residual and the offset would collapse toward 0 over re-runs.
    v = fetch_actual_temp(meta["lat"], meta["lon"], date_iso, mtype)
    cache[key] = v
    time.sleep(0.15)
    return v


def learn(live_dir: Path, min_events: int) -> dict:
    rows = _read_jsonl(live_dir / "settlement-log.jsonl")
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    # One (station_est) per event from the YES 'between' band center.
    by_event: dict = {}
    for r in rows:
        if r.get("settlement_result") != "yes":
            continue
        p = _parsed_for(r)
        if p.get("bin_kind") != "between":
            continue
        city, date_iso, mtype = p.get("city_code"), p.get("date_iso"), p.get("market_type")
        thr = p.get("threshold_f")
        if not (city in STATIONS and date_iso and mtype in ("daily_high", "daily_low") and thr is not None):
            continue
        by_event[(city, date_iso, mtype)] = float(thr)  # band center = station actual ±1°F

    deltas: dict = defaultdict(list)
    for (city, date_iso, mtype), station_est in by_event.items():
        grid = _grid_cached(city, date_iso, mtype, cache)
        if grid is None:
            continue
        deltas[(city, mtype)].append(station_est - grid)

    CACHE_PATH.write_text(json.dumps(cache))

    corrections: dict = defaultdict(dict)
    skipped = []
    for (city, mtype), ds in sorted(deltas.items()):
        if len(ds) < min_events:
            skipped.append({"city": city, "mtype": mtype, "n": len(ds), "reason": "below_min_events"})
            continue
        raw = statistics.mean(ds)
        shrunk = raw * (len(ds) / (len(ds) + SHRINK_K))  # damp small-sample noise toward 0
        clamped = max(-MAX_ABS_CORRECTION_F, min(MAX_ABS_CORRECTION_F, shrunk))
        corrections[city][mtype] = {
            "correction_f": round(clamped, 2),   # ready to ADD to the grid actual at scoring time
            "raw_mean_f": round(raw, 2),
            "n_events": len(ds),
            "std_f": round(statistics.pstdev(ds), 2) if len(ds) > 1 else 0.0,
        }
    return {
        "generated_from": str(live_dir),
        "min_events": min_events,
        "max_abs_correction_f": MAX_ABS_CORRECTION_F,
        "note": "station_consistent_actual = grid_actual + correction_f. Learned from YES 2°F band centers.",
        "corrections": {c: v for c, v in corrections.items()},
        "skipped": skipped,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-dir", default="state/paper")
    ap.add_argument("--min", type=int, default=MIN_EVENTS_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write the corrections file")
    args = ap.parse_args()
    live_dir = ROOT / args.live_dir if not Path(args.live_dir).is_absolute() else Path(args.live_dir)
    res = learn(live_dir, args.min)
    if not args.dry_run:
        tmp = CORRECTIONS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, indent=2))
        tmp.replace(CORRECTIONS_PATH)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"\nPER-CITY GRID→STATION ACTUAL CORRECTIONS (from {res['generated_from']})")
        print("=" * 74)
        print(f"  {'city':6} {'metric':11} {'correction':>11} {'raw':>7} {'n':>4} {'std':>6}")
        for city, mm in sorted(res["corrections"].items()):
            for mtype, d in sorted(mm.items()):
                print(f"  {city:6} {mtype:11} {d['correction_f']:>+10.2f}°F {d['raw_mean_f']:>+6.1f} "
                      f"{d['n_events']:>4} {d['std_f']:>6.1f}")
        print(f"\n  {'(not written — dry run)' if args.dry_run else 'wrote ' + str(CORRECTIONS_PATH)}"
              f"   {len(res['skipped'])} (city,metric) skipped for < {args.min} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())

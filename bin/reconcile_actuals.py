#!/usr/bin/env python3
"""
bin/reconcile_actuals.py — Reconcile MODEL-truth vs SETTLEMENT-truth (audit 2026-07-06).

Every model scorer (bias EWMA, CRPS, Brier/shadow skill, forecast gate) settles against
the Open-Meteo grid-cell REANALYSIS at the station lat/lon, while Kalshi settles P&L on
the NWS STATION sensor. Grid cells differ from the airport/park sensor by 1-3+°F for
coastal cities — but the two sources were never reconciled per-market, even though both
are already on disk. This read-only tool closes that gap in decision-relevant terms:

For each settled market it fetches OUR grid actual, buckets it through the SAME outcome
convention the model scores with (shadow_score.outcome), and compares the implied YES/NO
to Kalshi's actual settlement. A mismatch means our grid actual would have MIS-SCORED
that market — i.e. calibration/skill gates trained on a different truth than the money.
It also bounds the divergence: on a mismatch, Kalshi's true extreme is on the far side
of the strike, so |grid − strike| is a floor on the grid-vs-station error that day.

Usage:
  python3 bin/reconcile_actuals.py                       # state/paper, human report
  python3 bin/reconcile_actuals.py --live-dir state/live-premium
  python3 bin/reconcile_actuals.py --json                # machine-readable

Read-only: fetches archive actuals (cached per run), writes nothing.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import STATIONS, parse_market_ticker  # noqa: E402
from model.error_tracker import fetch_actual_temp            # noqa: E402
from shadow_score import outcome                             # noqa: E402


def _read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _parsed_for(row: dict) -> dict:
    """Prefer the settlement-time snapshot (correct T-direction); fall back to ticker parse."""
    mp = row.get("market_parsed")
    if mp and mp.get("bin_kind"):
        return mp
    try:
        return parse_market_ticker(row.get("ticker", ""))
    except Exception:
        return {}


def reconcile_row(row: dict, cache: dict) -> Optional[dict]:
    """Return a per-market reconciliation record, or None if not evaluable."""
    ticker = row.get("ticker", "")
    p = _parsed_for(row)
    if not p:
        return None
    city = p.get("city_code")
    date_iso = p.get("date_iso")
    mtype = p.get("market_type")
    if mtype not in ("daily_high", "daily_low"):
        return None  # hourly markets settle on a different grid field; skip
    meta = STATIONS.get(city)
    if not meta or not date_iso:
        return None
    settlement = row.get("settlement_result")
    if settlement not in ("yes", "no"):
        return None

    key = (city, date_iso, mtype)
    if key not in cache:
        cache[key] = fetch_actual_temp(meta["lat"], meta["lon"], date_iso, mtype)
    grid = cache[key]
    if grid is None:
        return None

    thr = p.get("threshold_f")
    our_yes = outcome(p.get("bin_kind"), thr, p.get("bin_low"), p.get("bin_high"), grid)
    if our_yes is None:
        return None
    kal_yes = 1 if settlement == "yes" else 0
    match = (our_yes == kal_yes)
    # On a mismatch, Kalshi's true extreme is on the OTHER side of the crossed boundary from
    # our grid value, so the distance to the NEAREST relevant boundary lower-bounds the
    # grid-vs-station error that day. For between-markets that's the nearest of (lo, hi); for
    # above/below it's the single threshold.
    strike_gap = 0.0
    if not match:
        lo, hi = p.get("bin_low"), p.get("bin_high")
        bounds = [b for b in (thr, lo, hi) if isinstance(b, (int, float))]
        if bounds:
            strike_gap = min(abs(grid - b) for b in bounds)
    return {
        "ticker": ticker, "city": city, "date": date_iso, "mtype": mtype,
        "grid_actual": grid, "strike": thr, "bin_kind": p.get("bin_kind"),
        "our_yes": our_yes, "kalshi_yes": kal_yes, "match": match,
        "min_grid_vs_station_err": round(strike_gap, 2),
    }


def analyze(live_dir: Path) -> dict:
    rows = _read_jsonl(live_dir / "settlement-log.jsonl")
    cache: dict = {}
    recs = [r for r in (reconcile_row(row, cache) for row in rows) if r]
    by_city: dict = defaultdict(lambda: {"n": 0, "mismatch": 0, "gaps": []})
    for r in recs:
        c = by_city[r["city"]]
        c["n"] += 1
        if not r["match"]:
            c["mismatch"] += 1
            c["gaps"].append(r["min_grid_vs_station_err"])
    cities = []
    for city, c in sorted(by_city.items(), key=lambda kv: -(kv[1]["mismatch"] / max(kv[1]["n"], 1))):
        worst = max(c["gaps"]) if c["gaps"] else 0.0
        cities.append({
            "city": city, "n": c["n"], "mismatch": c["mismatch"],
            "mismatch_rate": round(c["mismatch"] / c["n"], 3) if c["n"] else 0.0,
            "worst_min_err_f": round(worst, 2),
        })
    n = len(recs)
    mism = sum(1 for r in recs if not r["match"])
    return {
        "live_dir": str(live_dir), "n_evaluated": n, "n_mismatch": mism,
        "mismatch_rate": round(mism / n, 3) if n else None,
        "by_city": cities,
        "worst_markets": sorted([r for r in recs if not r["match"]],
                                key=lambda r: -r["min_grid_vs_station_err"])[:10],
    }


def render(a: dict) -> str:
    L = [f"\nACTUALS RECONCILIATION — {a['live_dir']} (grid model-truth vs Kalshi settlement-truth)",
         "=" * 82]
    if not a["n_evaluated"]:
        L.append("  (no evaluable settled markets yet)")
        return "\n".join(L)
    L.append(f"OVERALL: {a['n_mismatch']}/{a['n_evaluated']} markets where our grid actual "
             f"would MIS-SCORE vs Kalshi ({a['mismatch_rate']*100:.1f}%)")
    L.append("\nBY CITY (worst mismatch-rate first):")
    L.append(f"  {'city':6} {'n':>4} {'mismatch':>9} {'rate':>7} {'worst≥°F':>9}")
    for c in a["by_city"]:
        L.append(f"  {c['city']:6} {c['n']:>4} {c['mismatch']:>9} {c['mismatch_rate']*100:>6.1f}% "
                 f"{c['worst_min_err_f']:>9.1f}")
    if a["worst_markets"]:
        L.append("\nWORST MARKETS (grid on the wrong side of the strike by ≥°F):")
        for r in a["worst_markets"]:
            L.append(f"  {r['ticker']:28} grid={r['grid_actual']:.1f}°F strike={r['strike']} "
                     f"{r['bin_kind']:>7} ours={'Y' if r['our_yes'] else 'N'} "
                     f"kalshi={'Y' if r['kalshi_yes'] else 'N'}  ≥{r['min_grid_vs_station_err']:.1f}°F off")
    L.append("\n(A high per-city mismatch-rate ⇒ the grid cell is a poor proxy for that station's "
             "sensor; consider re-siting the lat/lon or a station-obs source for scoring.)")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-dir", default="state/paper")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    a = analyze(ROOT / args.live_dir if not Path(args.live_dir).is_absolute() else Path(args.live_dir))
    print(json.dumps(a, indent=2) if args.json else render(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())

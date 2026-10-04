#!/usr/bin/env python3
"""bin/rain_firewall_backtest.py — QUICK, LEAK-FREE firewall backtest for the monthly-rain kill screen.

The forward screen (rain_screen_snapshot.py) needs ~12 months to read out. This shortens the KILL case:
the model must first clear the FIREWALL — beat plain climatology at month-open lead — and that is a
model-vs-climatology question with NO market prices, so it is backtestable on history TODAY.

WHY NOT the obvious quick path: the Open-Meteo historical-forecast API stitches ~1-day-lead forecasts
(verified 2026-07-14: its Jul-2024 month totals were 12-48% off the gauge from grid basis AND effectively
"knew" the month as it unfolded), so a backtest on it FAKES month-open skill. Invalid.

WHAT THIS DOES INSTEAD (leak-free, immediate, from station history only): over ~26 years × the 11
KXRAIN* cities, it asks whether the information available AT MONTH-OPEN beyond raw climatology —
persistence (last month's standardized anomaly) and ENSO (prior-season ONI) — improves the probability
of each "monthly total > N inches" bin, scored leave-one-out against the ACIS GAUGE the market settles on.
Firewall = Brier(climatology) − Brier(conditioned); event-clustered (city+month+year) BCa CI. Also reports
the out-of-sample R² of predicting the monthly anomaly.

SCOPE / HONESTY: this tests the INTER-ANNUAL + persistence predictable component. It does NOT capture the
within-month synoptic skill a real NWP ensemble adds in the first ~1-2 weeks — that needs a proper
month-open REFORECAST (GEFSv12), the rigorous path. But that synoptic contribution is small for a MONTHLY
total and near-zero at month-open lead in the literature (CPC monthly-precip Heidke ~12-22%), so:
  • firewall ≤ 0 here → combined with the near-zero published NWP month-open skill, a strong KILL of the
    model side now — no 12-month wait.
  • firewall > 0 here → there IS leak-free monthly predictability; escalate to the GEFSv12 reforecast test.
Read-only. Sources: ACIS StnData (gauge precip), CPC ONI.

Usage: python3 bin/rain_firewall_backtest.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import cluster_bootstrap_ci  # noqa: E402
from rain_screen_snapshot import CITY_COORDS  # noqa: E402  (city → ACIS station id)

ACIS = "https://data.rcc-acis.org/StnData"
ONI_URLS = ["https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
            "https://origin.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"]
START_YEAR = 2000
THRESHOLDS = [0.5, 1, 2, 3, 4, 5, 6, 7, 8]   # the KXRAIN "> N inches" ladder
MIN_YEARS = 12
# ONI 3-month seasons in order → the LAST calendar month each covers (NDJ spills to Jan of next year=13).
_SEAS_LAST = {"DJF": 2, "JFM": 3, "FMA": 4, "MAM": 5, "AMJ": 6, "MJJ": 7,
              "JJA": 8, "JAS": 9, "ASO": 10, "SON": 11, "OND": 12, "NDJ": 13}


# ── pure helpers (unit-tested) ───────────────────────────────────────────────────────────────────

def prior_month(year: int, month: int):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def prob_above(threshold: float, totals: list[float], shift: float = 0.0) -> float:
    """Empirical P(total + shift > threshold) over the (leave-one-out) climatology sample."""
    if not totals:
        return 0.5
    return sum(1 for t in totals if (t + shift) > threshold) / len(totals)


def loo_predict_anomaly(rows: list[tuple], i: int) -> float:
    """rows = [(anom, prior_anom, oni)]. OLS of anom ~ prior_anom + oni fit on all rows except i, then
    predict row i's anomaly. Falls back to 0.0 (climatology) if the fit is degenerate/unavailable."""
    import numpy as np
    tr = [r for j, r in enumerate(rows) if j != i]
    if len(tr) < 6:
        return 0.0
    X = np.array([[1.0, r[1], r[2]] for r in tr])
    y = np.array([r[0] for r in tr])
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        return float(np.array([1.0, rows[i][1], rows[i][2]]) @ beta)
    except Exception:
        return 0.0


# ── data ─────────────────────────────────────────────────────────────────────────────────────────

def _acis_monthly(sid: str) -> dict:
    body = json.dumps({"sid": sid, "sdate": f"{START_YEAR}-01-01", "edate": "2025-12-31",
                       "elems": [{"name": "pcpn", "interval": "mly", "reduce": "sum"}]}).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(ACIS, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "kwb-firewall/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return {}
        except Exception:
            time.sleep(1.0); continue
    else:
        return {}
    out = {}
    for t, v in d.get("data", []):
        if v in ("M", None) or v == "T":
            v = 0.0 if v == "T" else None
        if v is not None:
            try:
                y, m = t.split("-")
                out[(int(y), int(m))] = float(v)
            except Exception:
                continue
    return out


def _fetch_oni() -> dict:
    """{(year, last_month): oni_value}. Keyed by the season's LAST calendar month so month-open lookup is
    exact — for target month M, the newest season fully in the past ends at M-1."""
    for url in ONI_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kwb-firewall/1.0"})
            txt = urllib.request.urlopen(req, timeout=30).read().decode()
        except Exception:
            continue
        oni = {}
        for line in txt.strip().splitlines()[1:]:
            p = line.split()
            if len(p) < 4 or p[0] not in _SEAS_LAST:
                continue
            last = _SEAS_LAST[p[0]]
            yr = int(p[1])
            if last == 13:
                last, yr = 1, yr + 1
            try:
                oni[(yr, last)] = float(p[3])
            except Exception:
                continue
        if oni:
            return oni
    return {}


# ── backtest ─────────────────────────────────────────────────────────────────────────────────────

def backtest_city_month(totals_cm: dict, monthly: dict, oni: dict, city: str, month: int):
    """totals_cm = {year: total} for one calendar month. Returns per-bin Brier items for CLIM vs COND."""
    years = sorted(totals_cm)
    rows, ylist = [], []
    for y in years:
        pm = prior_month(y, month)
        pa = monthly.get(pm)
        o = oni.get((y, month - 1)) if month > 1 else oni.get((y - 1, 12))
        if pa is None or o is None:
            continue
        # standardize the prior-month anomaly by that month's own climatology
        pms = [monthly[(yy, pm[1])] for yy in range(START_YEAR, 2026) if (yy, pm[1]) in monthly]
        if len(pms) < MIN_YEARS:
            continue
        mu = sum(pms) / len(pms)
        sd = (sum((x - mu) ** 2 for x in pms) / len(pms)) ** 0.5 or 1.0
        rows.append((totals_cm[y], (pa - mu) / sd, o))   # (total, std_prior_anom, oni)
        ylist.append(y)
    if len(rows) < MIN_YEARS:
        return [], []
    clim_mean = sum(r[0] for r in rows) / len(rows)
    anom_rows = [(r[0] - clim_mean, r[1], r[2]) for r in rows]   # regress the ANOMALY
    clim_items, cond_items, oos = [], [], []
    for i, (y, row) in enumerate(zip(ylist, rows)):
        others = [rows[j][0] for j in range(len(rows)) if j != i]        # LOO climatology sample
        pred = loo_predict_anomaly(anom_rows, i)                          # conditioned anomaly (LOO)
        oos.append((anom_rows[i][0], pred))
        ev = f"{city}-{month:02d}-{y}"
        for N in THRESHOLDS:
            p_cl = prob_above(N, others)
            if not (0.05 <= p_cl <= 0.95):     # skip climatology-decided (no tradeable uncertainty) bins
                continue
            p_co = prob_above(N, others, shift=pred)
            outcome = 1.0 if row[0] > N else 0.0
            clim_items.append((ev, (p_cl - outcome) ** 2))
            cond_items.append((ev, (p_co - outcome) ** 2))
    return clim_items, cond_items, oos


def run(cities=None):
    oni = _fetch_oni()
    if not oni:
        return {"error": "could not fetch ONI"}
    cities = cities or list(CITY_COORDS)
    clim_all, cond_all, oos_all = [], [], []
    per_city = {}
    for city in cities:
        sid = CITY_COORDS[city][3]
        monthly = _acis_monthly(sid)
        time.sleep(0.3)
        if not monthly:
            continue
        c_clim, c_cond = [], []
        for month in range(1, 13):
            cm = {y: monthly[(y, month)] for y in range(START_YEAR, 2026) if (y, month) in monthly}
            r = backtest_city_month(cm, monthly, oni, city, month)
            if r and r[0]:
                c_clim += r[0]; c_cond += r[1]; oos_all += r[2]
        clim_all += c_clim; cond_all += c_cond
        if c_clim:
            per_city[city] = {"n_bins": len(c_clim),
                              "brier_clim": _mean(c_clim), "brier_cond": _mean(c_cond)}
    # firewall per-bin: clim_brier − cond_brier (paired by index; same (event, threshold) order)
    fw = [(clim_all[i][0], clim_all[i][1] - cond_all[i][1]) for i in range(len(clim_all))]
    import math
    ss_clim = sum(a * a for a, _ in oos_all)
    ss_cond = sum((a - p) ** 2 for a, p in oos_all)
    oos_r2 = (1 - ss_cond / ss_clim) if ss_clim > 0 else None
    return {
        "n_events": len({ev for ev, _ in clim_all}), "n_bins": len(clim_all), "n_cities": len(per_city),
        "brier_clim": _mean(clim_all), "brier_cond": _mean(cond_all),
        "firewall_ci": cluster_bootstrap_ci(fw) if fw else (None, None, None),
        "oos_r2_anomaly": oos_r2, "per_city": per_city,
        "verdict": _verdict(fw, oos_r2),
    }


def _mean(items):
    return sum(v for _, v in items) / len(items) if items else None


def _verdict(fw_items, oos_r2) -> str:
    if not fw_items:
        return "NO DATA — no live (uncertain) bins scored"
    ci = cluster_bootstrap_ci(fw_items)
    pt, lo, hi = ci
    if lo is not None and lo > 0:
        return (f"PREDICTABILITY FOUND — leak-free month-open predictors beat climatology (firewall "
                f"{pt:+.4f} [{lo:+.4f},{hi:+.4f}], OOS R² {oos_r2:+.3f}). The screen is NOT dead on priors; "
                f"escalate to the GEFSv12 reforecast test for the full NWP firewall.")
    tag = "and OOS anomaly R² ≤ 0" if (oos_r2 is not None and oos_r2 <= 0) else ""
    return (f"KILL-SUPPORTING — no leak-free month-open predictability beyond climatology (firewall "
            f"{pt:+.4f} [{lo:+.4f},{hi:+.4f}] spans/≤0 {tag}, OOS R² {oos_r2:+.3f}). Persistence+ENSO add "
            f"nothing; combined with the published near-zero NWP month-open monthly-precip skill, the "
            f"model side is very unlikely to clear the firewall — a KILL basis now, without the 12-mo wait. "
            f"(Caveat: does not test within-month synoptic skill; GEFSv12 reforecast would close that gap.)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Quick leak-free firewall backtest for the rain kill screen")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--city", help="single city (default: all)")
    args = ap.parse_args()
    r = run([args.city] if args.city else None)
    if "error" in r:
        print(f"[rain-firewall] {r['error']}"); return 1
    if args.json:
        print(json.dumps(r, default=str)); return 0

    def ci(c): return "n/a" if not c or c[0] is None else f"{c[0]:+.4f} [{c[1]:+.4f}, {c[2]:+.4f}]"
    print(f"[rain-firewall] LEAK-FREE month-open predictability — {r['n_bins']} live bins over "
          f"{r['n_events']} city-months, {r['n_cities']} cities (~{2026-START_YEAR}yr history)")
    print(f"  Brier: climatology={r['brier_clim']:.4f}  persistence+ENSO conditioned={r['brier_cond']:.4f}")
    print(f"  firewall (clim_b − cond_b, >0 ⇒ predictors beat climatology): {ci(r['firewall_ci'])}")
    print(f"  OOS R² of monthly-anomaly prediction: {r['oos_r2_anomaly']:+.3f} "
          f"({'≤0 → no skill' if (r['oos_r2_anomaly'] or 0) <= 0 else 'positive'})")
    print(f"\n  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

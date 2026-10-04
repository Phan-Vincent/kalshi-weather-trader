#!/usr/bin/env python3
"""bin/rain_screen_snapshot.py — ROADMAP monthly-rain KILL SCREEN (the only residual weather probe).

Context: the catalog scan (2026-07-14) found Kalshi weather picked clean — every longer-horizon family
fails the same triad (accumulation-leak + public-consensus-priced + untestable in useful time). Monthly
city rain (KXRAIN*M) is the ONE family worth a single 12-month zero-capital falsification screen, because
(a) it powers fastest (~11 cities × 12 mo/yr) and (b) its leak is cleanly instrumentable: the published
banked month-to-date lets us decompose skill(FULL) − skill(BANKED-ONLY) and prove prospectively whether
any apparent skill is a real remaining-precip FORECAST or mere outcome observation — the daily-bin
leak-audit discipline applied forward. Expected outcome: death.

WHAT THIS DOES (forward data collection — must run daily so the 12-month clock starts now): for each
KXRAIN*M market it writes ONE frozen, timestamped snapshot per bin with three probabilities for the
"total monthly precip > N inches" question:
  • MARKET       — Kalshi mid (and bid/ask/spread) for the bin.
  • FULL         — P(banked + REMAINING crosses N) with the remaining-month distribution from a GEFS
                   ensemble (climatology tail beyond the ~16-day forecast horizon).
  • BANKED-ONLY  — same, but the remaining month uses CLIMATOLOGY only (no forecast). The FULL−BANKED
                   gap is the forecast-contribution firewall: if FULL≈BANKED, any "skill" is observation.
Nothing post-snapshot can enter a frozen prob. The load-bearing LEAK-FREE metric is the MONTH-OPEN
snapshot (≥25 days remaining, ~nothing banked); mid-month/near-close snapshots are logged but flagged
leak-exposed and are NOT eligible for the success test. Scoring (Brier/log-loss, the firewall, taker
PnL, month-clustered CIs) is a separate read-out once settlements accrue — see bin/rain_screen_score.py.

Read-only w.r.t. trading (no orders, no capital). Precip via Open-Meteo (archive + ensemble); Kalshi
market data via the public API. Defensive: a per-city fetch failure skips that city, never crashes.

Usage:
    python3 bin/rain_screen_snapshot.py            # snapshot all cities, append to state/rain-screen/
    python3 bin/rain_screen_snapshot.py --dry-run  # print, don't write
    python3 bin/rain_screen_snapshot.py --city HOU
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "rain-screen"
LOG = STATE / "snapshots.jsonl"

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OM_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
ACIS = "https://data.rcc-acis.org/StnData"
ENSEMBLE_MODEL = "gfs_seamless"   # GEFS (31 members); ECMWF ifs025 also available if wanted
ENSEMBLE_HORIZON_DAYS = 16
CLIM_YEARS = 15
LEAKFREE_MIN_REMAINING = 25       # ≥25 days remaining ⇒ the leak-free "month-open" snapshot

# KXRAIN*M cities → (lat, lon, tz, ACIS station id). CRITICAL: banked + climatology come from the ACIS
# GAUGE station the market actually settles on (from each series' rules: "total precipitation at CLIxxx")
# — NOT gridded model precip, which carries multi-inch basis vs the point gauge (verified 2026-07-14:
# Open-Meteo grid Chicago = 2.1in vs the CLIMDW gauge = 3.1in for the same MTD). lat/lon are only used
# for the Open-Meteo ENSEMBLE forecast, which is then bias-corrected into gauge space. Stations verified
# against rules_primary: CHI=Midway (CLIMDW, not O'Hare), DAL=DFW, STP=St Petersburg (CLISPG), NYC=Central Park.
CITY_COORDS = {
    "HOU": (29.645, -95.279, "America/Chicago", "KHOU"),
    "CHI": (41.786, -87.752, "America/Chicago", "KMDW"),
    "AUS": (30.194, -97.670, "America/Chicago", "KAUS"),
    "DAL": (32.8998, -97.0403, "America/Chicago", "KDFW"),
    "NYC": (40.779, -73.969, "America/New_York", "KNYC"),
    "MIA": (25.795, -80.287, "America/New_York", "KMIA"),
    "STP": (27.762, -82.627, "America/New_York", "KSPG"),
    "LAX": (33.942, -118.408, "America/Los_Angeles", "KLAX"),
    "SFO": (37.620, -122.365, "America/Los_Angeles", "KSFO"),
    "SEA": (47.444, -122.314, "America/Los_Angeles", "KSEA"),
    "DEN": (39.856, -104.673, "America/Denver", "KDEN"),
}


def _get(url: str, tries: int = 4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kwb-rainscreen/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0); continue
    return None


# ── pure helpers (unit-tested) ───────────────────────────────────────────────────────────────────

def parse_rain_bin(m: dict):
    """A KXRAIN*M market → (threshold_inches, ticker) for the 'total monthly precip > threshold' question,
    or None if it isn't a simple greater-than bin."""
    if m.get("strike_type") != "greater":
        return None
    fl = m.get("floor_strike")
    if fl is None:
        return None
    try:
        return float(fl), m.get("ticker", "")
    except Exception:
        return None


def month_bounds(today: date):
    """(days_elapsed_incl_today, days_remaining_after_today, [remaining date objs])."""
    last = calendar.monthrange(today.year, today.month)[1]
    rem = [date(today.year, today.month, d) for d in range(today.day + 1, last + 1)]
    return today.day, len(rem), rem


def prob_above(threshold: float, banked: float, remaining_samples: list[float]) -> float | None:
    """P(banked + remaining > threshold) as the fraction of the remaining-month distribution that crosses."""
    if not remaining_samples:
        return None
    hits = sum(1 for s in remaining_samples if (banked + s) > threshold)
    return hits / len(remaining_samples)


def full_remaining_samples(ens_within: list[float], clim_beyond: list[float]) -> list[float]:
    """Combine ensemble within-horizon member sums with the climatology beyond-horizon tail
    (deterministic index-pairing, so a frozen snapshot is reproducible). If the ensemble already covers
    the whole remaining month, clim_beyond is empty and the members ARE the distribution."""
    if not ens_within:
        return list(clim_beyond)
    if not clim_beyond:
        return list(ens_within)
    n = max(len(ens_within), len(clim_beyond))
    return [ens_within[i % len(ens_within)] + clim_beyond[i % len(clim_beyond)] for i in range(n)]


# ── network fetchers ─────────────────────────────────────────────────────────────────────────────

def _acis_daily(sid: str, sdate: str, edate: str) -> dict:
    """{date_iso: inches} of official GAUGE daily precip from ACIS (the NWS lineage Kalshi settles on).
    'T' (trace) → 0.0; 'M' (missing) → skipped. One call covers current-MTD + multi-year climatology."""
    body = json.dumps({"sid": sid, "sdate": sdate, "edate": edate,
                       "elems": [{"name": "pcpn", "interval": "dly"}]}).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(ACIS, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "kwb-rainscreen/1.0"})
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
        if v in ("M", None):
            continue
        out[t] = 0.0 if v == "T" else (float(v) if _isnum(v) else None)
    return {k: v for k, v in out.items() if v is not None}


def _isnum(x) -> bool:
    try:
        float(x); return True
    except Exception:
        return False


def _logged_today(now: datetime) -> bool:
    """True if snapshots.jsonl already has a record whose ts_utc is today (UTC) — cheap tail read."""
    if not LOG.exists():
        return False
    today = now.date().isoformat()
    try:
        with open(LOG, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 4096))   # last ~4KB is enough for one run's tail
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return False
    for line in reversed(tail.splitlines()):
        try:
            if json.loads(line).get("ts_utc", "").startswith(today):
                return True
        except Exception:
            continue
    return False


def _sum_over(daily: dict, md_set: set, year: int | None = None) -> float:
    tot = 0.0
    for t, v in daily.items():
        dt = date.fromisoformat(t)
        if (dt.month, dt.day) in md_set and (year is None or dt.year == year):
            tot += v
    return tot


def _per_year_sums(daily: dict, md_set: set, years: list[int]) -> list[float]:
    return [round(_sum_over(daily, md_set, y), 4) for y in years]


def fetch_gauge(sid: str, today: date, remaining: list[date], within: list[date], beyond: list[date]):
    """One ACIS call → banked-MTD (gauge) + per-year gauge climatology for remaining/within/beyond days."""
    daily = _acis_daily(sid, date(today.year - CLIM_YEARS, 1, 1).isoformat(), today.isoformat())
    if not daily:
        return None
    banked = round(_sum_over(daily, {(today.month, d) for d in range(1, today.day + 1)}, today.year), 4)
    years = list(range(today.year - CLIM_YEARS, today.year))
    return {
        "banked": banked,
        "clim_remaining": _per_year_sums(daily, {(d.month, d.day) for d in remaining}, years),
        "clim_within": _per_year_sums(daily, {(d.month, d.day) for d in within}, years) if within else [],
        "clim_beyond": _per_year_sums(daily, {(d.month, d.day) for d in beyond}, years) if beyond else [],
    }


def fetch_ensemble_within(lat, lon, tz, today: date, within: list[date]) -> list[float]:
    """Per-member GEFS precip sum (GRID space) over the ensemble-covered remaining days."""
    if not within:
        return []
    fdays = (within[-1] - today).days + 2
    q = (f"{OM_ENSEMBLE}?latitude={lat}&longitude={lon}&daily=precipitation_sum&models={ENSEMBLE_MODEL}"
         f"&timezone={urllib.parse.quote(tz)}&precipitation_unit=inch&forecast_days={min(16, max(1, fdays))}")
    d = _get(q)
    if not d:
        return []
    dd = d.get("daily", {})
    want = {x.isoformat() for x in within}
    idx = [i for i, t in enumerate(dd.get("time", [])) if t in want]
    return [sum((dd[mk][i] or 0.0) for i in idx if i < len(dd[mk]))
            for mk in dd if mk.startswith("precipitation_sum")]


def fetch_grid_clim_within(lat, lon, tz, within: list[date], years: list[int]) -> list[float]:
    """Per-year GRID (Open-Meteo archive) precip over the within-horizon days — the denominator of the
    grid→gauge bias ratio that maps the grid ensemble into gauge space."""
    if not within:
        return []
    start = date(years[0], 1, 1).isoformat()
    end = date(years[-1], 12, 31).isoformat()
    d = _get(f"{OM_ARCHIVE}?latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
             f"&daily=precipitation_sum&timezone={urllib.parse.quote(tz)}&precipitation_unit=inch")
    if not d:
        return []
    dd = d.get("daily", {})
    daily = {t: (v or 0.0) for t, v in zip(dd.get("time", []), dd.get("precipitation_sum", []))}
    return _per_year_sums(daily, {(x.month, x.day) for x in within}, years)


def _bias_ratio(gauge_within: list[float], grid_within: list[float]) -> float:
    """gauge/grid mean ratio over the within-horizon climatology; 1.0 if undefined. Maps a grid ensemble
    member's remaining-precip into the gauge space the market settles on."""
    if not gauge_within or not grid_within:
        return 1.0
    g = sum(grid_within) / len(grid_within)
    if g <= 0:
        return 1.0
    return (sum(gauge_within) / len(gauge_within)) / g


def snapshot_city(city: str, now: datetime):
    coords = CITY_COORDS.get(city)
    if not coords:
        return []
    lat, lon, tz, sid = coords
    from zoneinfo import ZoneInfo
    today = now.astimezone(ZoneInfo(tz)).date()
    days_elapsed, days_remaining, remaining = month_bounds(today)
    horizon_end = (remaining[0] + timedelta(days=ENSEMBLE_HORIZON_DAYS - 1)) if remaining else today
    within = [d for d in remaining if d <= horizon_end]
    beyond = [d for d in remaining if d > horizon_end]

    series = f"KXRAIN{city}M"
    km = _get(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=50")
    if not km:
        return []
    bins = [b for b in (parse_rain_bin(m) for m in km.get("markets", [])) if b]
    if not bins:
        return []
    mkt_by_ticker = {m.get("ticker"): m for m in km.get("markets", [])}

    gauge = fetch_gauge(sid, today, remaining, within, beyond)   # banked + climatology, GAUGE space
    if gauge is None:
        return []
    banked = gauge["banked"]
    years = list(range(today.year - CLIM_YEARS, today.year))
    ens_grid = fetch_ensemble_within(lat, lon, tz, today, within)
    grid_within = fetch_grid_clim_within(lat, lon, tz, within, years)
    ratio = _bias_ratio(gauge["clim_within"], grid_within)
    ens_gauge = [m * ratio for m in ens_grid]                    # grid ensemble → gauge space
    full_samples = full_remaining_samples(ens_gauge, gauge["clim_beyond"])
    banked_only_samples = gauge["clim_remaining"]

    ts = now.astimezone(timezone.utc).isoformat()
    month_iso = f"{today.year}-{today.month:02d}"
    recs = []
    for thr, ticker in bins:
        m = mkt_by_ticker.get(ticker, {})
        bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
        try:
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
        except Exception:
            bid = ask = None
        mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
        recs.append({
            "ts_utc": ts, "month": month_iso, "city": city, "series": series, "ticker": ticker,
            "acis_station": sid, "threshold_in": thr,
            "days_elapsed": days_elapsed, "days_remaining": days_remaining,
            "leakfree_monthopen": days_remaining >= LEAKFREE_MIN_REMAINING,
            "banked_in": banked, "bias_ratio": round(ratio, 3),
            "market_bid": bid, "market_ask": ask, "market_mid": mid,
            "market_prob": mid,  # YES = "above threshold"; mid is the market's P(total > thr)
            "full_prob": prob_above(thr, banked, full_samples),
            "banked_only_prob": prob_above(thr, banked, banked_only_samples),
            "n_ens": len(ens_gauge), "n_clim": len(banked_only_samples), "n_full": len(full_samples),
        })
    return recs


def main() -> int:
    ap = argparse.ArgumentParser(description="Monthly-rain kill-screen frozen snapshot logger")
    ap.add_argument("--city", help="single city code (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="print records, do not append to the log")
    ap.add_argument("--once-daily", action="store_true",
                    help="skip if a snapshot already exists for today's UTC date (safe for an hourly cron)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    if args.once_daily and not args.dry_run and _logged_today(now):
        print(f"[rain-screen] already snapshotted {now.date().isoformat()}; skipping (--once-daily)")
        return 0
    cities = [args.city] if args.city else list(CITY_COORDS)
    all_recs, ok, fail = [], [], []
    for c in cities:
        try:
            r = snapshot_city(c, now)
        except Exception as e:
            r = []
            print(f"[rain-screen] {c}: ERROR {type(e).__name__}: {e}", file=sys.stderr)
        (ok if r else fail).append(c)
        all_recs += r
        time.sleep(0.4)

    if not args.dry_run and all_recs:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            for r in all_recs:
                f.write(json.dumps(r) + "\n")

    lf = sum(1 for r in all_recs if r["leakfree_monthopen"])
    print(f"[rain-screen] {len(all_recs)} bin-snapshots from {len(ok)} cities "
          f"({'; '.join(ok)}){' | no-data: ' + ','.join(fail) if fail else ''}")
    print(f"  leak-free (month-open) snapshots this run: {lf} | "
          f"{'DRY-RUN (not written)' if args.dry_run else 'appended to ' + str(LOG.relative_to(ROOT))}")
    # a compact per-city readout of the firewall at a glance
    seen = set()
    for r in all_recs:
        if r["city"] in seen:
            continue
        seen.add(r["city"])
        fp, bp, mp = r["full_prob"], r["banked_only_prob"], r["market_prob"]
        def s(x): return f"{x:.2f}" if x is not None else " n/a"
        print(f"    {r['city']:4s} banked={r['banked_in']:.2f}in rem={r['days_remaining']:>2}d  "
              f"bin>{r['threshold_in']:.0f}in: mkt={s(mp)} FULL={s(fp)} BANKED={s(bp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

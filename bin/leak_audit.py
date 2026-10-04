#!/usr/bin/env python3
"""bin/leak_audit.py — ROADMAP Phase-0 item P0-1: separate same-day forecast SKILL from OUTCOME LEAK.

The whole profitability thesis rests on one question (see ROADMAP.md): the model beats the market on
Brier (BSS +8.8%, n=453), but 100% of that edge lives in SAME-DAY markets and is a clean zero one day
out. That lead-time signature is ambiguous — it is *exactly* what an outcome leak looks like AND
*exactly* what a legitimate faster-nowcast advantage looks like:

  • LEAK          — the "forecast" fair_prob is (partly) reading the target day's own accumulating
                    observations, so skill only appears once most of the day has happened.
  • GENUINE EDGE  — the model prices intraday-observable temperature *before the market adjusts*, so
                    skill is present while there is still real forecast uncertainty (hours before the
                    day's high/low is realized).

`leakfree_skill.py` answers a coarser question (drop ALL same-day rows → is there anything left?).
This tool answers the decisive one: it keeps the same-day rows and STRATIFIES them by how long BEFORE
the day's extreme the prediction was made, then reads model-vs-market Δbrier per stratum with an
EVENT-CLUSTERED (city+date) BCa CI. The tell:

  • skill CONCENTRATED in the <2h / post-extreme strata and NULL at ≥6h  →  LEAK (tradeable edge ≈ 0)
  • skill PRESENT at ≥6h-before-extreme (CI excludes 0)                  →  GENUINE forecast/nowcast edge

Read-only. Reuses the shared actuals cache; defaults to --no-fetch (cache only) so it is fast and
never touches the network — pass --fetch to backfill missing actuals (same safe-persist guard as
leakfree_skill.py). Same loading / outcome / clustering machinery as leakfree_skill.py so the numbers
are directly comparable.

Usage:
    python3 bin/leak_audit.py                    # forecast mode, cached actuals, ≥6h leak-free gate
    python3 bin/leak_audit.py --mode caledge     # score a different arm
    python3 bin/leak_audit.py --min-events 150   # the pre-registered P0-1 gate size
    python3 bin/leak_audit.py --fetch            # backfill missing archive actuals first
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from crps_report import parse_ticker, get_actual, load_cache, STATE  # noqa: E402
import crps_report  # noqa: E402
from shadow_score import outcome  # noqa: E402  (bin resolution — one source of truth)
from compare_variants import _event, cluster_bootstrap_ci  # noqa: E402
from data.weather_data import is_weather_ticker, STATION_TZ, CITY_ALIASES  # noqa: E402
from leakfree_skill import _asof_local_date, _DEFAULT_CUTOFF  # noqa: E402  (share the leak window + tz)

FLOG = STATE / "forecast-log.jsonl"

# Heuristic station-local hour by which the calendar day's extreme is typically REALIZED. This is the
# reference the leak axis is measured against — NOT market settlement (end of local day). A daily HIGH
# peaks mid/late afternoon; a daily LOW bottoms out around sunrise. Once asof passes this hour the
# extreme is largely observable, so any "skill" there is leak-suspect. These are deliberately
# conservative (late for highs, not-too-early for lows) so the ≥6h "leak-free" bucket is genuinely
# ahead of the extreme. Override per-run with --high-hour / --low-hour for a sensitivity sweep; the
# verdict should be robust to ±2h shifts if the edge (or the leak) is real.
EXTREME_LOCAL_HOUR = {"daily_high": 17.0, "daily_low": 7.0}

# A skill ruling off a handful of correlated events is noise (matches leakfree_skill.MIN_EVENTS_FOR_
# VERDICT). NOTE: the pre-registered P0-1 GATE in ROADMAP.md requires n_events ≥ 150 in the ≥6h
# leak-free stratum — this floor only prevents an early false verdict; --min-events raises it.
MIN_EVENTS_FOR_VERDICT = 10

# A null is only "well-powered" (can exclude a small +edge on its own) with enough independent events;
# below this the verdict says "underpowered on its own" so a thin slice isn't oversold as conclusive.
# 150 ≈ the P0-1 gate and ~80% power to detect a +0.036 Brier edge at this repo's per-event SD.
WELL_POWERED_EVENTS = 150

# hours-before-extreme strata, coarse→fine as we approach (and pass) the extreme. Each is (label, lo, hi)
# on hrs_before_extreme; hi=None is open-ended. "post" (<0) = asof AFTER the typical extreme hour.
_STRATA = [
    (">=24h",  24.0, None),   # prior-day+ forecast — cannot leak the target day at all
    ("12-24h", 12.0, 24.0),
    ("6-12h",   6.0, 12.0),   # ── ≥6h boundary: everything above is the "leak-free" pool ──
    ("2-6h",    2.0,  6.0),
    ("0-2h",    0.0,  2.0),
    ("post",   None,  0.0),    # asof past the typical extreme hour — most leak-prone
]
LEAKFREE_MIN_HRS = 6.0   # ≥ this many hours before the extreme ⇒ counts as leak-free
SUSPECT_MAX_HRS = 2.0    # < this many hours before the extreme ⇒ counts as leak-suspect


def _local_instant_utc(date_iso: str, hour: float, city: str) -> datetime:
    """UTC instant of `hour` (station-local, fractional ok) on `date_iso` for `city`.
    Falls back to UTC when the station tz is unknown (rare; flagged by caller via tz_unknown)."""
    y, m, d = (int(x) for x in date_iso.split("-"))
    hh = int(hour)
    mm = int(round((hour - hh) * 60))
    tz = STATION_TZ.get(city)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _settle_instant_utc(date_iso: str, city: str) -> datetime:
    """Market settlement instant = end of the target station-local day (local midnight of the next
    day). Objective (no meteorological assumption), used only for the secondary hours-to-settle view."""
    nxt = (_date(*(int(x) for x in date_iso.split("-"))) + timedelta(days=1)).isoformat()
    return _local_instant_utc(nxt, 0.0, city)


def _stratum(hbe: float) -> str:
    for label, lo, hi in _STRATA:
        if (lo is None or hbe >= lo) and (hi is None or hbe < hi):
            return label
    return "post"


def collect(rows, actual_of, cutoff_dt: datetime, today_iso: str, mode_filter: str,
            hi_hour: float, lo_hour: float):
    """Return (scored, counts). `scored` = list of dicts with event, brier terms and both time axes;
    one row per settled, leak-window-passing forecast-log prediction in the requested mode."""
    ext_hour = {"daily_high": hi_hour, "daily_low": lo_hour}
    scored = []
    c = Counter()
    for e in rows:
        mode = e.get("mode", "?")
        if mode_filter not in (None, "all") and mode != mode_filter:
            continue
        fp, mid = e.get("fair_prob"), e.get("market_mid")
        tk = e.get("ticker", "")
        tp = parse_ticker(tk)
        if fp is None or not tp or not is_weather_ticker(tk):
            continue
        city, mtype, date_iso = tp
        city = CITY_ALIASES.get(city, city)
        if date_iso >= today_iso:            # unsettled target day — archive actual unreliable
            continue
        try:
            asof = datetime.fromisoformat(str(e.get("asof_utc")).replace("Z", "+00:00"))
        except Exception:
            continue
        if asof <= cutoff_dt:                # historical forecast-log contamination window
            c["pre_window"] += 1
            continue
        a = actual_of(city, mtype, date_iso)
        if a is None:
            c["awaiting_actual"] += 1        # passed filters, just no settled archive actual yet
            continue
        out = outcome(e.get("bin_kind"), e.get("thr"), e.get("lo"), e.get("hi"), a)
        if out is None:
            continue
        ext = _local_instant_utc(date_iso, ext_hour.get(mtype, 17.0), city)
        hbe = (ext - asof).total_seconds() / 3600.0
        hts = (_settle_instant_utc(date_iso, city) - asof).total_seconds() / 3600.0
        model_b = (fp - out) ** 2
        market_b = ((mid - out) ** 2) if (mid is not None and 0 < mid < 1) else None
        scored.append({"event": _event(tk), "model_b": model_b, "market_b": market_b,
                       "hbe": hbe, "hts": hts, "same_day": date_iso <= _asof_local_date(asof, city)})
        c["scored"] += 1
    return scored, c


def _summ(triples) -> dict:
    """triples = [(event, model_b, market_b|None)]. Δbrier = market_b - model_b (>0 ⇒ model beats mkt)."""
    mb = [m for _, m, _ in triples]
    kb = [k for _, _, k in triples if k is not None]
    diff = [(ev, k - m) for ev, m, k in triples if k is not None]
    ci = cluster_bootstrap_ci(diff) if diff else (None, None, None)
    model = sum(mb) / len(mb) if mb else None
    market = sum(kb) / len(kb) if kb else None
    skill = ((market - model) / market) if (model is not None and market and market > 0) else None
    return {"n": len(mb), "n_events": len({ev for ev, _, _ in triples}),
            "model_brier": model, "market_brier": market, "skill": skill, "ci_diff": ci}


def _as_triples(scored):
    return [(r["event"], r["model_b"], r["market_b"]) for r in scored]


def analyze(scored, min_events: int) -> dict:
    strata = {label: _summ(_as_triples([r for r in scored if _stratum(r["hbe"]) == label]))
              for label, _, _ in _STRATA}
    clean = _summ(_as_triples([r for r in scored if r["hbe"] >= LEAKFREE_MIN_HRS]))
    suspect = _summ(_as_triples([r for r in scored if r["hbe"] < SUSPECT_MAX_HRS]))
    return {"strata": strata, "clean": clean, "suspect": suspect,
            "verdict": _verdict(clean, suspect, min_events)}


def _pos(ci) -> bool:   # CI excludes 0 on the positive side (model beats market)
    return bool(ci) and ci[1] is not None and ci[1] > 0


def _neg(ci) -> bool:   # CI excludes 0 on the negative side (model WORSE than market)
    return bool(ci) and ci[2] is not None and ci[2] < 0


def _null(ci) -> bool:  # CI spans 0
    return bool(ci) and ci[1] is not None and ci[2] is not None and ci[1] <= 0 <= ci[2]


def _verdict(clean: dict, suspect: dict, min_events: int) -> str:
    """Leak-free (≥6h before extreme) is the pool that DECIDES P0-1; `suspect` (<2h) only characterizes
    how skill behaves near the extreme, to tell a leak apart from a mere shortage of clean data.

    The four outcomes: GENUINE EDGE (leak-free beats market) · LEAK-DOMINATED (skill only near/after the
    extreme, absent or negative with real lead) · NO EDGE (well-powered leak-free null/negative, and no
    near-extreme spike) · ACCRUING (too few leak-free events to rule)."""
    cci, sci = clean.get("ci_diff"), suspect.get("ci_diff")
    n_clean = clean.get("n_events", 0)
    if not cci or cci[0] is None:
        return "INSUFFICIENT DATA — no leak-free (≥6h-before-extreme) settled predictions scored"
    if n_clean < min_events:
        return (f"ACCRUING — only {n_clean} leak-free event(s) scored (need ≥{min_events} for a "
                f"P0-1 ruling); keep the forecast log growing")
    if _pos(cci):
        return ("GENUINE EDGE — model beats the market with ≥6h forecast lead (leak-free CI excludes 0). "
                "The same-day skill is NOT purely a leak; proceed to Phase 1.")
    # No leak-free edge. If skill nonetheless appears <2h before the extreme, that gap IS the leak.
    if _pos(sci):
        lead = ("and is anti-skilled — WORSE than the market — with real lead" if _neg(cci)
                else "and is a null with real lead")
        return (f"LEAK-DOMINATED — skill appears only <2h before the extreme {lead} (≥6h Δbrier "
                f"{cci[0]:+.4f} [{cci[1]:+.4f},{cci[2]:+.4f}]). The same-day 'edge' is an outcome leak; "
                f"leak-free tradeable edge ≈ 0. Run the book as a benchmark, do not build Phase 1 on it.")
    if _neg(cci):
        return ("NO EDGE — leak-free the model is WORSE than the market (CI below 0) and shows no "
                "near-extreme spike. No tradeable forecast edge here.")
    powered = ("well-powered null" if n_clean >= WELL_POWERED_EVENTS
               else f"null (underpowered on its own at {n_clean} events — consistent with no edge but "
                    f"cannot exclude a tiny <+0.005 edge alone; lean on a ≥{WELL_POWERED_EVENTS}-event pool)")
    return (f"NO GENUINE EDGE — leak-free skill is a {powered}; CI [{cci[1]:+.4f},{cci[2]:+.4f}] "
            f"spans 0 over {n_clean} events, no near-extreme spike. Any same-day edge measured "
            f"elsewhere is leak or selection, not tradeable ≥6h-lead forecast skill.")


def _fmt_ci(ci) -> str:
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.4f} [{ci[1]:+.4f}, {ci[2]:+.4f}]"


def _row(tag: str, s: dict) -> str:
    if not s or s.get("n", 0) == 0:
        return f"  {tag:>8}: (no scorable rows)"
    sk = f"{s['skill']:+.1%}" if s.get("skill") is not None else "  n/a"
    mk = f"{s['market_brier']:.4f}" if s.get("market_brier") is not None else " n/a "
    return (f"  {tag:>8}: n={s['n']:>5} ev={s['n_events']:>4}  model={s['model_brier']:.4f} "
            f"market={mk} skill={sk}  Δbrier(mkt-model) {_fmt_ci(s.get('ci_diff'))}")


def evaluate(mode_filter="forecast", allow_fetch=False, cutoff=None,
             hi_hour=None, lo_hour=None, flog: Path = None) -> dict:
    flog = flog or FLOG
    if not flog.exists():
        return {"error": "no forecast-log.jsonl yet"}
    cutoff_dt = datetime.fromisoformat(os.environ.get("KALSHI_WEATHER_LEAKFREE_ASOF",
                                                      cutoff or _DEFAULT_CUTOFF))
    today_iso = datetime.now(timezone.utc).date().isoformat()
    hi_hour = EXTREME_LOCAL_HOUR["daily_high"] if hi_hour is None else hi_hour
    lo_hour = EXTREME_LOCAL_HOUR["daily_low"] if lo_hour is None else lo_hour
    cache = load_cache()

    def actual_of(city, mtype, date_iso):
        return get_actual(city, mtype, date_iso, cache, allow_fetch)

    # Cheap pre-filter: skip json.loads on lines that can't match the mode (94MB / 400k rows).
    needle = None if mode_filter in (None, "all") else f'"mode": "{mode_filter}"'
    rows = []
    for line in open(flog):
        if needle and needle not in line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    scored, counts = collect(rows, actual_of, cutoff_dt, today_iso, mode_filter, hi_hour, lo_hour)

    if allow_fetch:  # persist newly-fetched actuals with leakfree_skill.py's same archive-lag guard
        try:
            horizon = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
            clean = {k: v for k, v in cache.items()
                     if not (v is None and k.rsplit("|", 1)[-1] >= horizon)}
            tmp = f"{crps_report.ACTUALS_CACHE}.tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f)
            os.replace(tmp, crps_report.ACTUALS_CACHE)
        except Exception:
            pass

    return {"mode": mode_filter, "counts": dict(counts), "cutoff": cutoff_dt.isoformat(),
            "today": today_iso, "hi_hour": hi_hour, "lo_hour": lo_hour, "scored": scored}


def main() -> int:
    ap = argparse.ArgumentParser(description="P0-1 leak audit: same-day forecast skill vs outcome leak")
    ap.add_argument("--mode", default="forecast", help="forecast-log mode to score (default: forecast)")
    ap.add_argument("--min-events", type=int, default=MIN_EVENTS_FOR_VERDICT,
                    help="leak-free events required for a verdict (P0-1 gate = 150)")
    ap.add_argument("--fetch", action="store_true", help="backfill missing archive actuals (network)")
    ap.add_argument("--no-fetch", action="store_true", help="cached actuals only (default; cron-safe)")
    ap.add_argument("--high-hour", type=float, default=None, help="station-local hour a daily HIGH peaks")
    ap.add_argument("--low-hour", type=float, default=None, help="station-local hour a daily LOW bottoms")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    allow_fetch = args.fetch and not args.no_fetch
    r = evaluate(mode_filter=args.mode, allow_fetch=allow_fetch,
                 hi_hour=args.high_hour, lo_hour=args.low_hour)
    if "error" in r:
        print(f"[leak-audit] {r['error']}")
        return 1
    res = analyze(r["scored"], args.min_events)
    if args.json:
        # strip the bulky per-row scored list from the machine payload
        print(json.dumps({**{k: v for k, v in r.items() if k != "scored"},
                          "n_scored": len(r["scored"]), **res}, default=str))
        return 0

    ct = r["counts"]
    print(f"[leak-audit] P0-1 same-day skill vs leak — mode={r['mode']}, leak window asof>{r['cutoff']}, "
          f"extreme@ HIGH {r['hi_hour']:.0f}:00 / LOW {r['lo_hour']:.0f}:00 station-local")
    print(f"  scored {ct.get('scored', 0)} predictions "
          f"({ct.get('awaiting_actual', 0)} awaiting archive actuals, "
          f"{ct.get('pre_window', 0)} pre-window dropped)\n")
    print("  by hours BEFORE the day's extreme (leak axis) — Δbrier>0 ⇒ model beats market:")
    for label, _, _ in _STRATA:
        print(_row(label, res["strata"][label]))
    print("\n  pooled:")
    print(_row("LEAKFREE", res["clean"]), " (≥6h before extreme — genuine-forecast pool)")
    print(_row("SUSPECT", res["suspect"]), " (<2h before extreme — leak-prone pool)")
    print(f"\n  VERDICT (P0-1): {res['verdict']}")
    print("  (GENUINE EDGE ⇒ build Phase 1; LEAK-DOMINATED ⇒ tradeable edge ≈ 0, run book as benchmark)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

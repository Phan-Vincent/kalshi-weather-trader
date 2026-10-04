#!/usr/bin/env python3
"""bin/leakfree_skill.py — leak-free recompute of model-vs-market Brier skill.

Quant review 2026-07-01 experiment #2. `bin/shadow_score.py` reports the model's forecast skill
vs the market over EVERY shadow prediction, but that sample is majority same-day-contaminated
(§2.4: 53% of rows leak the resolution day's own observed temp) and it treats ~44k correlated
predictions as independent (§3.1 pseudo-replication). Both inflate apparent skill. This tool
recomputes the same Brier skill under two corrections:

  1. LEAK-FREE WINDOW — only rows with asof_utc AFTER the LEAK-1 guard became empirically
     effective (2026-06-30T01:21Z; override KALSHI_WEATHER_LEAKFREE_ASOF).
  2. SAME-DAY EXCLUSION — drop any row whose resolution day ≤ the asof STATION-LOCAL day
     (the model could already have seen the target day's temperature).

and reports an EVENT-CLUSTERED (city+date) BCa CI on the per-row Brier difference d = market_b −
model_b (positive ⇒ model beats the market) — the honest interval `shadow_score` lacks.

Decisive question (§4 #2): does FORECAST-mode skill survive? If leak-free forecast skill collapses
to ≤0, the "forecast beats the market" (Stage-2) case is dead. Read-only; reuses the shared
crps actuals cache; `--no-fetch` in cron so it never mutates the shared cache.

Usage:
    python3 bin/leakfree_skill.py             # fetch+cache actuals as needed
    python3 bin/leakfree_skill.py --no-fetch  # cached actuals only (cron)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta as _timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from crps_report import parse_ticker, get_actual, load_cache, STATE, ACTUALS_CACHE  # noqa: E402
import crps_report  # noqa: E402
from shadow_score import outcome  # noqa: E402  (bin resolution — one source of truth)
from compare_variants import _event, cluster_bootstrap_ci  # noqa: E402
from data.weather_data import is_weather_ticker, STATION_TZ, CITY_ALIASES  # noqa: E402

FLOG = STATE / "forecast-log.jsonl"

# LEAK-1 guard became empirically effective ~2026-06-30T01:21Z (quant review §2.4); rows before it
# carry historical forecast-log contamination. Overridable for sensitivity sweeps.
_DEFAULT_CUTOFF = "2026-06-30T01:21:00+00:00"

def _asof_local_date(asof_dt: datetime, city: str) -> str:
    # `city` is already canonicalized (CITY_ALIASES) by the caller, so STATION_TZ resolves it —
    # including the KXHIGHNY series (parse "NY" → "NYC"). An unmapped city falls back to UTC, which
    # over-excludes during 00-06Z but never keeps a leak.
    tz = STATION_TZ.get(city)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return asof_dt.astimezone(ZoneInfo(tz)).date().isoformat()
        except Exception:
            pass
    return asof_dt.astimezone(timezone.utc).date().isoformat()


def _skill(model_b: float, market_b) -> float | None:
    return ((market_b - model_b) / market_b) if (market_b and market_b > 0) else None


def score(rows, actual_of, cutoff_dt: datetime, today_iso: str, leakfree: bool = True) -> dict:
    """Pure scorer. `rows` = forecast-log dicts; `actual_of(city, mtype, date_iso) -> float|None`.

    Returns per-mode {n, model_brier, market_brier, skill, n_events, ci_diff (BCa on market_b-model_b)}.
    leakfree=False reproduces shadow_score's contaminated view (drops only unsettled/no-actual rows),
    so main() can quantify how much of the raw skill was leakage.
    """
    # mode -> list of (event, model_b, market_b|None)
    per = defaultdict(list)
    n_leak_dropped = n_window_dropped = n_no_actual = 0
    for e in rows:
        fp, mid = e.get("fair_prob"), e.get("market_mid")
        tp = parse_ticker(e.get("ticker", ""))
        if fp is None or not tp or not is_weather_ticker(e.get("ticker", "")):
            continue
        city, mtype, date_iso = tp
        city = CITY_ALIASES.get(city, city)  # KXHIGHNY parses "NY"; canonicalize so tz + get_actual
                                             # resolve it (else NY rows are permanently unscoreable)
        if date_iso >= today_iso:            # unsettled target day — archive "actual" unreliable
            continue
        asof_raw = e.get("asof_utc")
        try:
            asof = datetime.fromisoformat(str(asof_raw).replace("Z", "+00:00"))
        except Exception:
            continue
        if leakfree:
            if asof <= cutoff_dt:            # pre-guard window
                n_window_dropped += 1
                continue
            if date_iso <= _asof_local_date(asof, city):   # same-day (or later) → possible leak
                n_leak_dropped += 1
                continue
        a = actual_of(city, mtype, date_iso)
        if a is None:
            if leakfree:
                n_no_actual += 1   # passed all leak filters — just awaiting a settled archive actual
            continue
        out = outcome(e.get("bin_kind"), e.get("thr"), e.get("lo"), e.get("hi"), a)
        if out is None:
            continue
        model_b = (fp - out) ** 2
        market_b = ((mid - out) ** 2) if (mid is not None and 0 < mid < 1) else None
        per[e.get("mode", "?")].append((_event(e.get("ticker", "")), model_b, market_b))

    def summarize(triples):
        mb = [m for _, m, _ in triples]
        kb = [k for _, _, k in triples if k is not None]
        model = sum(mb) / len(mb) if mb else None
        market = sum(kb) / len(kb) if kb else None
        diff_items = [(ev, k - m) for ev, m, k in triples if k is not None]   # market_b - model_b
        ci = cluster_bootstrap_ci(diff_items) if diff_items else (None, None, None)
        n_events = len({ev for ev, _, _ in triples})
        return {"n": len(mb), "model_brier": model, "market_brier": market,
                "skill": _skill(model, market) if model is not None else None,
                "n_events": n_events, "ci_diff": ci}

    modes = {m: summarize(t) for m, t in per.items()}
    allt = [x for t in per.values() for x in t]
    modes["all"] = summarize(allt) if allt else {"n": 0}
    modes["_dropped"] = {"same_day": n_leak_dropped, "pre_window": n_window_dropped,
                         "awaiting_actual": n_no_actual}
    return modes


def evaluate(flog: Path = None, allow_fetch: bool = False, cutoff: str = None) -> dict:
    flog = flog or FLOG
    cutoff_dt = datetime.fromisoformat(os.environ.get("KALSHI_WEATHER_LEAKFREE_ASOF",
                                                      cutoff or _DEFAULT_CUTOFF))
    today_iso = datetime.now(timezone.utc).date().isoformat()
    if not flog.exists():
        return {"error": "no forecast-log.jsonl yet"}
    cache = load_cache()

    def actual_of(city, mtype, date_iso):
        return get_actual(city, mtype, date_iso, cache, allow_fetch)

    rows = []
    for line in open(flog):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    leakfree = score(rows, actual_of, cutoff_dt, today_iso, leakfree=True)
    raw = score(rows, actual_of, cutoff_dt, today_iso, leakfree=False)

    # Persist any newly-fetched actuals atomically (never under --no-fetch: the shared cache must
    # not be truncate-rewritten by the 6x/day cron — a mid-write kill → silent empty → 429 storm).
    # Guard: get_actual caches misses permanently, and the Open-Meteo archive lags ~6-7 days, so a
    # None fetched for a just-past target would STICK and poison leak-free scoring of that day once
    # the archive catches up. Never persist a None for a target within the archive-lag horizon.
    if allow_fetch:
        try:
            horizon = (datetime.now(timezone.utc).date() - _timedelta(days=8)).isoformat()
            clean = {k: v for k, v in cache.items()
                     if not (v is None and k.rsplit("|", 1)[-1] >= horizon)}
            tmp = f"{crps_report.ACTUALS_CACHE}.tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f)
            os.replace(tmp, crps_report.ACTUALS_CACHE)
        except Exception:
            pass

    return {"leakfree": leakfree, "raw": raw, "cutoff": cutoff_dt.isoformat(), "today": today_iso}


# A skill ruling off a handful of correlated events is noise — and a SINGLE event yields a
# degenerate zero-width bootstrap CI (one cluster → (pt,pt,pt)) whose lo==pt could flash a false
# "SKILL SURVIVES" GO on the first scorable day. Require this many independent weather EVENTS before
# ruling either way; below it, keep accruing.
MIN_EVENTS_FOR_VERDICT = 10


def _verdict(lf_fc: dict, awaiting: int = 0) -> str:
    ci = lf_fc.get("ci_diff") if lf_fc else None
    if not lf_fc or not ci or ci[0] is None:
        if awaiting:
            return (f"ACCRUING — {awaiting} leak-free predictions pass all filters but their target "
                    f"days aren't in the Open-Meteo archive yet (~6-7d lag); scorable soon")
        return "INSUFFICIENT DATA — no leak-free settled forecast-mode predictions yet"
    n_ev = lf_fc.get("n_events", 0)
    if n_ev < MIN_EVENTS_FOR_VERDICT:
        return (f"ACCRUING — only {n_ev} leak-free forecast event(s) scored "
                f"(need ≥{MIN_EVENTS_FOR_VERDICT} for a skill ruling); {awaiting} awaiting actuals")
    pt, lo, hi = ci
    if lo is not None and lo > 0:
        return "SKILL SURVIVES — leak-free forecast beats the market (event-clustered CI excludes 0)"
    if hi is not None and hi < 0:
        return ("NO SKILL — leak-free forecast is WORSE than the market (CI below 0); "
                "the Stage-2 'forecast beats market' case is dead")
    return "INCONCLUSIVE — leak-free forecast skill is within noise (event-clustered CI spans 0)"


def _fmt_ci(ci):
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.4f} [{ci[1]:+.4f}, {ci[2]:+.4f}]"


def _line(tag: str, s: dict) -> str:
    if not s or s.get("n", 0) == 0:
        return f"  {tag:>9}: (no scorable rows)"
    sk = s.get("skill")
    mb = s.get("market_brier")
    return (f"  {tag:>9}: n={s['n']:>5} events={s['n_events']:>4}  "
            f"model={s['model_brier']:.4f}  market={mb:.4f}  "
            f"skill_vs_mkt={sk:+.1%}  Δbrier(mkt-model) {_fmt_ci(s.get('ci_diff'))}"
            if mb else f"  {tag:>9}: n={s['n']:>5}  model={s['model_brier']:.4f}  (no market mids)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="cached actuals only (cron-safe)")
    args = ap.parse_args()
    r = evaluate(allow_fetch=not args.no_fetch)
    if "error" in r:
        print(f"[leakfree-skill] {r['error']}")
        return 1
    lf, raw = r["leakfree"], r["raw"]
    drp = lf.get("_dropped", {})
    print(f"[leakfree-skill] model-vs-market Brier — leak-free window asof>{r['cutoff']}, "
          f"same-day-excluded (station-local), event-clustered")
    print(f"  dropped: {drp.get('same_day', 0)} same-day-leak rows, "
          f"{drp.get('pre_window', 0)} pre-window rows; "
          f"{drp.get('awaiting_actual', 0)} leak-free rows awaiting a settled archive actual\n")
    print(" RAW (contaminated, matches shadow_score):")
    for m in ("legacy", "forecast", "all"):
        if m in raw:
            print(_line(m, raw[m]))
    print("\n LEAK-FREE:")
    for m in ("legacy", "forecast", "all"):
        if m in lf:
            print(_line(m, lf[m]))
    print(f"\n  VERDICT (forecast mode): "
          f"{_verdict(lf.get('forecast'), awaiting=drp.get('awaiting_actual', 0))}")
    print("  (positive Δbrier = model beats market; skill requires the event-clustered CI to exclude 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

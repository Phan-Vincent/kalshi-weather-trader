#!/usr/bin/env python3
"""bin/persistence_leak_probe.py — instrument + A/B the persistence same-day leak.

Quant review 2026-07-01 experiment #8. The persistence prior blends yesterday's observed temp into
the climatology fair_prob. The PRE-FIX code fetched UTC-"yesterday" — and in the ~00-06Z window
UTC-yesterday IS the resolution day for US stations, so persistence blended the market day's OWN
(realized) temp into the prior: a same-day look-ahead OUTSIDE the LEAK-1 guard (§2.4). LEAK-2 fixed
it — persistence now uses the STATION-LOCAL yesterday plus a belt-and-braces skip. This tool,
read-only, quantifies the leak and verifies the fix:

  A. EXPOSURE — the historical OLD-code leak rate: persistence-eligible rows where UTC-yesterday(asof)
     ≥ market_date (the old fetch would have grabbed the resolution day or later), by city + UTC hour.
  B. CURRENT-LOGIC PROJECTION — how today's station-local logic would disposition those historical
     UTC-leak rows: (i) NOW-LEGIT (station-local yesterday < market_date → a genuine PRIOR day) vs
     (ii) GUARD-SKIPPED (≥ market_date → the guard drops persistence). A large NOW-LEGIT count is
     evidence the station-local switch is engaged (a regression to UTC-yesterday would collapse it
     to 0); end-to-end guard correctness is pinned by tests/test_persistence_leak_guard.py, which
     exercises the real fair_value path — NOT by this log projection (which reimplements the guard).
  C. MAGNITUDE — how big was the leaked signal: on settled rows the OLD fetch ≈ the resolution-day
     ACTUAL, so the leaked prior shift was |w·(persist_p(actual) − climo_prob)| prob-points, and it
     pulled fair_prob toward the truth. Reported distribution + mean pull-toward-outcome.
  D. SKILL-INFLATION (decisive "was it material?") — model-vs-market Brier on OLD-LEAK rows vs CLEAN
     rows, event-clustered; if leak rows show anomalously higher skill, the leak inflated it.

Note: live prices come off the book, not the model (MEMORY: kalshi-paper-live-divergence), so this
leak has NO live-P&L impact today — a fair-value/paper-skill issue that would become P&L-material
only if a model-edge arm ships live.

Usage:  python3 bin/persistence_leak_probe.py [--fetch]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from crps_report import parse_ticker, get_actual, load_cache, STATE  # noqa: E402
from shadow_score import outcome  # noqa: E402
from compare_variants import _event, cluster_bootstrap_ci  # noqa: E402
from data.weather_data import is_weather_ticker, STATION_TZ, CITY_ALIASES, STATIONS  # noqa: E402
from model.persistence import (  # noqa: E402
    yesterday_local_date, persistence_exceedance_prob, persistence_below_prob,
    persistence_between_prob, persistence_weight,
)

FLOG = STATE / "forecast-log.jsonl"
MIN_EVENTS = 10   # a skill ruling off fewer correlated events is noise (matches leakfree_skill)


def _canon(city: str) -> str:
    return CITY_ALIASES.get(city, city)


def _parse_asof(row) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("asof_utc")).replace("Z", "+00:00"))
    except Exception:
        return None


def _utc_yesterday(asof: datetime) -> str:
    return (asof.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()


def old_leak(asof: datetime, market_date: str) -> bool:
    """The PRE-FIX bug: persistence fetched UTC-yesterday. It leaked iff UTC-yesterday(asof) is the
    resolution day or later — i.e. the fetched 'yesterday' temp was the market day's own temp."""
    return _utc_yesterday(asof) >= market_date


def _local_yesterday(asof: datetime, city: str) -> str:
    return yesterday_local_date(STATION_TZ.get(_canon(city)), now_utc=asof)


def guard_fires(asof: datetime, city: str, market_date: str) -> bool:
    """NEW code's belt-and-braces guard: skip persistence when station-local yesterday ≥ market_date."""
    return bool(market_date) and _local_yesterday(asof, city) >= market_date


def hours_to_close(asof: datetime, market_date: str) -> float:
    # Kalshi weather markets close ~06:00Z the day AFTER the target date (shadow_score convention)
    close = datetime.fromisoformat(market_date + "T06:00:00+00:00") + timedelta(days=1)
    return (close - asof).total_seconds() / 3600.0


# ── A + B: exposure and fix verification ──────────────────────────────────────

def instrument(rows) -> dict:
    by_city = defaultdict(lambda: [0, 0])   # city -> [old_leak, total]
    by_hour = defaultdict(lambda: [0, 0])   # utc_hour -> [old_leak, total]
    n = leak = now_legit = guard_skipped = 0
    for r in rows:
        tp = parse_ticker(r.get("ticker", ""))
        asof = _parse_asof(r)
        if not tp or asof is None or not is_weather_ticker(r.get("ticker", "")):
            continue
        _c, mtype, date_iso = tp
        if mtype not in ("daily_high", "daily_low"):   # persistence only runs for daily hi/lo
            continue
        n += 1
        by_city[_c][1] += 1
        h = asof.astimezone(timezone.utc).hour
        by_hour[h][1] += 1
        if old_leak(asof, date_iso):
            leak += 1
            by_city[_c][0] += 1
            by_hour[h][0] += 1
            if guard_fires(asof, _c, date_iso):
                guard_skipped += 1        # new code drops persistence (past-dated markets)
            else:
                now_legit += 1            # new code now fetches a genuine prior day (the primary fix)
    return {"n": n, "leak": leak, "leak_rate": (leak / n) if n else None,
            "now_legit": now_legit, "guard_skipped": guard_skipped,
            # Falsifiable signal (NOT a tautology): a regression of yesterday_local_date back to
            # UTC-yesterday would push every old-leak row into guard_skipped and collapse now_legit
            # to 0. So now_legit>0 is real evidence the station-local switch differs from UTC here.
            "station_local_fix_engaged": now_legit > 0,
            "by_city": {c: {"leak": v[0], "total": v[1]} for c, v in sorted(by_city.items())},
            "by_hour": {h: {"leak": v[0], "total": v[1]} for h, v in sorted(by_hour.items())}}


# ── C: leaked magnitude (old fetch ≈ resolution-day actual on settled rows) ────

def _persist_and_climo(row, city, mtype, date_iso, temp_f, climo):
    bk, thr, lo, hi = row.get("bin_kind"), row.get("thr"), row.get("lo"), row.get("hi")
    mmdd = date_iso[5:]
    if bk == "above" and thr is not None:
        return persistence_exceedance_prob(thr, temp_f, mtype), climo.exceedance_prob(city, mmdd, mtype, thr)
    if bk == "below" and thr is not None:
        return persistence_below_prob(thr, temp_f, mtype), climo.below_prob(city, mmdd, mtype, thr)
    if bk == "between" and lo is not None and hi is not None:
        return persistence_between_prob(lo, hi, temp_f, mtype), climo.between_prob(city, mmdd, mtype, lo, hi)
    return None, None


def leaked_magnitude(rows, actual_of, climo, today_iso: str) -> dict:
    deltas, pulls = [], []
    n = 0
    for r in rows:
        tp = parse_ticker(r.get("ticker", ""))
        asof = _parse_asof(r)
        if not tp or asof is None or not is_weather_ticker(r.get("ticker", "")):
            continue
        city, mtype, date_iso = tp
        city = _canon(city)
        if mtype not in ("daily_high", "daily_low") or date_iso >= today_iso:
            continue
        # Only the clean same-day leak (UTC-yesterday == market_date) → the old fetch was this day's temp
        if _utc_yesterday(asof) != date_iso:
            continue
        actual = actual_of(city, mtype, date_iso)   # ≈ the temp the OLD code leaked in as "yesterday"
        if actual is None:
            continue
        persist_p, climo_p = _persist_and_climo(r, city, mtype, date_iso, actual, climo)
        if persist_p is None or climo_p is None:
            continue
        out = outcome(r.get("bin_kind"), r.get("thr"), r.get("lo"), r.get("hi"), actual)
        w = max(0.0, min(1.0, persistence_weight(hours_to_close(asof, date_iso))))
        delta_signed = w * (persist_p - climo_p)
        deltas.append(abs(delta_signed))
        if out is not None:
            # signed pull of the prior TOWARD the realized outcome (positive = leak helped the model)
            pulls.append(delta_signed * (1.0 if out == 1 else -1.0))
        n += 1
    return {"n": n,
            "delta_median_pp": _pctl(deltas, 0.5), "delta_p90_pp": _pctl(deltas, 0.9),
            "delta_max_pp": max(deltas) if deltas else None,
            "mean_pull_toward_outcome_pp": (sum(pulls) / len(pulls)) if pulls else None}


def _pctl(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


# ── D: skill inflation (old-leak vs clean, event-clustered) ───────────────────

def skill_partition(rows, actual_of, today_iso: str) -> dict:
    buckets = {"leak": [], "clean": []}
    for r in rows:
        tp = parse_ticker(r.get("ticker", ""))
        asof = _parse_asof(r)
        fp = r.get("fair_prob")
        if not tp or asof is None or fp is None or not is_weather_ticker(r.get("ticker", "")):
            continue
        city, mtype, date_iso = tp
        if mtype not in ("daily_high", "daily_low") or date_iso >= today_iso:
            continue
        a = actual_of(_canon(city), mtype, date_iso)
        if a is None:
            continue
        out = outcome(r.get("bin_kind"), r.get("thr"), r.get("lo"), r.get("hi"), a)
        if out is None:
            continue
        mid = r.get("market_mid")
        model_b = (fp - out) ** 2
        market_b = ((mid - out) ** 2) if (mid is not None and 0 < mid < 1) else None
        name = "leak" if old_leak(asof, date_iso) else "clean"
        buckets[name].append((_event(r.get("ticker", "")), model_b, market_b))

    def summ(triples):
        if not triples:
            return {"n": 0, "n_events": 0}
        mb = [m for _, m, _ in triples]
        diff = [(ev, k - m) for ev, m, k in triples if k is not None]
        kb = [k for _, _, k in triples if k is not None]
        market = sum(kb) / len(kb) if kb else None
        model = sum(mb) / len(mb)
        return {"n": len(mb), "n_events": len({ev for ev, _, _ in triples}),
                "model_brier": model, "market_brier": market,
                "skill": ((market - model) / market) if (market and market > 0) else None,
                "ci_diff": cluster_bootstrap_ci(diff) if diff else (None, None, None)}
    return {k: summ(v) for k, v in buckets.items()}


def _clear(ci) -> bool:
    _, lo, hi = ci
    return lo is not None and lo != hi and ((lo > 0 and hi > 0) or (lo < 0 and hi < 0))


def _verdict(inst, skill) -> dict:
    if inst["leak"] == 0:
        projection = "no historical UTC-leak rows in the log — nothing to project"
    else:
        eng = "ENGAGED" if inst["station_local_fix_engaged"] else "NOT engaged — possible regression"
        projection = (f"the current station-local logic dispositions all {inst['leak']} historical "
                      f"UTC-leak rows to a genuine PRIOR day ({inst['now_legit']}) or a guard-skip "
                      f"({inst['guard_skipped']}); station-local switch {eng}. Guard correctness is "
                      f"pinned by tests/test_persistence_leak_guard.py, not by this log projection.")
    lk, cl = skill.get("leak", {}), skill.get("clean", {})
    lk_ci, cl_ci = lk.get("ci_diff"), cl.get("ci_diff")
    if not lk_ci or lk.get("n_events", 0) < MIN_EVENTS or cl.get("n_events", 0) < MIN_EVENTS:
        material = (f"SKILL-INFLATION: INSUFFICIENT DATA — leak n_events={lk.get('n_events', 0)}, "
                    f"clean n_events={cl.get('n_events', 0)} (need ≥{MIN_EVENTS} each)")
    else:
        leak_clear = _clear(lk_ci) and lk_ci[0] > 0
        clean_clear = _clear(cl_ci) and cl_ci[0] > 0
        if leak_clear and not clean_clear:
            material = ("SKILL-INFLATION CONFIRMED — old-leak rows beat the market (CI excludes 0) "
                        "while clean rows do not; the leak was inflating apparent skill")
        elif leak_clear and clean_clear and lk_ci[0] > cl_ci[0]:
            material = "SKILL-INFLATION LIKELY — both positive but leak skill exceeds clean skill"
        else:
            material = "NO CLEAR SKILL-INFLATION — old-leak and clean skill are comparable / within noise"
    return {"fix_projection": projection, "materiality": material}


def evaluate(flog: Path = None, allow_fetch: bool = False) -> dict:
    flog = flog or FLOG
    if not flog.exists():
        return {"error": "no forecast-log.jsonl"}
    rows = []
    for line in open(flog):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    today_iso = datetime.now(timezone.utc).date().isoformat()
    from model.fair_value import _get_climo
    climo = _get_climo()
    cache = load_cache()

    def actual_of(c, m, d):
        return get_actual(c, m, d, cache, allow_fetch)

    inst = instrument(rows)
    mag = leaked_magnitude(rows, actual_of, climo, today_iso)
    skill = skill_partition(rows, actual_of, today_iso)
    return {"instrument": inst, "magnitude": mag, "skill": skill, "verdict": _verdict(inst, skill)}


def _fmt_ci(ci):
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.4f} [{ci[1]:+.4f}, {ci[2]:+.4f}]"


def _f(x, nd=4):
    return "n/a" if x is None else f"{x:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="fetch missing actuals (default cache-only)")
    args = ap.parse_args()
    r = evaluate(allow_fetch=args.fetch)
    if "error" in r:
        print(f"[persistence-leak-probe] {r['error']}")
        return 1
    inst, mg, sk = r["instrument"], r["magnitude"], r["skill"]
    print("[persistence-leak-probe] persistence same-day leak — instrument + A/B (read-only)")
    print(f"  A. OLD-code exposure: {inst['leak']}/{inst['n']} persistence-eligible rows = "
          f"{(inst['leak_rate'] or 0):.1%} had UTC-yesterday ≥ market_date")
    hot = sorted(inst["by_hour"].items(), key=lambda kv: -kv[1]["leak"])[:5]
    print("     top leak UTC-hours: " + ", ".join(f"{h:02d}Z {v['leak']}" for h, v in hot if v["leak"]))
    print(f"  B. current-logic projection: {inst['now_legit']} now-legit prior day + "
          f"{inst['guard_skipped']} guard-skipped of {inst['leak']} historical UTC-leak rows")
    print(f"     {r['verdict']['fix_projection']}")
    print(f"  C. leaked magnitude (old fetch ≈ resolution actual, {mg['n']} settled same-day rows): "
          f"|Δ| median {_f(mg['delta_median_pp'])} / p90 {_f(mg['delta_p90_pp'])} / max "
          f"{_f(mg['delta_max_pp'])} pp; mean pull→outcome {_f(mg['mean_pull_toward_outcome_pp'])} pp")
    print("  D. skill inflation (event-clustered Δbrier = market−model, >0 ⇒ model beats market):")
    print(f"       OLD-LEAK: n={sk['leak'].get('n', 0)} events={sk['leak'].get('n_events', 0)}  "
          f"Δbrier {_fmt_ci(sk['leak'].get('ci_diff'))}")
    print(f"       CLEAN   : n={sk['clean'].get('n', 0)} events={sk['clean'].get('n_events', 0)}  "
          f"Δbrier {_fmt_ci(sk['clean'].get('ci_diff'))}")
    print(f"  VERDICT: {r['verdict']['materiality']}")
    print("  (live prices come off the book, not the model → NO live-P&L impact today; "
          "fair-value/paper-skill only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

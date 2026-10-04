#!/usr/bin/env python3
"""bin/m3_multiday_taker.py — ROADMAP Phase-1 item M3: is there a TAKEABLE edge in 2–4 day markets?

The whole edge search (P0-1/P0-2) killed same-day, resting-maker, and ≤1-day forecast edges — all
leak-exposed or already priced by the market. M3 is the ONE orthogonal lane left: multi-day markets,
where (a) a leak is impossible (you can't observe a temperature 3 days out), (b) fills aren't the wall
because you TAKE across the spread instead of resting a quote, and (c) the book is thinnest/stalest, so
a large model-vs-market divergence might not be priced yet.

The hypothesis is NOT "the model beats the market on average" (P0-1 says it doesn't) — it's the sharper
"on the large-divergence subset at long lead, the model beats a stale multi-day quote by enough to pay
the taker fee + spread." So this tool trades ONLY where |fair_prob − market_mid| ≥ a threshold, at a
configurable lead window (default 2–4 days), and reports realized per-contract taker P&L, event-clustered.

DATA HONESTY — no bid/ask is logged at multi-day lead (brier-log is stale; forecast-log has only the
mid). So this computes GROSS P&L at the MID minus the exact Kalshi taker fee (ceil(0.07·P·(1−P))). That
is an UPPER BOUND on the real taker edge: a taker actually pays the ask, ≥ half the (wide) multi-day
spread above the mid. `--spread-haircut-cents` subtracts an assumed half-spread so you can see how wide
the book would have to be to kill it. **Decision rule: if even the gross-at-mid−fee edge does not clear
0, M3 is dead and needs no bid/ask study. Only if it clears do we go get real book data.** Read-only.

Usage:
    python3 bin/m3_multiday_taker.py                     # forecast model, 2–4d, threshold sweep
    python3 bin/m3_multiday_taker.py --min-lead 2 --max-lead 4 --threshold 10
    python3 bin/m3_multiday_taker.py --spread-haircut-cents 3   # assume 6¢ spread (3¢ half)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date as _date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from crps_report import parse_ticker, get_actual, load_cache, STATE  # noqa: E402
from shadow_score import outcome  # noqa: E402  (bin resolution — one source of truth)
from compare_variants import _event, cluster_bootstrap_ci  # noqa: E402
from data.weather_data import is_weather_ticker, CITY_ALIASES  # noqa: E402
from leak_audit import _asof_local_date, _DEFAULT_CUTOFF  # noqa: E402  (share leak window + tz-local day)
from trader.orders import _kalshi_taker_fee_cents  # noqa: E402  (exact Kalshi taker fee)

FLOG = STATE / "forecast-log.jsonl"
THRESHOLDS = [0.0, 5.0, 10.0, 15.0]   # |fair−mid| entry gates, cents
MIN_EVENTS_FOR_VERDICT = 10


def _lead_days(date_iso: str, asof: datetime, city: str) -> int:
    """Whole days from the asof STATION-LOCAL day to the target resolution day (≥1 ⇒ not same-day)."""
    y, m, d = (int(x) for x in date_iso.split("-"))
    ay, am, ad = (int(x) for x in _asof_local_date(asof, city).split("-"))
    return (_date(y, m, d) - _date(ay, am, ad)).days


def collect(rows, actual_of, cutoff_dt: datetime, today_iso: str, mode_filter: str,
            min_lead: int, max_lead: int):
    """{ticker: [snapshot dicts]} for settled weather markets in the lead window, with outcome resolved."""
    by_ticker = defaultdict(list)
    counts = {"scanned": 0, "in_window": 0, "no_actual": 0, "unsettled": 0}
    for e in rows:
        if mode_filter not in (None, "all") and e.get("mode", "?") != mode_filter:
            continue
        fp, mid = e.get("fair_prob"), e.get("market_mid")
        tk = e.get("ticker", "")
        tp = parse_ticker(tk)
        if fp is None or mid is None or not (0 < mid < 1) or not tp or not is_weather_ticker(tk):
            continue
        city, mtype, date_iso = tp
        city = CITY_ALIASES.get(city, city)
        if date_iso >= today_iso:
            counts["unsettled"] += 1
            continue
        try:
            asof = datetime.fromisoformat(str(e.get("asof_utc")).replace("Z", "+00:00"))
        except Exception:
            continue
        if asof <= cutoff_dt:
            continue
        counts["scanned"] += 1
        lead = _lead_days(date_iso, asof, city)
        if not (min_lead <= lead <= max_lead):
            continue
        counts["in_window"] += 1
        a = actual_of(city, mtype, date_iso)
        if a is None:
            counts["no_actual"] += 1
            continue
        y = outcome(e.get("bin_kind"), e.get("thr"), e.get("lo"), e.get("hi"), a)
        if y is None:
            continue
        by_ticker[tk].append({"asof": asof, "lead": lead, "fair": fp, "mid": mid, "y": float(y),
                              "event": _event(tk)})
    return by_ticker, counts


def simulate(by_ticker: dict, threshold_c: float, spread_haircut_c: float):
    """One taker trade per market: the EARLIEST snapshot in the window whose |fair−mid| clears the gate.
    Returns [(event, net_cents, gross_cents, lead)]. P&L at the mid (upper bound) minus fee minus haircut."""
    trades = []
    thr = threshold_c / 100.0
    for tk, recs in by_ticker.items():
        cand = [r for r in recs if abs(r["fair"] - r["mid"]) >= thr]
        if not cand:
            continue
        r = min(cand, key=lambda x: x["asof"])           # act on the first divergence you'd see
        yes = (r["fair"] - r["mid"]) > 0
        entry = r["mid"] if yes else (1.0 - r["mid"])    # taker pays the mid-side price (upper bound)
        gross_c = ((r["y"] - r["mid"]) if yes else (r["mid"] - r["y"])) * 100.0
        fee_c = _kalshi_taker_fee_cents(1, max(1, min(99, round(entry * 100))))
        trades.append((r["event"], gross_c - fee_c - spread_haircut_c, gross_c, r["lead"]))
    return trades


def _summ(trades):
    if not trades:
        return {"n": 0, "n_events": 0}
    net = [(ev, nc) for ev, nc, _, _ in trades]
    gross = [gc for _, _, gc, _ in trades]
    ci = cluster_bootstrap_ci(net)
    return {"n": len(trades), "n_events": len({ev for ev, _, _, _ in trades}),
            "net_cpc": sum(n for _, n in net) / len(net), "gross_cpc": sum(gross) / len(gross),
            "ci_net": ci, "win_rate": sum(1 for _, n in net if n > 0) / len(net)}


def _fmt_ci(ci) -> str:
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.2f} [{ci[1]:+.2f}, {ci[2]:+.2f}]"


def _verdict(cells: list[dict], min_events: int) -> str:
    """cells = summaries across the threshold sweep for the requested lead window. Positive if ANY
    well-populated cell's net-taker CI clears 0; otherwise dead (upper bound already ≤0)."""
    ruled = [c for c in cells if c.get("n_events", 0) >= min_events]
    if not ruled:
        return f"ACCRUING — no threshold cell has ≥{min_events} events yet; keep the forecast log growing"
    hits = [c for c in ruled if c["ci_net"][1] is not None and c["ci_net"][1] > 0]
    if hits:
        h = max(hits, key=lambda c: c["ci_net"][0])
        return (f"SIGNAL — net taker edge clears 0 at threshold {h['threshold']:.0f}¢: "
                f"{_fmt_ci(h['ci_net'])} ¢/ct over {h['n_events']} events. This is the UPPER BOUND "
                f"(no spread cost modeled) — get real 2–4d bid/ask before risking capital.")
    best = max(ruled, key=lambda c: (c["gross_cpc"]))
    return (f"DEAD — even the gross-at-mid upper bound clears 0 nowhere (best gross {best['gross_cpc']:+.2f}¢/ct "
            f"at threshold {best['threshold']:.0f}¢, net {_fmt_ci(best['ci_net'])}). The model has no takeable "
            f"multi-day edge; do not pursue longer leads or fetch book data.")


def evaluate(mode_filter="forecast", min_lead=2, max_lead=4, spread_haircut_c=0.0,
             thresholds=None, allow_fetch=False, flog: Path = None) -> dict:
    flog = flog or FLOG
    if not flog.exists():
        return {"error": "no forecast-log.jsonl yet"}
    cutoff_dt = datetime.fromisoformat(_DEFAULT_CUTOFF)
    today_iso = datetime.now(timezone.utc).date().isoformat()
    thresholds = thresholds or THRESHOLDS
    cache = load_cache()

    def actual_of(city, mtype, date_iso):
        return get_actual(city, mtype, date_iso, cache, allow_fetch)

    needle = None if mode_filter in (None, "all") else f'"mode": "{mode_filter}"'
    rows = []
    for line in open(flog):
        if needle and needle not in line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    by_ticker, counts = collect(rows, actual_of, cutoff_dt, today_iso, mode_filter, min_lead, max_lead)
    cells = []
    for thr in thresholds:
        s = _summ(simulate(by_ticker, thr, spread_haircut_c))
        s["threshold"] = thr
        cells.append(s)
    # per-integer-lead texture at the 0¢ (trade-everything) threshold
    per_lead = {}
    for lead in range(min_lead, max_lead + 1):
        sub = {tk: [r for r in recs if r["lead"] == lead] for tk, recs in by_ticker.items()}
        sub = {tk: r for tk, r in sub.items() if r}
        per_lead[lead] = _summ(simulate(sub, 0.0, spread_haircut_c))
    if not by_ticker:
        verdict = (f"NO MARKETS — 0 settled snapshots at {min_lead}–{max_lead}d lead over the whole "
                   f"forecast log. Kalshi lists daily high/low weather markets only ~0–1.7 days out "
                   f"(confirmed against the live API 2026-07-14), so this lane has no markets to trade and "
                   f"CANNOT accrue. M3 is structurally untestable/dead for daily-bin products — not null, "
                   f"but non-existent. Only a different (longer-horizon) weather product could revive it.")
    else:
        verdict = _verdict(cells, MIN_EVENTS_FOR_VERDICT)
    return {"mode": mode_filter, "min_lead": min_lead, "max_lead": max_lead,
            "spread_haircut_c": spread_haircut_c, "counts": counts, "cells": cells,
            "per_lead": per_lead, "n_markets": len(by_ticker), "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description="M3: takeable multi-day (2–4d) taker edge probe")
    ap.add_argument("--mode", default="forecast", help="forecast-log mode (default: forecast)")
    ap.add_argument("--min-lead", type=int, default=2, help="min days-ahead (default 2)")
    ap.add_argument("--max-lead", type=int, default=4, help="max days-ahead (default 4)")
    ap.add_argument("--threshold", type=float, default=None, help="single |fair−mid|¢ gate (else sweep)")
    ap.add_argument("--spread-haircut-cents", type=float, default=0.0,
                    help="assumed half-spread the taker pays above mid (default 0 = pure upper bound)")
    ap.add_argument("--fetch", action="store_true", help="backfill missing archive actuals (network)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    thresholds = [args.threshold] if args.threshold is not None else THRESHOLDS
    r = evaluate(mode_filter=args.mode, min_lead=args.min_lead, max_lead=args.max_lead,
                 spread_haircut_c=args.spread_haircut_cents, thresholds=thresholds,
                 allow_fetch=args.fetch)
    if "error" in r:
        print(f"[m3] {r['error']}")
        return 1
    if args.json:
        print(json.dumps(r, default=str))
        return 0
    c = r["counts"]
    print(f"[m3] takeable multi-day edge — mode={r['mode']}, lead {r['min_lead']}–{r['max_lead']}d, "
          f"spread-haircut {r['spread_haircut_c']:.1f}¢, taker fee = ceil(0.07·P·(1−P))")
    print(f"  {r['n_markets']} settled markets in the lead window "
          f"({c['in_window']} snapshots, {c['no_actual']} awaiting actuals)\n")
    print("  by |fair−mid| entry threshold (one taker trade per market, earliest qualifying snapshot):")
    print(f"    {'thr':>5} {'trades':>7} {'events':>7} {'gross¢/ct':>10} {'net¢/ct':>9}  "
          f"{'net CI (event-clustered)':>26} {'win%':>6}")
    for s in r["cells"]:
        if s.get("n", 0) == 0:
            print(f"    {s['threshold']:>4.0f}¢  (no trades clear this threshold)")
            continue
        print(f"    {s['threshold']:>4.0f}¢ {s['n']:>7} {s['n_events']:>7} {s['gross_cpc']:>+10.2f} "
              f"{s['net_cpc']:>+9.2f}  {_fmt_ci(s['ci_net']):>26} {s['win_rate']*100:>5.0f}%")
    print("\n  per lead-day (trade-everything, 0¢ gate):")
    for lead, s in r["per_lead"].items():
        if s.get("n", 0) == 0:
            print(f"    {lead}d: (no settled markets)")
            continue
        print(f"    {lead}d: n={s['n']:>4} ev={s['n_events']:>3}  gross {s['gross_cpc']:>+6.2f}¢  "
              f"net {_fmt_ci(s['ci_net'])} ¢/ct")
    print(f"\n  VERDICT (M3): {r['verdict']}")
    print("  (SIGNAL ⇒ get real 2–4d bid/ask before capital; DEAD ⇒ multi-day taker lane is closed too)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

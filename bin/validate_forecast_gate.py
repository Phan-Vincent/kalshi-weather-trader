#!/usr/bin/env python3
"""bin/validate_forecast_gate.py — Stage-1 (Phase C) forecast→live go/no-go gate.

Does the forecast model improve realized P&L over climatology when a strategy ACTUALLY
USES it? Compares two MODEL-EDGE arms (mode=mm — decisions driven by fair_prob), identical
except the fair-values file they price on:
    forecast-edge : forecast model at LIVE knobs (fair-values-forecast-live.json)
    climo-edge    : climatology   at LIVE knobs (fair-values.json)
Both built at the SAME knobs, so the diff isolates EXACTLY the USE_FORECAST=1 flip that
forecast→live (Stage 2) would ship — no KDE/floor/blend confound.

REBUILT 2026-06-29: the old gate compared `premium-forecast` vs `premium`, but premium mode
prices 100% off the order book and ignores fair_prob (trader/scanner.py:375-405) — those arms
never used the model, so their diff was order-book snapshot-timing noise, not forecast skill.

Data hygiene:
  --since YYYY-MM-DD : count only trades opened on/after this date. Default = the LEAK-1
                       fix date, so leak-era settled trades (same-day Open-Meteo look-ahead)
                       don't contaminate. Pass --since "" to disable the floor.
  Same-day markets are EXCLUDED: post-fix BOTH arms price same-day on climatology (identical
  fair value), so they carry no forecast signal and would dilute the matched count.

GATE PASS requires ALL of:
  1. forecast-edge own per-contract realized P&L 95% CI excludes 0 (a real positive edge), AND
  2. pairwise (forecast - climo) matched-market diff 95% CI excludes 0 favoring forecast, AND
  3. matched (since-floored, non-same-day) markets >= --min-n.
Realized SETTLED P&L only (markout misleads for intraday-resolving weather). Per-contract,
event-clustered. The per-side (YES/NO) breakdown is printed regardless — watch the YES bin.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))
from compare_variants import (  # noqa: E402  — reuse the repo's stats, don't duplicate
    _read_jsonl, bootstrap_ci, cluster_bootstrap_ci, _event, _arms_from_config, _fmt_ci,
)

STATE = ROOT / "state"
LEAK_FIX_DATE = "2026-06-29"  # commit eb0707e — same-day forecast look-ahead closed
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _ticker_date(ticker: str) -> str | None:
    """Resolving date (YYYY-MM-DD) parsed from a ...-YYMONDD-... ticker token
    (Kalshi weather format is year-first, e.g. KXHIGHTNY-26JUN29-... = 2026-06-29)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker or "")
    if not m:
        return None
    yy, mmm, dd = m.groups()
    mo = _MON.get(mmm)
    return f"20{yy}-{mo:02d}-{int(dd):02d}" if mo else None


def _opened_date(rec: dict) -> str:
    return (rec.get("opened_utc") or rec.get("opened_at") or "")[:10]


def _is_sameday(rec: dict) -> bool:
    """Trade filled on the market's own resolution day → it priced on climatology in BOTH
    arms post-LEAK-1, so it carries no forecast signal."""
    td = _ticker_date(rec.get("ticker", ""))
    return td is not None and td == _opened_date(rec)


def _filter(recs: list[dict], since: str, drop_sameday: bool) -> list[dict]:
    out = []
    for r in recs:
        if not r.get("qty"):
            continue
        if since and _opened_date(r) and _opened_date(r) < since:
            continue
        if drop_sameday and _is_sameday(r):
            continue
        out.append(r)
    return out


def _per_contract(recs: list[dict]) -> list[float]:
    return [float(r.get("pnl_cents", 0)) / r["qty"] for r in recs if r.get("qty")]


def _per_ticker(recs: list[dict]) -> dict:
    agg: dict = {}
    for r in recs:
        tk, q = r.get("ticker"), r.get("qty")
        if not tk or not q:
            continue
        a = agg.setdefault(tk, {"pnl": 0.0, "qty": 0.0})
        a["pnl"] += float(r.get("pnl_cents", 0))
        a["qty"] += float(q)
    return agg


def _matched_pairwise(frecs: list[dict], brecs: list[dict]) -> dict:
    """Per-contract, event-clustered matched-market diff on already-filtered records."""
    fa, ba = _per_ticker(frecs), _per_ticker(brecs)
    keys = sorted(set(fa) & set(ba))
    items_pc = [
        (_event(k), fa[k]["pnl"] / fa[k]["qty"] - ba[k]["pnl"] / ba[k]["qty"])
        for k in keys if fa[k]["qty"] > 0 and ba[k]["qty"] > 0
    ]
    return {
        "matched_markets": len(keys),
        "matched_events": len({_event(k) for k in keys}),
        "diff_ci": cluster_bootstrap_ci(items_pc) if items_pc else (None, None, None),
    }


def _side_breakdown(recs: list[dict]) -> dict:
    out = {}
    for side in ("yes", "no"):
        sr = [r for r in recs if r.get("side") == side]
        pc = _per_contract(sr)
        out[side] = {
            "n": len(sr),
            "contracts": sum(int(r.get("qty", 0)) for r in sr),
            "pc_ci": bootstrap_ci(pc) if pc else (None, None, None),
        }
    return out


def _ci_above_zero(ci) -> bool:
    pt, lo, hi = ci
    return pt is not None and lo is not None and lo > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "variants.json"))
    ap.add_argument("--forecast", default="forecast-edge")
    ap.add_argument("--baseline", default="climo-edge")
    ap.add_argument("--since", default=LEAK_FIX_DATE,
                    help="count trades opened on/after YYYY-MM-DD (default: LEAK-1 fix); '' disables")
    ap.add_argument("--keep-sameday", action="store_true",
                    help="don't exclude same-day markets (they carry no forecast signal post-fix)")
    ap.add_argument("--min-n", type=int, default=30, help="min matched markets to allow a PASS")
    args = ap.parse_args()

    dirs = dict(_arms_from_config(Path(args.config)))
    for nm in (args.forecast, args.baseline):
        if nm not in dirs:
            print(f"unknown arm '{nm}'. Known: {', '.join(dirs)}")
            return 2
    fdir, bdir = STATE / dirs[args.forecast], STATE / dirs[args.baseline]
    drop_sd = not args.keep_sameday

    fraw = _read_jsonl(fdir / "settlement-log.jsonl")
    braw = _read_jsonl(bdir / "settlement-log.jsonl")
    frecs = _filter(fraw, args.since, drop_sd)
    brecs = _filter(braw, args.since, drop_sd)

    f_pc = bootstrap_ci(_per_contract(frecs)) if frecs else (None, None, None)
    b_pc = bootstrap_ci(_per_contract(brecs)) if brecs else (None, None, None)
    pw = _matched_pairwise(frecs, brecs)

    print(f"\nStage-1 forecast→live gate (MODEL-EDGE):  {args.forecast}  vs  {args.baseline}")
    print(f"  realized settled P&L; since={args.since or 'all'}; "
          f"same-day {'kept' if args.keep_sameday else 'excluded'}; * = 95% CI excludes 0")
    print(f"  (filtered {len(fraw)}→{len(frecs)} forecast, {len(braw)}→{len(brecs)} climo trades)\n")
    print(f"  {args.forecast:16s} n={len(frecs):>4d}  ¢/ct {_fmt_ci(f_pc, sig='', excl0=True)}")
    print(f"  {args.baseline:16s} n={len(brecs):>4d}  ¢/ct {_fmt_ci(b_pc, sig='', excl0=True)}")
    print(f"  pairwise matched markets: {pw['matched_markets']} (events {pw['matched_events']})   "
          f"diff/ct {_fmt_ci(pw['diff_ci'], sig='')}   (+ ⇒ forecast > climo)")

    print("\n  per-side realized ¢/contract (watch the YES-bin bleed):")
    for label, recs in ((args.forecast, frecs), (args.baseline, brecs)):
        sd = _side_breakdown(recs)
        print(f"    {label}:")
        for side in ("yes", "no"):
            s = sd[side]
            print(f"      {side.upper():3s} n={s['n']:>4d} ct={s['contracts']:>5d}  "
                  f"¢/ct {_fmt_ci(s['pc_ci'], sig='', excl0=True)}")

    c1 = _ci_above_zero(f_pc)
    c2 = _ci_above_zero(pw["diff_ci"])
    c3 = pw["matched_markets"] >= args.min_n
    passed = c1 and c2 and c3
    print(f"\n  GATE: {'PASS ✅ — forecast beats climo, ready for Stage 2' if passed else 'NOT YET ❌'}")
    print(f"    [{'x' if c1 else ' '}] forecast-edge own ¢/ct CI > 0")
    print(f"    [{'x' if c2 else ' '}] pairwise diff CI > 0 (forecast beats climo)")
    print(f"    [{'x' if c3 else ' '}] matched markets ({pw['matched_markets']}) >= {args.min_n}")
    if not passed:
        print("    → keep accumulating paper trades; do NOT ship to live (Stage 2) yet.")
        if not frecs and not brecs:
            print("    (forecast-edge/climo-edge are new arms — they begin accumulating next cycle.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

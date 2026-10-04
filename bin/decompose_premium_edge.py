#!/usr/bin/env python3
"""
bin/decompose_premium_edge.py — where does the premium-capture edge live?

Combines the REALIZED settled trades (settlement-log.jsonl) across the offset-0 band-priced premium
arms (live-premium + paper-premium are the SAME strategy — premium mode prices off the order book,
not the model) and breaks the realized edge down by ENTRY PRICE BAND and by CITY. Read-only.

Use it to decide band gates (KALSHI_WEATHER_PREMIUM_MIN/MAX_CENTS) and per-city skips: the bands/
cities with negative EV/ct on a non-trivial sample are the ones bleeding the edge.

    python3 bin/decompose_premium_edge.py                 # default arms
    python3 bin/decompose_premium_edge.py arm1 arm2 ...   # explicit arm dirs (under state/)
"""
from __future__ import annotations
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARMS = ["live-premium", "paper-premium"]
BANDS = [(1, 19), (20, 29), (30, 39), (40, 49), (50, 59), (60, 99)]


def _city(tk: str) -> str:
    m = re.match(r"KX(?:HIGH|LOW)T?([A-Z]+)", tk or "")
    return m.group(1) if m else "?"


def _band(px: int) -> str:
    for lo, hi in BANDS:
        if lo <= px <= hi:
            return f"{lo:>2}-{hi}"
    return "?"


def _load(arms: list[str]) -> list[dict]:
    rows = []
    for arm in arms:
        p = ROOT / "state" / arm / "settlement-log.jsonl"
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            qty, pnl, ec = int(r.get("qty") or 0), int(r.get("pnl_cents") or 0), r.get("entry_cents")
            if qty <= 0 or ec in (None, 0):
                continue
            rows.append({"city": _city(r.get("ticker", "")), "band": _band(int(ec)),
                         "qty": qty, "pnl": pnl, "win": 1 if pnl > 0 else 0})
    return rows


def _table(rows: list[dict], key: str, label: str) -> None:
    agg = defaultdict(lambda: {"n": 0, "qty": 0, "pnl": 0, "win": 0})
    for r in rows:
        a = agg[r[key]]
        a["n"] += 1; a["qty"] += r["qty"]; a["pnl"] += r["pnl"]; a["win"] += r["win"]
    print(f"{label:>8}  {'n':>4} {'ct':>5} {'WR':>5} {'totPnL$':>9} {'EV/ct':>7}")
    for k in sorted(agg, key=lambda x: -(agg[x]["pnl"] / max(agg[x]["qty"], 1))):
        a = agg[k]
        ev = a["pnl"] / a["qty"] if a["qty"] else 0
        wr = a["win"] / a["n"] if a["n"] else 0
        flag = " <<LOSS" if ev < 0 else ("  *" if ev > 12 else "")
        print(f"{k:>8}  {a['n']:>4} {a['qty']:>5} {wr:>4.0%} {a['pnl'] / 100:>+9.2f} {ev:>+6.1f}c{flag}")
    print()


def main() -> int:
    arms = sys.argv[1:] or DEFAULT_ARMS
    print("Premium edge decomposition — DESCRIPTIVE point estimates only (NO significance).")
    print("CAVEATS (validation 2026-06-28): (1) these EV/ct carry NO CI — the band/city sub-group")
    print("  differences are WITHIN NOISE at this sample (an event-clustered bootstrap of the diffs spans")
    print("  0); do NOT flip live config on them. (2) PAPER fills are OPTIMISTIC (the sim fills a maker")
    print("  only when the bid drifts toward the eventual winner) → paper EV overstates live. (3) arms with")
    print("  different offsets are DIFFERENT strategies — shown SEPARATELY here, never pooled. For any")
    print("  decision use bin/compare_variants.py --baseline (event-clustered, per-contract, multiplicity).\n")
    any_rows = False
    for arm in arms:
        rows = _load([arm])
        if not rows:
            print(f"================ {arm} — no settled trades ================\n")
            continue
        any_rows = True
        print(f"================ {arm}  ({len(rows)} settled trades) ================")
        _table(rows, "band", "BAND")
        _table(rows, "city", "CITY")
        tq = sum(r["qty"] for r in rows); tp = sum(r["pnl"] for r in rows); tw = sum(r["win"] for r in rows)
        print(f"  OVERALL: n={len(rows)} ct={tq} WR={tw / len(rows):.0%} "
              f"totPnL=${tp / 100:+.2f} EV/ct={tp / tq:+.1f}c  (point estimate, no CI)\n")
    print("'<<LOSS' / '*' flags are point estimates to TEST as paper A/B hypotheses, NOT established edges.")
    return 0 if any_rows else 1


if __name__ == "__main__":
    sys.exit(main())

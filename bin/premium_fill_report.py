#!/usr/bin/env python3
"""bin/premium_fill_report.py — honest execution/fill quality for a premium book.

The premium-capture edge is "significant but fill-bound" (bin/tail_edge.py). This
report answers: is execution eating the edge? For one book's state dir it shows:

  A. FILL FUNNEL (maker-lifecycle.jsonl): posted / replaced / filled / expired and
     the fill rate — the metric the settlement log can't give (it never sees misses).
  B. REALIZED EDGE of filled positions (settlement-log.jsonl), per price band, with
     a Wilson 95% CI — is the captured edge real or noise?
  C. MARKOUT (markout-log.jsonl): post-fill mid drift vs our fill price by age bucket
     — negative ⇒ the market moves against us right after we fill (adverse selection).
  D. SLIPPAGE sensitivity on the cheap band — break-even slippage and EV at +2/+5¢.

The true "filled vs unfilled-counterfactual" gap (do we only fill the losers?) needs
real fills — that's bin/reconcile_fills.py once live. Read-only; no Kalshi calls.

Usage:
  python3 bin/premium_fill_report.py                       # state/paper-premium
  python3 bin/premium_fill_report.py --state-dir state/paper
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))
from trader.orders import fee_per_contract_cents
from tail_edge import wilson  # reuse the exact Wilson CI used by the thesis script

BANDS = [(1, 15), (16, 25), (26, 40), (41, 60), (61, 99)]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _band(px) -> tuple | None:
    if px is None:
        return None
    for lo, hi in BANDS:
        if lo <= px <= hi:
            return (lo, hi)
    return None


def fill_funnel(lifecycle: list[dict]) -> None:
    by_event = defaultdict(int)
    posted_band = defaultdict(int)
    filled_band = defaultdict(int)
    for r in lifecycle:
        by_event[r.get("event")] += 1
        b = _band(r.get("limit_price_cents"))
        if r.get("event") == "posted" and b:
            posted_band[b] += 1
        if r.get("event") == "filled" and b:
            filled_band[b] += 1
    posted = by_event.get("posted", 0)
    filled = by_event.get("filled", 0)
    expired = by_event.get("expired", 0)
    resolved = filled + expired
    print("A. FILL FUNNEL (maker-lifecycle.jsonl):")
    if not lifecycle:
        print("   (no lifecycle log yet — runs accrue once the book posts/sweeps makers)\n")
        return
    print(f"   posted={posted}  replaced={by_event.get('replaced',0)}  "
          f"filled={filled}  expired={expired}  cancelled_dup={by_event.get('cancelled_dup',0)}")
    print(f"   fill rate (filled ÷ resolved) = {filled}/{resolved} = "
          f"{(filled/resolved*100 if resolved else 0):.1f}%  (dup-cancels excluded: already held)")
    print(f"   {'band':>8} {'posted':>7} {'filled':>7} {'fill%':>7}")
    for lo, hi in BANDS:
        p = posted_band[(lo, hi)]
        f = filled_band[(lo, hi)]
        if p or f:
            print(f"   {f'{lo}-{hi}':>8} {p:>7} {f:>7} {(f/p*100 if p else 0):>6.0f}%")
    print()


def realized_edge(settle: list[dict]) -> None:
    print("B. REALIZED EDGE of filled positions (settlement-log.jsonl):")
    if not settle:
        print("   (no settlements yet)\n")
        return
    win = lambda r: (r.get("pnl_cents") or 0) > 0
    print(f"   {'band':>8} {'n':>4} {'WR':>5} {'WR 95% CI':>12} {'px':>4} "
          f"{'EV/ct':>7} {'EV-fee':>7} {'real?':>6}")
    for lo, hi in BANDS:
        b = [r for r in settle if lo <= (r.get("entry_cents") or -1) <= hi]
        if not b:
            continue
        n = len(b)
        k = sum(win(r) for r in b)
        q = sum(r.get("qty", 0) for r in b) or 1
        pnl = sum(r.get("pnl_cents", 0) for r in b)
        px = sum(r.get("entry_cents", 0) for r in b) / n
        clo, chi = wilson(k, n)
        fee = sum(fee_per_contract_cents(r.get("qty", 1), r.get("entry_cents", 0), maker=True)
                  for r in b) / n
        evfee = pnl / q - fee
        real = "YES" if clo * 100 > px else "no"
        print(f"   {f'{lo}-{hi}':>8} {n:>4} {k/n*100:>4.0f}% {clo*100:>5.0f}-{chi*100:<4.0f}% "
              f"{px:>4.0f}c {pnl/q:>+6.1f}c {evfee:>+6.1f}c {real:>6}")
    print()


def markout(mk: list[dict]) -> None:
    print("C. MARKOUT — post-fill mid drift vs fill price (markout-log.jsonl):")
    if not mk:
        print("   (no markout log yet — populates as cmd_report runs on open positions)\n")
        return
    age_buckets = [("≤6h", 0, 6), ("6-18h", 6, 18), (">18h", 18, 1e9)]
    print(f"   {'band':>8} " + " ".join(f"{lab:>9}" for lab, _, _ in age_buckets))
    rows = [(r, _band(r.get("fill_px"))) for r in mk
            if r.get("cur_mid") is not None and r.get("source") != "sync_fill_detect"]  # skip at-fill diagnostic snaps
    for lo, hi in BANDS:
        cells = []
        any_data = False
        for _, a0, a1 in age_buckets:
            drifts = [r["cur_mid"] - r["fill_px"] for r, b in rows
                      if b == (lo, hi) and r.get("age_hours") is not None
                      and a0 <= r["age_hours"] < a1]
            if drifts:
                any_data = True
                cells.append(f"{sum(drifts)/len(drifts):>+8.1f}c")
            else:
                cells.append(f"{'—':>9}")
        if any_data:
            print(f"   {f'{lo}-{hi}':>8} " + " ".join(cells))
    print("   (negative ⇒ mid fell below our buy after fill = adverse selection)\n")


def slippage(settle: list[dict]) -> None:
    cheap = [r for r in settle if (r.get("entry_cents") or 99) <= 25]
    if not cheap:
        return
    cq = sum(r.get("qty", 0) for r in cheap) or 1
    cp = sum(r.get("pnl_cents", 0) for r in cheap)
    print("D. SLIPPAGE sensitivity (cheap band ≤25¢ — does the premium survive execution?):")
    print(f"   base EV/ct={cp/cq:+.2f}c → break-even slippage ≈ {cp/cq:.1f}c/contract")
    for s in (2, 5):
        print(f"   +{s}c slippage → EV/ct {(cp - s*cq)/cq:+.2f}c  (${(cp - s*cq)/100:+.2f})")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Premium execution/fill quality report.")
    ap.add_argument("--state-dir", default=os.environ.get(
        "KALSHI_WEATHER_STATE_DIR", str(ROOT / "state" / "paper-premium")))
    args = ap.parse_args()
    sd = Path(args.state_dir)
    if not sd.is_absolute():
        sd = ROOT / sd

    print(f"\nPremium fill report — {sd}\n{'='*60}")
    fill_funnel(_read_jsonl(sd / "maker-lifecycle.jsonl"))
    settle = _read_jsonl(sd / "settlement-log.jsonl")
    realized_edge(settle)
    markout(_read_jsonl(sd / "markout-log.jsonl"))
    slippage(settle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

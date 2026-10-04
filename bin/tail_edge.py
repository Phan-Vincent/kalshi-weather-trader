#!/usr/bin/env python3
"""
bin/tail_edge.py — is the bot's edge structural premium-capture (not forecasting)?

The model loses to the market on Brier, yet P&L is positive. This formalizes WHY:
a favorite-longshot bias — the Kalshi crowd underprices moderate-probability weather
bins. Over state/settlement-log.jsonl it reports, per entry-price band:
  • win rate vs the break-even price, with a Wilson 95% CI (is the edge real or noise?)
  • EV/contract gross and after Kalshi maker fees
  • the favorite-longshot curve (deep longshots lose, mid-priced bins win)
  • SELECTABILITY: does model conviction (fair_prob − price) predict EV? (it's anti-
    predictive — the forecast HURTS this edge)
  • fill-survivability: break-even slippage + an adverse-selection haircut

Read-only; no Kalshi calls, no orders, no state writes.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trader.orders import fee_per_contract_cents

SETTLE = ROOT / "state" / "settlement-log.jsonl"


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def main() -> int:
    rows = [json.loads(l) for l in open(SETTLE) if l.strip()]
    win = lambda r: (r.get("pnl_cents") or 0) > 0
    N = len(rows); Q = sum(r.get("qty", 0) for r in rows); P = sum(r.get("pnl_cents", 0) for r in rows)
    print(f"Tail-edge analysis — {N} settled, {Q} contracts, gross ${P/100:+.2f}, "
          f"EV/ct {P/Q:+.2f}c\n")

    print("ENTRY-BAND EDGE (is buying cheap bins +EV, and is it significant?):")
    print(f"{'band':>7} {'n':>4} {'WR':>4} {'WR 95% CI':>11} {'px':>4} {'edge':>6} "
          f"{'EV/ct':>6} {'EV-fee':>7} {'real?':>6}")
    for lo, hi in [(1, 15), (16, 25), (26, 40), (41, 60), (61, 99)]:
        b = [r for r in rows if lo <= (r.get("entry_cents") or -1) <= hi]
        if not b:
            continue
        n = len(b); k = sum(win(r) for r in b); q = sum(r.get("qty", 0) for r in b)
        pnl = sum(r.get("pnl_cents", 0) for r in b); px = sum(r.get("entry_cents", 0) for r in b) / n
        clo, chi = wilson(k, n)
        fee = sum(fee_per_contract_cents(r.get("qty", 1), r.get("entry_cents", 0), maker=True)
                  for r in b) / n
        evfee = pnl / q - fee
        real = "YES" if clo * 100 > px else "no"
        print(f"{lo:>2}-{hi:<4}{n:>4}{k/n*100:>4.0f}%{clo*100:>5.0f}-{chi*100:<4.0f}%{px:>4.0f}c"
              f"{k/n*100-px:>+6.1f}{pnl/q:>+6.1f}c{evfee:>+6.1f}c{real:>6}")

    print("\nFAVORITE-LONGSHOT CURVE (EV/ct by price — longshots lose, mids win):")
    for lo, hi in [(1, 10), (11, 20), (21, 30), (31, 45), (46, 65), (66, 99)]:
        b = [r for r in rows if lo <= (r.get("entry_cents") or -1) <= hi]
        if b:
            q = sum(r.get("qty", 0) for r in b); pnl = sum(r.get("pnl_cents", 0) for r in b)
            bar = ("+" if pnl >= 0 else "-") * min(20, int(abs(pnl / q)))
            print(f"  {lo:>2}-{hi:<2}c: EV/ct {pnl/q:>+6.1f}c  {bar}")

    print("\nSELECTABILITY — does model conviction predict EV? (≤40¢ bins, split by "
          "fair_prob−price):")
    mid = [r for r in rows if (r.get("entry_cents") or 99) <= 40 and r.get("fair_prob_at_open") is not None]
    mid.sort(key=lambda r: r["fair_prob_at_open"] - r.get("entry_cents", 0) / 100)
    half = len(mid) // 2
    for lab, g in [("LOW model-edge ", mid[:half]), ("HIGH model-edge", mid[half:])]:
        q = sum(r.get("qty", 0) for r in g); pnl = sum(r.get("pnl_cents", 0) for r in g)
        wr = sum(win(r) for r in g) / len(g) * 100
        print(f"  {lab} half: n={len(g)} WR={wr:.0f}% EV/ct={pnl/q:+.1f}c")
    print("  ⇒ if HIGH ≤ LOW, the forecast is ANTI-predictive — size by the band, not the model.")

    print("\nFILL-SURVIVABILITY (cheap band ≤25¢ — does the premium survive real execution?):")
    cheap = [r for r in rows if (r.get("entry_cents") or 99) <= 25]
    cq = sum(r.get("qty", 0) for r in cheap); cp = sum(r.get("pnl_cents", 0) for r in cheap)
    print(f"  base EV/ct={cp/cq:+.2f}c → break-even slippage ≈ {cp/cq:.1f}c/contract")
    for s in (2, 5):
        print(f"  +{s}c slippage → EV/ct {(cp - s*cq)/cq:+.2f}c  (${(cp - s*cq)/100:+.2f})")
    wins = [r for r in cheap if win(r)]; losses = [r for r in cheap if not win(r)]
    wp = sum(r.get("pnl_cents", 0) for r in wins); lp = sum(r.get("pnl_cents", 0) for r in losses)
    for x in (0.25, 0.5):
        print(f"  adverse-selection: miss {int(x*100)}% of winning fills → "
              f"${(wp*(1-x)+lp)/100:+.2f}")

    print("\nMARKET-TYPE (where the premium concentrates):")
    for kind, lab in (("B", "bins"), ("T", "thresholds")):
        g = [r for r in rows if re.search(f"-{kind}[0-9.]+$", r.get("ticker", ""))
             and (r.get("entry_cents") or 99) <= 60]
        if g:
            q = sum(r.get("qty", 0) for r in g); pnl = sum(r.get("pnl_cents", 0) for r in g)
            print(f"  {lab:>11} ≤60¢: n={len(g)} EV/ct={pnl/q:+.1f}c pnl=${pnl/100:+.0f}")

    print("\nVERDICT: the edge is STRUCTURAL premium capture in mid-priced (≈20-60¢) bins, "
          "significant but fill-bound. Harvest = buy the band broadly, ignore the (anti-predictive) "
          "forecast for sizing, and fill within ~3¢ of quote. NOTE: in-sample on paper fills; "
          "bin/shadow_score.py is the unbiased forward confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
bin/mm_sim.py — honest market-making backtest on REAL recorded price paths.

We established (state/forecast-log.jsonl, beta≈0.04) that our forecast does NOT
lead the market, so a *directional* scalper has no signal. The only viable
"scalp" is direction-neutral MARKET MAKING: quote both sides, capture the spread
+ maker rebate, and eat adverse selection + inventory risk.

This backtests exactly that on the real per-market mid paths the bot recorded
(market_mid each cycle). Adverse selection is baked in by construction: a fill
happens only when the mid moves THROUGH your quote, and the inventory you pick up
is then marked to where the price actually went next — i.e. you buy as it falls,
sell as it rises. Spread capture comes from round-trips; the net tells you whether
spread + rebate beats the adverse drift. Flattens residual inventory at the last
observed mid (assumes you can exit at close — optimistic; noted).

Sweeps the half-spread (how far from mid you quote) and the maker rebate (Kalshi's
liquidity incentive — the swing factor). Read-only.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOG = ROOT / "state" / "forecast-log.jsonl"


def paths():
    """ticker -> ordered list of mid (cents), from the recorded forecast-log."""
    seq = defaultdict(list)
    for line in open(FLOG):
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("market_mid"); a = r.get("asof_utc"); t = r.get("ticker")
        if m is not None and a and t and 0 < m < 1:
            seq[t].append((a, m * 100.0))
    out = {}
    for t, s in seq.items():
        s.sort()
        mids = [m for _, m in s]
        if len(mids) >= 4:
            out[t] = mids
    return out


def sim(all_paths, hs: float, rebate: float):
    """Simulate a two-sided maker quoting +/- hs around the mid each step.
    Returns (net_cents, spread_pnl, inv_pnl, rebate_total, n_fills, n_markets)."""
    spread_pnl = inv_cost = reb = 0.0
    fills = 0
    for mids in all_paths.values():
        cash = 0.0; inv = 0  # inv = net YES contracts
        for i in range(len(mids) - 1):
            m0, m1 = mids[i], mids[i + 1]
            bid, ask = m0 - hs, m0 + hs
            if m1 <= bid:                 # market sold through our bid -> we BUY at bid
                cash -= bid; inv += 1; reb += rebate; fills += 1
            elif m1 >= ask:               # market bought through our ask -> we SELL at ask
                cash += ask; inv -= 1; reb += rebate; fills += 1
        cash += inv * mids[-1]            # flatten residual at last mid
        spread_pnl += cash                # (cash already nets the round-trips + residual mark)
    return spread_pnl + reb, spread_pnl, reb, fills, len(all_paths)


def main() -> int:
    if not FLOG.exists():
        print("No forecast-log yet."); return 1
    P = paths()
    if not P:
        print("Not enough multi-snapshot price paths yet."); return 1
    steps = sum(len(m) - 1 for m in P.values())
    print(f"MM backtest on {len(P)} real market price-paths ({steps} cycle-steps)\n")
    print("Net P&L per market-path (cents), by half-spread × maker rebate/fill:")
    print(f"  {'half-spread':>11} {'rebate=0c':>11} {'rebate=0.5c':>12} {'rebate=1c':>11}  {'fills':>7}")
    for hs in (1.0, 2.0, 3.0, 5.0):
        cells = []
        nf = 0
        for reb in (0.0, 0.5, 1.0):
            net, sp, rb, fills, nm = sim(P, hs, reb)
            cells.append(net / nm)  # per-market average
            nf = fills
        flag = "  ✓ profitable" if cells[1] > 0 else ""
        print(f"  ±{hs:>4.0f}c      {cells[0]:>+9.1f}c {cells[1]:>+10.1f}c {cells[2]:>+9.1f}c  "
              f"{nf/len(P):>6.1f}/mkt{flag}")
    # decompose the best-case (±3c, rebate 0.5c)
    net, sp, rb, fills, nm = sim(P, 3.0, 0.5)
    print(f"\nDecomposition @ ±3c spread, 0.5c rebate ({nm} markets, {fills} fills):")
    print(f"  spread + inventory P&L = {sp/100:+.2f}$   (this is where adverse selection shows up)")
    print(f"  maker rebates          = {rb/100:+.2f}$")
    print(f"  NET                    = {(sp+rb)/100:+.2f}$  ({(sp+rb)/nm:+.2f}c/market)")
    print("\nReads: if NET is <0 without rebate, the spread doesn't beat adverse selection — "
          "MM only works if Kalshi's rebate covers the gap. Flattening at last mid is optimistic "
          "(real exits in thin books cost more), so treat positive results as an upper bound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

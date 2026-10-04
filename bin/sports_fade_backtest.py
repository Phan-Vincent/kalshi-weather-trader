#!/usr/bin/env python3
"""bin/sports_fade_backtest.py — the last deployable Kalshi edge test: fade retail longshot-overbetting.

The platform-wide scan (2026-07-14) found NO retail-algo-beatable market at deployable size — every
category long-shot or dead. Sports (~95% of volume) was ranked "least-dead" not because its edge is
plausible but because it's the ONLY front that is BOTH zero-capital testable at high power AND deployable
if it improbably passed. This runs that definitive $0 test.

THE STRATEGY: retail chronically over-buys cheap YES longshots (favorite-longshot bias). Fade it — be the
MAKER selling YES (buying NO) to those retail YES-buyers. Realized fade P&L per contract on a retail
YES-buy at price p = (p − outcome_yes) − maker_fee. If cheap YES contracts resolve YES LESS often than
their price implies (by more than fees), the fade is +EV.

HONEST FILL MODEL (the weather-leak trap, avoided): we fill on EVERY retail YES-buy trade (taker_side=yes)
at p ≤ threshold — we do NOT cherry-pick. That deliberately eats the adverse-selection tail (we're also
the counterparty when the longshot HITS). It overstates VOLUME (you can't be counterparty to 100% of
flow) but gives the honest per-contract EV including adverse selection — which is exactly the question.
Event-clustered (per settled market) BCa CI. Scores on real settled outcomes. Read-only, no capital.

KILL: net-of-fee fade EV ≤ 0 (the surplus is already captured by the incumbent MMs / it's efficiently
priced). Expected outcome per the scan: dies like weather. A pass would be the one tradeable edge found.

Usage:
    python3 bin/sports_fade_backtest.py --max-markets 400 --threshold 0.30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from math import ceil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from compare_variants import cluster_bootstrap_ci  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PRICE_BUCKETS = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25), (0.25, 0.30)]


# ── pure logic (unit-tested) ─────────────────────────────────────────────────────────────────────

def taker_fee_cents(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return float(ceil(0.07 * p * (1.0 - p) * 100)) if 0 < p < 1 else 0.0


def maker_fee_cents(p: float) -> float:
    """Kalshi maker fee = 25% of the taker fee (post-2026-07-07 schedule)."""
    return 0.25 * taker_fee_cents(p)


def fade_pnl_cents(yes_price: float, outcome_yes: int) -> float:
    """Per-contract P&L of the fader who SOLD YES at yes_price to a retail buyer.
    = (premium kept − payout) − maker fee = (yes_price − outcome_yes)·100 − maker_fee."""
    return (yes_price - outcome_yes) * 100.0 - maker_fee_cents(yes_price)


def _bucket(p: float):
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return f"{int(lo*100)}-{int(hi*100)}c"
    return None


# ── data ─────────────────────────────────────────────────────────────────────────────────────────

def _get(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kwb-fade/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.5 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0); continue
    return None


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def sports_series_set() -> set:
    d = _get(f"{KALSHI}/series/?category={urllib.parse.quote('Sports')}")
    return {s.get("ticker") for s in (d or {}).get("series", [])}


def collect_settled_sports(sports: set, max_markets: int, min_vol: float, log=print,
                           max_series: int = 900) -> list[dict]:
    """Iterate sports SERIES and pull each one's SETTLED markets directly (real game markets), instead of
    the generic settled feed which is dominated by dead zero-volume parlay junk. Keep real-volume markets
    with a known yes/no result. Iterates series until max_markets is reached or max_series scanned."""
    out, scanned = [], 0
    for ser in sorted(sports):
        if len(out) >= max_markets or scanned >= max_series:
            break
        scanned += 1
        cursor = ""
        for _pg in range(2):
            q = f"{KALSHI}/markets?series_ticker={ser}&status=settled&limit=1000"
            if cursor:
                q += f"&cursor={cursor}"
            d = _get(q)
            time.sleep(0.12)
            if not d:
                break
            for m in d.get("markets", []):
                res = m.get("result")
                if res not in ("yes", "no") or (_num(m.get("volume_fp")) or 0) < min_vol:
                    continue
                out.append({"ticker": m["ticker"], "outcome_yes": 1 if res == "yes" else 0})
                if len(out) >= max_markets:
                    break
            cursor = d.get("cursor") or ""
            if not cursor or len(out) >= max_markets:
                break
        if scanned % 100 == 0:
            log(f"  … scanned {scanned} series, {len(out)} settled sports markets so far")
    return out


def fetch_longshot_yes_buys(ticker: str, threshold: float, max_pages: int = 4) -> list[tuple]:
    """Retail YES-buy trades (taker_side='yes') at yes_price ≤ threshold → [(yes_price, count)]."""
    out, cursor, pages = [], "", 0
    while pages < max_pages:
        q = f"{KALSHI}/markets/trades?ticker={ticker}&limit=1000"
        if cursor:
            q += f"&cursor={cursor}"
        d = _get(q)
        if not d:
            break
        for t in d.get("trades", []):
            if t.get("taker_side") != "yes":
                continue
            p = _num(t.get("yes_price_dollars"))
            c = _num(t.get("count_fp")) or 0.0
            if p is not None and 0 < p <= threshold and c > 0:
                out.append((p, c))
        cursor = d.get("cursor") or ""
        pages += 1
        if not cursor:
            break
    return out


def run(max_markets=400, threshold=0.30, min_vol=50.0, log=print) -> dict:
    sports = sports_series_set()
    if not sports:
        return {"error": "could not fetch sports series"}
    log(f"[fade] {len(sports)} sports series; collecting up to {max_markets} settled sports markets (vol≥{min_vol})…")
    markets = collect_settled_sports(sports, max_markets, min_vol, log)
    log(f"[fade] collected {len(markets)} settled sports markets; fetching retail longshot YES-buys…")

    pnl_items, bucket = [], defaultdict(lambda: {"ct": 0.0, "yes": 0.0, "px_ct": 0.0})
    tot_ct = tot_pnl = 0.0
    n_mkt_with_trades = 0
    for i, m in enumerate(markets):
        trades = fetch_longshot_yes_buys(m["ticker"], threshold)
        time.sleep(0.2)
        if trades:
            n_mkt_with_trades += 1
        for p, c in trades:
            pnl = fade_pnl_cents(p, m["outcome_yes"])
            pnl_items.append((m["ticker"], pnl))          # per-CONTRACT-group; clustered by market
            tot_ct += c
            tot_pnl += pnl * c
            b = _bucket(p)
            if b:
                bk = bucket[b]
                bk["ct"] += c; bk["yes"] += m["outcome_yes"] * c; bk["px_ct"] += p * c
        if (i + 1) % 50 == 0:
            log(f"  … {i+1}/{len(markets)} markets, {int(tot_ct)} longshot contracts")

    net_cpc = (tot_pnl / tot_ct) if tot_ct else None
    win_rate = None
    if tot_ct:
        # fader wins when the longshot MISSES (outcome_yes=0)
        miss_ct = sum(bk["ct"] - bk["yes"] for bk in bucket.values())
        win_rate = miss_ct / tot_ct
    calib = []
    for lo, hi in PRICE_BUCKETS:
        b = f"{int(lo*100)}-{int(hi*100)}c"
        bk = bucket.get(b)
        if bk and bk["ct"] > 0:
            calib.append({"bucket": b, "contracts": round(bk["ct"], 1),
                          "avg_price": round(bk["px_ct"] / bk["ct"], 4),
                          "realized_yes": round(bk["yes"] / bk["ct"], 4)})
    return {"markets": len(markets), "markets_with_longshots": n_mkt_with_trades,
            "longshot_contracts": round(tot_ct, 1), "n_trade_groups": len(pnl_items),
            "net_fade_cpc": round(net_cpc, 3) if net_cpc is not None else None,
            "fader_win_rate": round(win_rate, 4) if win_rate is not None else None,
            "ci_cpc": cluster_bootstrap_ci(pnl_items) if pnl_items else (None, None, None),
            "calibration": calib, "verdict": _verdict(pnl_items, net_cpc)}


def _verdict(pnl_items, net_cpc) -> str:
    if not pnl_items or net_cpc is None:
        return "NO DATA — no retail longshot YES-buys collected"
    ci = cluster_bootstrap_ci(pnl_items)
    lo, hi = ci[1], ci[2]
    if lo is not None and lo > 0:
        return (f"EDGE FOUND — fading retail longshots nets {net_cpc:+.2f}¢/ct, event-clustered CI "
                f"[{lo:+.2f},{hi:+.2f}] EXCLUDES 0. The one deployable Kalshi edge; verify fills + size, "
                f"then paper-trade before capital.")
    if hi is not None and hi < 0:
        return (f"DEAD (worse than break-even) — the fade LOSES {net_cpc:+.2f}¢/ct [{lo:+.2f},{hi:+.2f}]; "
                f"cheap YES is fairly/under-priced or adverse selection dominates. Kalshi has no deployable "
                f"retail edge — platform fully picked clean.")
    return (f"DEAD (within noise) — fade EV {net_cpc:+.2f}¢/ct, CI [{lo:+.2f},{hi:+.2f}] spans 0: no "
            f"exploitable favorite-longshot surplus after fees. Kalshi platform fully picked clean.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fade retail longshot-overbetting on Kalshi sports (backtest)")
    ap.add_argument("--max-markets", type=int, default=400)
    ap.add_argument("--threshold", type=float, default=0.30, help="max YES price counted as a longshot")
    ap.add_argument("--min-vol", type=float, default=50.0, help="min market volume to include")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run(args.max_markets, args.threshold, args.min_vol)
    if "error" in r:
        print(f"[fade] {r['error']}"); return 1
    if args.json:
        print(json.dumps(r, default=str)); return 0

    def ci(c): return "n/a" if not c or c[0] is None else f"{c[0]:+.2f} [{c[1]:+.2f}, {c[2]:+.2f}]"
    print(f"\n[fade] {r['markets']} settled sports markets, {r['markets_with_longshots']} with retail "
          f"longshot YES-buys → {r['longshot_contracts']:.0f} contracts over {r['n_trade_groups']} trade groups")
    print(f"  net fade EV: {r['net_fade_cpc']:+.2f}¢/ct  (event-clustered CI {ci(r['ci_cpc'])})   "
          f"fader win-rate {r['fader_win_rate']:.1%}")
    print("\n  CALIBRATION (favorite-longshot bias — realized_yes < avg_price ⇒ overpriced ⇒ fade +EV):")
    print(f"    {'bucket':>8} {'contracts':>10} {'avg_price':>10} {'realized_yes':>13}  {'over/under':>10}")
    for c in r["calibration"]:
        gap = c["avg_price"] - c["realized_yes"]
        print(f"    {c['bucket']:>8} {c['contracts']:>10.0f} {c['avg_price']:>10.4f} {c['realized_yes']:>13.4f}  "
              f"{gap:>+10.4f}")
    print(f"\n  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

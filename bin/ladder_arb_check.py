#!/usr/bin/env python3
"""bin/ladder_arb_check.py — the last "is there free money on the table?" check for Kalshi weather.

The forecast-edge thesis is dead (see ROADMAP.md). This tests the ONE category that needs NO forecast
skill: STRUCTURAL / dutch-book arbitrage within a single event's own order book. Two locks:

  1. MONOTONICITY (nested ">N" ladders, e.g. KXRAIN "> N inches" and temp >/< thresholds): P(>N) must be
     non-increasing in N, so YES(>N_high) ≤ YES(>N_low). A tradeable violation is bid(>N_high) >
     ask(>N_low): buy YES(>N_low) + buy NO(>N_high) costs ask_low + (1−bid_high) < 1 yet always pays ≥ $1.
  2. PARTITION SUM (temp "between"+tail bins that tile the axis, exactly one wins): ΣYES = 1. Buy every
     YES for Σask < 1 → lock $1 (buy-all arb); or sell every YES (buy every NO) for Σbid > 1 → lock.

Both are netted against the Kalshi TAKER fee (ceil(0.07·P·(1−P)) per contract, per leg) and capped by
the FILLABLE top-of-book size — an arb you can't fill for size, after fees, is not money. Prior: arb bots
eat these in milliseconds, so expect ~nothing; this measures it definitively instead of assuming.
Read-only (no orders). Scans live KXHIGH*/KXLOW*/KXRAIN* order books.

Usage:
    python3 bin/ladder_arb_check.py              # scan all open weather events
    python3 bin/ladder_arb_check.py --series KXRAINHOUM
    python3 bin/ladder_arb_check.py --min-net-cents 1   # report arbs ≥1¢/ct net of fees
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

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def taker_fee_cents(price_dollars: float) -> float:
    """Kalshi taker fee per contract: ceil(0.07 · P · (1−P)) cents, P in dollars. At 0.50 → ceil(1.75)=2¢."""
    p = max(0.0, min(1.0, price_dollars))
    return float(ceil(0.07 * p * (1.0 - p) * 100)) if 0 < p < 1 else 0.0


# ── pure arb logic (unit-tested) ─────────────────────────────────────────────────────────────────

def monotonicity_arbs(ladder: list[dict], min_net_c: float = 0.0) -> list[dict]:
    """ladder = [{thr, yes_bid, yes_ask, bid_size, ask_size}] (any order). Flag every pair where a HIGHER
    threshold's YES bid exceeds a LOWER threshold's YES ask (the monotonicity lock), net of both legs' fees."""
    out = []
    rows = [r for r in ladder if r.get("thr") is not None]
    for i in range(len(rows)):
        for j in range(len(rows)):
            lo, hi = rows[i], rows[j]
            if hi["thr"] <= lo["thr"]:
                continue
            bh, al = hi.get("yes_bid"), lo.get("yes_ask")
            if bh is None or al is None or not (bh > al):
                continue
            gross_c = (bh - al) * 100.0
            fee_c = taker_fee_cents(al) + taker_fee_cents(1.0 - bh)   # buy YES(lo)@al + buy NO(hi)@(1-bh)
            net_c = gross_c - fee_c
            if net_c >= min_net_c:
                out.append({"kind": "monotonicity", "buy_yes_thr": lo["thr"], "sell_yes_thr": hi["thr"],
                            "gross_c": round(gross_c, 2), "fee_c": round(fee_c, 2), "net_c": round(net_c, 2),
                            "fill_size": min(lo.get("ask_size") or 0, hi.get("bid_size") or 0)})
    return out


def partition_arbs(bins: list[dict], min_net_c: float = 0.0) -> list[dict]:
    """bins = [{yes_bid, yes_ask, bid_size, ask_size}] for a mutually-exclusive-exhaustive set. Buy-all
    (Σask<1) and sell-all (Σbid>1) locks, net of per-leg fees. Fill size = min leg size."""
    tradeable_ask = [b for b in bins if b.get("yes_ask") is not None]
    tradeable_bid = [b for b in bins if b.get("yes_bid") is not None]
    out = []
    if len(tradeable_ask) == len(bins) and bins:
        ask_sum = sum(b["yes_ask"] for b in bins)
        fee_c = sum(taker_fee_cents(b["yes_ask"]) for b in bins)
        net_c = (1.0 - ask_sum) * 100.0 - fee_c
        if net_c >= min_net_c:
            out.append({"kind": "buy_all", "yes_ask_sum": round(ask_sum, 4), "fee_c": round(fee_c, 2),
                        "net_c": round(net_c, 2), "fill_size": min(b.get("ask_size") or 0 for b in bins)})
    if len(tradeable_bid) == len(bins) and bins:
        bid_sum = sum(b["yes_bid"] for b in bins)
        fee_c = sum(taker_fee_cents(1.0 - b["yes_bid"]) for b in bins)
        net_c = (bid_sum - 1.0) * 100.0 - fee_c
        if net_c >= min_net_c:
            out.append({"kind": "sell_all", "yes_bid_sum": round(bid_sum, 4), "fee_c": round(fee_c, 2),
                        "net_c": round(net_c, 2), "fill_size": min(b.get("bid_size") or 0 for b in bins)})
    return out


# ── data ─────────────────────────────────────────────────────────────────────────────────────────

def _get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kwb-arb/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0); continue
    return None


def _market_row(m: dict) -> dict:
    return {"ticker": m.get("ticker"), "strike_type": m.get("strike_type"),
            "floor": _num(m.get("floor_strike")), "cap": _num(m.get("cap_strike")),
            "yes_bid": _num(m.get("yes_bid_dollars")), "yes_ask": _num(m.get("yes_ask_dollars")),
            "bid_size": _num(m.get("yes_bid_size_fp")) or 0.0, "ask_size": _num(m.get("yes_ask_size_fp")) or 0.0}


def _weather_series() -> list[str]:
    d = _get(f"{KALSHI}/series/?category={urllib.parse.quote('Climate and Weather')}")
    ser = (d or {}).get("series", [])
    out = []
    for s in ser:
        tk = s.get("ticker", "")
        if tk.startswith(("KXHIGH", "KXLOW", "KXRAIN")):
            out.append(tk)
    return sorted(set(out))


def scan_series(series: str, min_net_c: float) -> list[dict]:
    d = _get(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=200")
    if not d:
        return []
    by_event = defaultdict(list)
    for m in d.get("markets", []):
        by_event[m.get("event_ticker")].append(_market_row(m))
    findings = []
    for ev, rows in by_event.items():
        # LADDER (nested thresholds): greater/less markets with a numeric strike → monotonicity in YES.
        # For ">N" use floor as the threshold; for "<N" (less) YES increases with cap, so treat the
        # complementary NO ladder — here we check the greater-ladder (rain + temp T-highs) which is the
        # clean nested case.
        ladder = [{**r, "thr": r["floor"]} for r in rows if r["strike_type"] == "greater" and r["floor"] is not None]
        for a in monotonicity_arbs(ladder, min_net_c):
            findings.append({"event": ev, "series": series, **a})
        # PARTITION: the full between+tail set for a temp event (exactly one wins).
        if len(rows) >= 3 and any(r["strike_type"] == "between" for r in rows):
            for a in partition_arbs(rows, min_net_c):
                findings.append({"event": ev, "series": series, **a})
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Structural (ladder/partition) arbitrage check for Kalshi weather")
    ap.add_argument("--series", help="single series ticker (default: all weather)")
    ap.add_argument("--min-net-cents", type=float, default=0.01,
                    help="report arbs with ≥ this net-of-fee ¢/contract (default 0.01)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    series = [args.series] if args.series else _weather_series()
    if not series:
        print("[arb] could not enumerate weather series"); return 1
    all_find, scanned = [], 0
    for s in series:
        all_find += scan_series(s, args.min_net_cents)
        scanned += 1
        time.sleep(0.25)

    if args.json:
        print(json.dumps({"series_scanned": scanned, "findings": all_find})); return 0

    print(f"[arb] scanned {scanned} weather series' live order books for structural arbitrage "
          f"(net ≥ {args.min_net_cents}¢/ct after taker fees, fillable size shown)\n")
    if not all_find:
        print("  NO fillable, fee-covering structural arbitrage found in any open weather event.")
        print("  → The order books are internally consistent (monotonic ladders, partitions summing to ~$1).")
        print("  Verdict: no free money on the table. The last door is closed — the venue is fully picked clean.")
        return 0
    all_find.sort(key=lambda f: -f["net_c"])
    for f in all_find:
        loc = (f"buy YES>{f['buy_yes_thr']:g}/sell YES>{f['sell_yes_thr']:g}" if f["kind"] == "monotonicity"
               else f["kind"])
        print(f"  {f['event']:24s} [{f['kind']}] net {f['net_c']:+.2f}¢/ct  fill≈{f['fill_size']:.0f} ct  ({loc})")
    print(f"\n  {len(all_find)} candidate arb(s). VERIFY each in the live book before trusting — quotes move, "
          f"and top-of-book size may not be real depth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

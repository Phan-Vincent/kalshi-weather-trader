#!/usr/bin/env python3
"""bin/discretionary.py — point the trading machinery at YOUR discretionary calls (starter).

The weather forecast edge is dead (see ROADMAP.md). The one edge that ever made money was a MANUAL call
(the KXUSAIRANAGREEMENT NO bet, +$42 unrealized). This tool reuses the repo's execution-grade machinery —
Kelly sizing (trader/orders.kelly_qty), post-fee edge (compute_post_fee_edge), and the event-clustered
bootstrap CI (compare_variants.cluster_bootstrap_ci) — to LOG, SIZE, and honestly GRADE your discretionary
bets. It does NOT place orders and does NOT pick trades — you decide and execute on Kalshi; this records
your decision-time probability, suggests a size, and later tells you the truth about whether your calls
have edge, with the SAME rigor that killed the weather model.

  log    — record a decision at DECISION TIME: your P(win) + thesis; fetch the live market; show the edge
           vs the market, the post-fee economics, and a quarter-Kelly suggested size. Writes to the journal.
  list   — show logged calls and their current settlement status.
  score  — for settled calls: Brier skill vs the market (do your probs beat the price?), calibration (when
           you say 70%, do you win ~70%?), realized P&L, event-clustered CI, and a pre-registered futility
           verdict. This is the whole point: an un-foolable, running answer to "do I actually have edge?"

HONESTY BUILT IN: Kelly assumes your probability is CALIBRATED. Until `score` shows your calls beat the
market over enough settled bets, treat the suggested size as an UPPER BOUND and go smaller — you are
betting on unproven skill. Discretionary bets are few and settle slowly, so a confirmed edge can take
years; the futility gate says exactly how unproven it still is. Read-only w.r.t. Kalshi (GETs only).

Usage:
    python3 bin/discretionary.py log --ticker KXUSAIRANAGREEMENT-27-26SEP --side no --prob 0.85 \
            --thesis "no deal by Sep; talks stalled" --bankroll 500
    python3 bin/discretionary.py list
    python3 bin/discretionary.py score
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.orders import kelly_qty, _kalshi_taker_fee_cents  # noqa: E402  (reuse the sizing + fee machinery)
from compare_variants import cluster_bootstrap_ci  # noqa: E402  (reuse the event-clustered CI)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
STATE = ROOT / "state" / "discretionary"
JOURNAL = STATE / "journal.jsonl"
MIN_CALLS_FOR_VERDICT = 20   # pre-registered: below this, no edge ruling — you're still gambling


def _get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kwb-disc/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0); continue
    return None


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


# ── pure logic (unit-tested) ─────────────────────────────────────────────────────────────────────

def analyze_entry(your_prob: float, side: str, yes_bid: float, yes_ask: float) -> dict:
    """What you'd pay for `side`, the market's implied prob for that side, and your edge over it.
    yes_bid/yes_ask in dollars (0-1). Taker economics: you cross the spread to enter."""
    mid = (yes_bid + yes_ask) / 2.0
    if side == "yes":
        entry, implied = yes_ask, mid
    else:  # no — buy NO at (1 − yes_bid)
        entry, implied = 1.0 - yes_bid, 1.0 - mid
    return {"entry_dollars": round(entry, 4), "market_implied": round(implied, 4),
            "edge_prob": round(your_prob - implied, 4)}


def outcome_for_side(result: str, side: str) -> float | None:
    """1.0 if your side won, 0.0 if it lost, None if not a yes/no settlement."""
    if result not in ("yes", "no"):
        return None
    return 1.0 if result == side else 0.0


def brier_skill_items(scored: list[dict]):
    """Paired per-call (your_brier, market_brier) → [(event, market_b − your_b)] (>0 ⇒ you beat market)."""
    items = []
    for r in scored:
        y, imp, out = r.get("your_prob"), r.get("market_implied"), r.get("_outcome")
        if y is None or imp is None or out is None:
            continue
        items.append((r.get("ticker", r.get("event", "?")), (imp - out) ** 2 - (y - out) ** 2))
    return items


def calibration_table(scored: list[dict], edges=(0.0, 0.5, 0.7, 0.9, 1.01)):
    """Bucket calls by your_prob; report predicted vs realized win rate per bucket."""
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        grp = [r for r in scored if r.get("your_prob") is not None and lo <= r["your_prob"] < hi
               and r.get("_outcome") is not None]
        if grp:
            pred = sum(r["your_prob"] for r in grp) / len(grp)
            real = sum(r["_outcome"] for r in grp) / len(grp)
            buckets.append({"range": f"{lo:.0%}-{hi:.0%}", "n": len(grp),
                            "you_said": round(pred, 3), "you_won": round(real, 3)})
    return buckets


def _verdict(skill_items, n_calls: int) -> str:
    if n_calls < MIN_CALLS_FOR_VERDICT:
        return (f"TOO EARLY — {n_calls} settled call(s); need ≥{MIN_CALLS_FOR_VERDICT} before any edge "
                f"ruling. Until then you are betting on UNPROVEN skill — keep size conservative.")
    ci = cluster_bootstrap_ci(skill_items)
    pt, lo, hi = ci
    if lo is not None and lo > 0:
        return (f"EDGE DETECTED — your probabilities beat the market (Brier skill {pt:+.4f} [{lo:+.4f},"
                f"{hi:+.4f}], excludes 0). Real, but keep accruing; size up only gradually.")
    if hi is not None and hi < 0:
        return (f"NEGATIVE — your calls are WORSE than the market (skill {pt:+.4f} [{lo:+.4f},{hi:+.4f}]). "
                f"Take the market price or don't trade; do not size up on conviction.")
    return (f"NO PROVEN EDGE — Brier skill {pt:+.4f} [{lo:+.4f},{hi:+.4f}] spans 0 over {n_calls} calls. "
            f"Consistent with luck, not skill. Stay small until the CI clears 0.")


# ── network: market snapshot + settlement ─────────────────────────────────────────────────────────

def fetch_market(ticker: str) -> dict | None:
    d = _get(f"{KALSHI}/markets/{ticker}")
    m = (d or {}).get("market")
    if not m:
        return None
    return {"yes_bid": _num(m.get("yes_bid_dollars")), "yes_ask": _num(m.get("yes_ask_dollars")),
            "status": m.get("status"), "result": m.get("result") or "",
            "title": m.get("title", ""), "close_time": m.get("close_time")}


# ── commands ───────────────────────────────────────────────────────────────────────────────────────

def cmd_log(args) -> int:
    m = fetch_market(args.ticker)
    if not m or m["yes_bid"] is None or m["yes_ask"] is None:
        print(f"[disc] no two-sided market for {args.ticker}"); return 1
    side = args.side.lower()
    a = analyze_entry(args.prob, side, m["yes_bid"], m["yes_ask"])
    entry_c = max(1, min(99, round(a["entry_dollars"] * 100)))
    bank_c = int(args.bankroll * 100)
    qty = kelly_qty(args.prob, entry_c, bank_c, fraction=args.fraction,
                    per_event_cap_cents=int(args.per_trade * 100),
                    per_trade_cap_cents=int(args.per_trade * 100), max_bankroll_pct=args.max_pct)
    fee_c = _kalshi_taker_fee_cents(max(1, qty), entry_c) / max(1, qty)
    rec = {"ts_utc": datetime.now(timezone.utc).isoformat(), "ticker": args.ticker, "side": side,
           "your_prob": args.prob, "market_yes_bid": m["yes_bid"], "market_yes_ask": m["yes_ask"],
           "entry_cents": entry_c, "market_implied": a["market_implied"], "edge_prob": a["edge_prob"],
           "fee_cents_per_ct": round(fee_c, 2), "suggested_qty": qty, "bankroll_dollars": args.bankroll,
           "fraction": args.fraction, "thesis": args.thesis, "status": "logged", "title": m["title"]}
    print(f"[disc] {args.ticker}  side={side}  '{m['title'][:60]}'")
    print(f"  your P({side})={args.prob:.2f}  market implied={a['market_implied']:.2f}  "
          f"→ edge {a['edge_prob']:+.2f}  (entry {entry_c}¢, fee ~{fee_c:.1f}¢/ct)")
    print(f"  suggested quarter-Kelly size: {qty} contracts  (bankroll ${args.bankroll:.0f}, "
          f"frac {args.fraction}, cap ${args.per_trade:.0f})")
    if a["edge_prob"] <= 0:
        print("  ⚠ your prob does NOT beat the market for this side — no positive edge to size.")
    print("  ⚠ Kelly assumes your prob is calibrated (UNPROVEN). Treat size as an upper bound; go smaller.")
    if not args.dry_run:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"  logged → {JOURNAL.relative_to(ROOT)}  (place the order yourself; this does NOT trade)")
    return 0


def _load_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    return [json.loads(l) for l in open(JOURNAL) if l.strip()]


def cmd_list(args) -> int:
    recs = _load_journal()
    if not recs:
        print(f"[disc] no calls logged yet at {JOURNAL.relative_to(ROOT)}"); return 0
    print(f"[disc] {len(recs)} logged call(s):")
    for r in recs:
        st = fetch_market(r["ticker"]) if args.refresh else None
        stat = f"{st['status']}/{st['result']}" if st else "(no refresh)"
        print(f"  {r['ts_utc'][:10]}  {r['ticker']:30s} {r['side']:3s} P={r['your_prob']:.2f} "
              f"edge{r['edge_prob']:+.2f} qty~{r['suggested_qty']}  [{stat}]  {r['thesis'][:40]}")
    return 0


def cmd_score(args) -> int:
    recs = _load_journal()
    if not recs:
        print("[disc] no calls logged yet"); return 0
    scored = []
    for r in recs:
        m = fetch_market(r["ticker"])
        time.sleep(0.2)
        if not m or m["status"] not in ("settled", "finalized", "determined") or m["result"] not in ("yes", "no"):
            continue
        out = outcome_for_side(m["result"], r["side"])
        if out is None:
            continue
        scored.append({**r, "_outcome": out})
    if not scored:
        print(f"[disc] {len(recs)} calls logged, 0 settled yet — nothing to score. "
              f"(Discretionary bets settle slowly; check back after they resolve.)")
        return 0
    items = brier_skill_items(scored)
    your_b = sum((r["your_prob"] - r["_outcome"]) ** 2 for r in scored) / len(scored)
    mkt_b = sum((r["market_implied"] - r["_outcome"]) ** 2 for r in scored) / len(scored)
    hit = sum(1 for r in scored if (r["your_prob"] >= 0.5) == (r["_outcome"] == 1.0)) / len(scored)
    print(f"[disc] scored {len(scored)} settled call(s):")
    print(f"  your Brier={your_b:.4f}  market Brier={mkt_b:.4f}  (lower = better)  directional hit-rate={hit:.0%}")
    ci = cluster_bootstrap_ci(items) if items else (None, None, None)
    cis = "n/a" if not ci or ci[0] is None else f"{ci[0]:+.4f} [{ci[1]:+.4f}, {ci[2]:+.4f}]"
    print(f"  Brier skill vs market (market_b − your_b, >0 ⇒ you beat the price): {cis}")
    cal = calibration_table(scored)
    if cal:
        print("  calibration (when you say X, you win Y):")
        for b in cal:
            print(f"    {b['range']:>9}: n={b['n']:>3}  you_said={b['you_said']:.2f}  you_won={b['you_won']:.2f}")
    print(f"\n  VERDICT: {_verdict(items, len(scored))}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Discretionary trade journal + sizing + honest edge scorer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="record a decision (does NOT place an order)")
    lg.add_argument("--ticker", required=True)
    lg.add_argument("--side", required=True, choices=["yes", "no"])
    lg.add_argument("--prob", required=True, type=float, help="YOUR probability your side wins (0-1)")
    lg.add_argument("--thesis", default="", help="one-line rationale (logged at decision time)")
    lg.add_argument("--bankroll", type=float, default=500.0, help="bankroll in $ for sizing")
    lg.add_argument("--fraction", type=float, default=0.25, help="Kelly fraction (default quarter-Kelly)")
    lg.add_argument("--per-trade", type=float, default=100.0, help="per-trade $ cap")
    lg.add_argument("--max-pct", type=float, default=0.25, help="max fraction of bankroll per trade")
    lg.add_argument("--dry-run", action="store_true", help="analyze only, do not write the journal")
    lg.set_defaults(fn=cmd_log)
    ls = sub.add_parser("list", help="show logged calls")
    ls.add_argument("--refresh", action="store_true", help="fetch live settlement status per call")
    ls.set_defaults(fn=cmd_list)
    sc = sub.add_parser("score", help="grade settled calls: Brier skill vs market + calibration + verdict")
    sc.set_defaults(fn=cmd_score)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

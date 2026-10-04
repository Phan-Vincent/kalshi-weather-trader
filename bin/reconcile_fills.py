#!/usr/bin/env python3
"""bin/reconcile_fills.py — calibrate the paper fill model against REAL live fills.

The whole premium effort exists to close the paper→live fill gap (paper +26% vs
live −8%). A fill model can't be validated against itself, so this compares the
live-premium book (real Kalshi fills, synced by sync_live_positions.py) against the
paper fill-model A/B arms and recommends the `ADVERSE_MARGIN_CENTS` / `FILL_DEPTH_FRAC`
that reproduce reality.

Method (no Kalshi calls — reads the already-synced state):
  • LIVE fill rate  = filled positions ÷ orders we posted (posted_live lifecycle rows),
    plus mean entry-vs-quote slippage.
  • PAPER fill rate = filled ÷ (filled+expired) for each premium fill-model arm
    (premium, premium-fill-strict, premium-fill-loose), from maker-lifecycle.jsonl.
  • Recommend the arm whose paper fill rate is closest to live → its fill params are
    the calibrated values to set on the paper books.

Needs the fill-model arms enabled (premium-fill-strict/loose) so the calibration grid
exists, and ≥30 live fills before the recommendation is trustworthy. Read-only.

Usage:
  python3 bin/reconcile_fills.py
  python3 bin/reconcile_fills.py --live-dir state/live-premium
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARGIN, DEFAULT_DEPTH = 1, 0.5  # paper_book.py fill-realism defaults


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


def _load_book(state_dir: Path) -> dict:
    p = state_dir / "paper-book.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _fill_params(env: dict) -> tuple[int, float]:
    return (int(env.get("KALSHI_WEATHER_ADVERSE_MARGIN_CENTS", DEFAULT_MARGIN)),
            float(env.get("KALSHI_WEATHER_FILL_DEPTH_FRAC", DEFAULT_DEPTH)))


def paper_arms() -> list[dict]:
    """Premium fill-model arms from variants.json, with their fill params."""
    cfg = json.loads((ROOT / "variants.json").read_text())
    arms = []
    for v in cfg.get("variants", []):
        env = v.get("env") or {}
        if env.get("KALSHI_WEATHER_PREMIUM_MODE") == "1" and not v.get("live"):
            margin, depth = _fill_params(env)
            arms.append({"name": v["name"], "dir": v["dir"],
                         "margin": margin, "depth": depth})
    return arms


def paper_fill_rate(state_dir: Path) -> tuple[float | None, int, int]:
    lc = _read_jsonl(state_dir / "maker-lifecycle.jsonl")
    filled = sum(1 for r in lc if r.get("event") == "filled")
    expired = sum(1 for r in lc if r.get("event") == "expired")
    # 2026-06-29 audit: a refresh-cancelled quote is a NON-fill outcome but logs no 'expired'
    # event — omitting it inflated premium-refresh's fill rate (93.7% vs a true ~27%). Count it.
    refresh_cx = sum(1 for r in lc if r.get("event") == "refresh_cancelled")
    resolved = filled + expired + refresh_cx
    return ((filled / resolved) if resolved else None, filled, expired + refresh_cx)


def live_fill_stats(live_dir: Path) -> dict:
    lc = _read_jsonl(live_dir / "maker-lifecycle.jsonl")
    posted = [r for r in lc if r.get("event") == "posted_live"]
    book = _load_book(live_dir)
    # slippage = actual entry − the price we quoted (match on ticker+side; positive = paid up)
    quote = {}
    for r in posted:
        quote.setdefault((r.get("ticker"), r.get("side")), r.get("limit_price_cents"))
    # Attribute only fills we actually posted. sync_live_positions.py mirrors the WHOLE
    # Kalshi account, so legacy/other positions on the shared account would otherwise
    # inflate the live fill rate. Keep positions whose (ticker, side) we quoted.
    posted_keys = set(quote)
    fills = [f for f in (list(book.get("open", [])) + list(book.get("closed", [])))
             if (f.get("ticker"), f.get("side")) in posted_keys]
    slips = []
    for f in fills:
        q = quote.get((f.get("ticker"), f.get("side")))
        e = f.get("avg_entry_cents") or f.get("entry_cents")
        if isinstance(q, (int, float)) and isinstance(e, (int, float)):
            slips.append(e - q)
    # 2026-06-29 audit: dedup the denominator. posted_live is re-logged every cycle with a
    # FRESH order_id (no dedup, ~1.37x), while paper post_maker dedupes — so raw len(posted)
    # structurally understated the live fill rate (54% vs a comparable ~74-81%). Count DISTINCT
    # logical orders (ticker, side) on both sides, matching paper's deduped denominator and the
    # book's one-position-per-ticker fills.
    raw_posted = len(posted)
    n_posted = len(posted_keys)
    filled_keys = {(f.get("ticker"), f.get("side")) for f in fills}
    n_filled = len(filled_keys)
    return {
        "posted": n_posted,
        "raw_posted": raw_posted,
        "repost_factor": (raw_posted / n_posted) if n_posted else None,
        "filled": n_filled,
        "fill_rate": (n_filled / n_posted) if n_posted else None,
        "mean_slippage": (sum(slips) / len(slips)) if slips else None,
        "n_slip": len(slips),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Calibrate paper fill model vs live fills.")
    ap.add_argument("--live-dir", default="state/live-premium")
    args = ap.parse_args()
    live_dir = ROOT / args.live_dir if not Path(args.live_dir).is_absolute() else Path(args.live_dir)

    print(f"\nFill-model reconciliation — live: {live_dir}\n{'='*60}")
    live = live_fill_stats(live_dir)
    if not live["posted"]:
        print("No live premium orders yet. Enable LIVE=1 with the premium-live arm and let")
        print("it post/fill for a few days, then re-run. (Reads the synced live book only.)")
        return 0

    lr = live["fill_rate"]
    print(f"LIVE: posted={live['posted']}  filled={live['filled']}  "
          f"fill_rate={lr*100:.1f}%" if lr is not None else "LIVE: fill_rate=n/a")
    if live.get("repost_factor"):
        print(f"      ({live['raw_posted']} raw posts deduped to {live['posted']} distinct "
              f"(ticker,side) orders; repost ×{live['repost_factor']:.2f})")
    if live["mean_slippage"] is not None:
        print(f"      mean entry-vs-quote slippage = {live['mean_slippage']:+.2f}c "
              f"(n={live['n_slip']}; positive = paid up vs our quote)")
    if live["filled"] < 30:
        print(f"      ⚠️ only {live['filled']} live fills — treat the recommendation as directional (<30).")

    print(f"\nPAPER fill-model arms (fill rate from maker-lifecycle):")
    print(f"  {'arm':22s} {'margin':>6} {'depth':>6} {'paper fill%':>11} {'Δ vs live':>10}")
    best = None
    for a in paper_arms():
        pr, pf, pe = paper_fill_rate(ROOT / "state" / a["dir"])
        if pr is None:
            print(f"  {a['name']:22s} {a['margin']:>6} {a['depth']:>6.2f} {'—':>11} {'(no data)':>10}")
            continue
        delta = abs(pr - lr) if lr is not None else None
        dstr = f"{(pr-lr)*100:+.1f}pt" if lr is not None else "—"
        print(f"  {a['name']:22s} {a['margin']:>6} {a['depth']:>6.2f} {pr*100:>10.1f}% {dstr:>10}")
        if lr is not None and (best is None or delta < best[0]):
            best = (delta, a)

    if best:
        _, a = best
        print(f"\nRECOMMENDATION: closest paper arm = '{a['name']}' → set the paper books to")
        print(f"  KALSHI_WEATHER_ADVERSE_MARGIN_CENTS={a['margin']}  "
              f"KALSHI_WEATHER_FILL_DEPTH_FRAC={a['depth']}")
        print("  (then confirm paper fill rate converges toward live on the next cycles.)")
    else:
        print("\nNo paper fill-model arms have lifecycle data yet — enable premium-fill-strict")
        print("and premium-fill-loose in variants.json to build the calibration grid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rebuild state/paper/{settlement-log,brier-log}.jsonl from paper-book.json.

SAFETY (QA-05, 2026-07-01): this rewrites ledger files, so it DEFAULTS TO DRY-RUN.
Pass --apply to actually write; --apply first copies each target to
<name>.bak.<UTC-timestamp> before overwriting. The market outcome is read from each
closed trade's recorded `settlement_result` field, NOT inferred from the P&L sign
(the old heuristic `"yes" if pnl>0 else "no"` mislabels NO-side winners, which then
corrupts downstream Brier/win-rate via backfill_brier.py).
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "state" / "paper"


def market_result(c: dict) -> str:
    """True market outcome ('yes'/'no') for a closed trade. Prefer the recorded
    settlement_result; only when it is missing reconstruct it from side + P&L sign
    (a winning position settled on its own side, a loser on the other)."""
    r = c.get("settlement_result")
    if r in ("yes", "no"):
        return r
    side = c.get("side")
    won = c.get("pnl_cents", 0) > 0
    if side in ("yes", "no"):
        return side if won else ("no" if side == "yes" else "yes")
    return "yes" if won else "no"


def _backup(path: Path, stamp: str) -> None:
    if path.exists():
        bak = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, bak)
        print(f"  backed up {path.name} -> {bak.name}")


def build_settlements(book: dict) -> tuple[list[dict], int]:
    """Return (settlement rows, count the old pnl-sign heuristic would have mislabeled)."""
    rows = []
    mislabeled = 0
    for c in book.get("closed", []):
        if not isinstance(c, dict):
            continue
        result = market_result(c)
        if result != ("yes" if c.get("pnl_cents", 0) > 0 else "no"):
            mislabeled += 1
        rows.append({
            "ticker": c.get("ticker", "?"),
            "side": c.get("side", "?"),
            "qty": c.get("qty", 0),
            "pnl_cents": c.get("pnl_cents", 0),
            "entry_cents": c.get("entry_cents", 0),
            "exit_cents": c.get("exit_cents", 100),
            "settlement_result": result,
            "settled_at_utc": c.get("settled_at_utc", book.get("updated_utc", "")),
            "market_settlement_price": result,
            "market_close_time": c.get("closed_utc", ""),
            "fair_prob_at_open": c.get("fair_prob_at_open", c.get("fair_prob", 0)),
            "opened_utc": c.get("opened_utc", ""),
            "paper_order_id": c.get("paper_order_id", c.get("order_id", "")),
            "rationale": "",
        })
    return rows, mislabeled


def build_brier(book: dict) -> tuple[list[dict], int, int]:
    """Filter the existing brier-log to trades still present in the book, plus stub any
    book trade with no brier entry. Returns (rows, matched, total_paper_trades)."""
    old_brier = []
    with open(PAPER / "brier-log.jsonl") as f:
        for line in f:
            if line.strip():
                old_brier.append(json.loads(line))

    paper_trades = set()
    for group in ("closed", "open", "pending_makers"):
        for t in book.get(group, []):
            if isinstance(t, dict):
                paper_trades.add((t.get("ticker", ""), t.get("side", ""), t.get("qty", 0)))

    rows, matched = [], set()
    for entry in old_brier:
        key = (entry.get("ticker", ""), entry.get("side", ""), entry.get("qty", 0))
        if key in paper_trades:
            rows.append(entry)
            matched.add(key)

    for ticker, side, qty in paper_trades - matched:
        rows.append({
            "record_id": f"rebuild-{ticker}-{side}-{qty}",
            "status": "open",
            "ticker": ticker,
            "side": side,
            "our_prob": 0.5,
            "market_prob": 0.5,
            "qty": qty,
            "price_cents": 0,
            "mode": "maker",
            "timestamp_utc": book.get("updated_utc", ""),
            "source": "paper",
        })
        print(f"  + missing brier entry: {ticker} {side} qty={qty}")

    return rows, len(matched), len(paper_trades)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually overwrite the ledger files (default: dry-run, no writes; "
                         "each target is backed up to <name>.bak.<timestamp> first)")
    args = ap.parse_args()
    dry = not args.apply
    tag = "DRY-RUN" if dry else "APPLY"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with open(PAPER / "paper-book.json") as f:
        book = json.load(f)

    settlements, mislabeled = build_settlements(book)
    total_pnl = sum(s["pnl_cents"] for s in settlements) / 100
    print(f"[{tag}] settlement-log: {len(settlements)} entries, PnL=${total_pnl:+,.2f}")
    if mislabeled:
        print(f"  (using recorded settlement_result — the old pnl-sign heuristic would have "
              f"mislabeled {mislabeled} outcome(s), e.g. NO-side winners)")

    new_brier, matched, n_trades = build_brier(book)
    settled_count = sum(1 for e in new_brier if e.get("status") == "settled")
    open_count = sum(1 for e in new_brier if e.get("status") == "open")
    print(f"[{tag}] brier-log: {len(new_brier)} entries ({settled_count} settled, {open_count} open); "
          f"matched {matched}/{n_trades} paper-book trades")

    if dry:
        print("\nDRY-RUN — no files written. Re-run with --apply to overwrite "
              "(each target is backed up to <name>.bak.<timestamp> first).")
    else:
        _backup(PAPER / "settlement-log.jsonl", stamp)
        with open(PAPER / "settlement-log.jsonl", "w") as f:
            for s in settlements:
                f.write(json.dumps(s) + "\n")
        _backup(PAPER / "brier-log.jsonl", stamp)
        with open(PAPER / "brier-log.jsonl", "w") as f:
            for e in new_brier:
                f.write(json.dumps(e) + "\n")
        print(f"\nWROTE settlement-log ({len(settlements)}) and brier-log ({len(new_brier)}).")

    # ── Summary ──
    cash = book.get("cash_cents", 0) / 100
    start = book.get("starting_bank_cents", 0) / 100
    closed = [c for c in book.get("closed", []) if isinstance(c, dict)]
    closed_pnl = sum(c.get("pnl_cents", 0) for c in closed) / 100
    wins = sum(1 for c in closed if c.get("pnl_cents", 0) > 0)
    losses = sum(1 for c in closed if c.get("pnl_cents", 0) < 0)
    print()
    print("=== PAPER BOOK ===")
    print(f"  Cash: ${cash:,.2f} / Start: ${start:,.2f}")
    print(f"  Closed PnL: ${closed_pnl:+,.2f}")
    if closed:
        print(f"  Trades: {len(closed)} | W:{wins} L:{losses} WR:{wins/len(closed)*100:.1f}%")
    else:
        print("  No closed trades yet")
    print(f"  Open: {len(book.get('open', []))} positions | {len(book.get('pending_makers', []))} pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())

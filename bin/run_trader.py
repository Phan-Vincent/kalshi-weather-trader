#!/usr/bin/env python3
"""
bin/run_trader.py — CLI entry point for the Kalshi weather trader.

Dry-run (default):
    python3 bin/run_trader.py --fair-values fair-values.json --mode taker --dry-run

Live (requires --yes):
    python3 bin/run_trader.py --fair-values fair-values.json --mode taker --live --yes --max-orders 2

Market-making dry-run:
    python3 bin/run_trader.py --fair-values fair-values.json --mode mm --dry-run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure trader modules are importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.risk import RiskGate
from trader.scanner import scan, Signal
from trader.orders import place_limit_order, OrderResult


# ── Paths ───────────────────────────────────────────────────────────────
RUNTIME_START = datetime.now(timezone.utc)
BASE_DIR = Path.home() / ".openclaw/workspace/automations/kalshi-weather"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
CACHE_DIR = BASE_DIR / "cache"

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────

def _log(msg: str, file=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    if file:
        file.write(line + "\n")
        file.flush()


def _write_run_log(result: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"run-{ts}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(result) + "\n")


# ── Table formatting ────────────────────────────────────────────────────

def _fmt_signals(signals: list[Signal]) -> str:
    if not signals:
        return "No signals."
    header = f"{'#':>3} {'Ticker':<28} {'Side':>4} {'Price':>5} {'Qty':>4} {'Edge':>6} {'Profit':>7} {'Conf':>5} {'Mode':>6}"
    lines = [header, "-" * len(header)]
    for i, s in enumerate(signals, 1):
        lines.append(
            f"{i:>3} {s.ticker:<28} {s.side.upper():>4} {s.limit_price_cents:>5} {s.qty:>4} "
            f"{s.edge_cents_post_fee:>5.1f} {s.est_profit_dollars:>6.2f} {s.confidence:>5} {s.mode:>6}"
        )
    return "\n".join(lines)


# ── Prompt ────────────────────────────────────────────────────────────

def _confirm_live(signals: list[Signal]) -> bool:
    print("\n" + "=" * 70, file=sys.stderr)
    print("LIVE TRADING PREVIEW", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(_fmt_signals(signals), file=sys.stderr)
    print("\nType CONFIRM to place these orders with REAL MONEY:", file=sys.stderr, end=" ")
    try:
        response = input().strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return response == "CONFIRM"


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi weather market trader")
    parser.add_argument("--fair-values", required=True, help="Path to fair-values.json")
    parser.add_argument("--mode", choices=["taker", "mm"], default="taker", help="Trading mode")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate only (default)")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt (live only)")
    parser.add_argument("--max-orders", type=int, default=None, help="Override per-run max orders")
    parser.add_argument("--prod", action="store_true", help="Use production API (default: demo)")
    parser.add_argument("--min-edge", type=int, default=None, help="Override min edge in cents")
    parser.add_argument("--daily-loss", type=int, default=None, help="Override daily max loss $")
    parser.add_argument("--weekly-loss", type=int, default=None, help="Override weekly max loss $")
    args = parser.parse_args()

    # Resolve mode flags
    dry_run = not args.live
    if args.live and args.dry_run:
        _log("WARNING: --live and --dry-run both passed; --live wins.")
        dry_run = False

    prod = args.prod or False

    # Build risk gate with optional overrides
    risk_kwargs = {}
    if args.max_orders is not None:
        risk_kwargs["per_run_max_orders"] = args.max_orders
    if args.min_edge is not None:
        risk_kwargs["min_edge_cents_after_fees"] = args.min_edge
    if args.daily_loss is not None:
        risk_kwargs["daily_max_loss_dollars"] = args.daily_loss
    if args.weekly_loss is not None:
        risk_kwargs["weekly_max_loss_dollars"] = args.weekly_loss

    risk_gate = RiskGate(**risk_kwargs)

    # ── Circuit breaker guard ────────────────────────────────────────
    ok_cb, reason_cb = risk_gate.check_circuit_breaker()
    if not ok_cb:
        _log(f"RISK GATE BLOCKED: {reason_cb}")
        out = {"status": "blocked", "reason": reason_cb, "risk_summary": risk_gate.get_summary()}
        print(json.dumps(out))
        _write_run_log(out)
        return 1

    # ── Budget guard ─────────────────────────────────────────────────
    ok, reason = risk_gate.check_budget()
    if not ok:
        _log(f"RISK GATE BLOCKED: {reason}")
        out = {"status": "blocked", "reason": reason, "risk_summary": risk_gate.get_summary()}
        print(json.dumps(out))
        _write_run_log(out)
        return 1

    summary = risk_gate.get_summary()
    _log(f"Budget OK | today_pnl=${summary['today_pnl_dollars']:.2f} week_pnl=${summary['week_pnl_dollars']:.2f}")

    # ── Scan ─────────────────────────────────────────────────────────
    _log(f"Scanning {args.fair_values} | mode={args.mode}")
    # Bankroll for Kelly sizing: default to $100 paper bank unless overridden via env
    import os
    bankroll_cents = int(float(os.environ.get("KALSHI_BANKROLL_DOLLARS", "100")) * 100)
    signals = scan(args.fair_values, risk_gate, mode=args.mode, top_n=5, bankroll_cents=bankroll_cents)

    if not signals:
        _log("No signals pass risk gate.")
        out = {"status": "no_signals", "signals": [], "risk_summary": summary}
        print(json.dumps(out))
        _write_run_log(out)
        return 0

    _log(f"Found {len(signals)} signals:")
    print(_fmt_signals(signals), file=sys.stderr)

    # ── JSON output to stdout ────────────────────────────────────────
    signals_json = [s.to_dict() for s in signals]
    out = {
        "status": "scanned",
        "mode": args.mode,
        "dry_run": dry_run,
        "prod": prod,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "signals": signals_json,
        "risk_summary": summary,
    }
    # Pretty-print top-level to stdout for agent consumption
    print(json.dumps(out, indent=2))

    # ── Live order placement ───────────────────────────────────────
    if dry_run:
        _log("Dry run complete. No orders placed.")
        for sig in signals:
            place_limit_order(
                sig.ticker, sig.side, sig.limit_price_cents, sig.qty,
                prod=prod, dry_run=True,
            )
        out["status"] = "dry_run"
        _write_run_log(out)
        return 0

    # LIVE path — must have --yes or interactive CONFIRM
    if not args.yes:
        if not _confirm_live(signals):
            _log("Live placement ABORTED by user.")
            out["status"] = "aborted"
            _write_run_log(out)
            return 1

    _log(f"LIVE MODE — placing up to {risk_gate.per_run_max_orders} orders")
    placed: list[dict] = []
    orders_sent = 0

    for sig in signals:
        if orders_sent >= risk_gate.per_run_max_orders:
            _log(f"Max orders per run ({risk_gate.per_run_max_orders}) reached.")
            break

        if not risk_gate.check_event_limit(sig.ticker, sig.qty, sig.limit_price_cents):
            _log(f"SKIPPED {sig.ticker}: per-event limit")
            continue

        result = place_limit_order(
            sig.ticker, sig.side, sig.limit_price_cents, sig.qty,
            prod=prod, dry_run=False,
        )
        orders_sent += 1

        record = {
            "ticker": sig.ticker,
            "side": sig.side,
            "price_cents": sig.limit_price_cents,
            "qty": sig.qty,
            "success": result.success,
            "order_id": result.order_id,
            "error": result.error,
        }
        placed.append(record)

        if result.success and result.order_id:
            _log(f"PLACED {sig.ticker} {sig.side.upper()} @{sig.limit_price_cents} qty={sig.qty} id={result.order_id}")
            risk_gate.record_order(sig.ticker, sig.side, sig.qty, sig.limit_price_cents, result.order_id)
        else:
            _log(f"FAILED {sig.ticker}: {result.error}")

    out["placed"] = placed
    out["status"] = "live_complete"
    _write_run_log(out)
    _log(f"Run complete. Placed {sum(1 for p in placed if p['success'])} / {len(placed)} orders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

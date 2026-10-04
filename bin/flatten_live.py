#!/usr/bin/env python3
"""bin/flatten_live.py — EMERGENCY: cancel ALL resting live Kalshi orders (panic button).

Stops new exposure immediately by cancelling every resting (unfilled) order on the
live prod account. Open *positions* are left as-is (use --flatten-positions, phase-2,
to close them too — not yet implemented). Risk-REDUCING by design.

  python3 bin/flatten_live.py            # DRY-RUN: list what would be cancelled
  python3 bin/flatten_live.py --execute  # actually cancel all resting orders (REAL)

The dry-run is the default so this is safe to run any time to inspect resting orders.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.orders import list_resting_orders, cancel_order  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Cancel all resting live Kalshi orders")
    ap.add_argument("--execute", action="store_true", help="actually cancel (default: dry-run)")
    ap.add_argument("--demo", action="store_true", help="use demo API instead of prod")
    args = ap.parse_args()
    prod = not args.demo

    orders, list_err = list_resting_orders(prod=prod)
    if list_err:
        # The list call FAILED — we CANNOT assume "no orders". Alert loudly and exit
        # non-zero; a panic button that silently cancels nothing is dangerous.
        msg = f"flatten_live: could NOT list resting orders — {list_err}. Manual check required."
        print(f"[flatten] ✗ {msg}", file=sys.stderr)
        if prod:
            try:
                from trader.notify import alert
                alert("🧯⚠️ " + msg, key="flatten_list_failed")
            except Exception as e:
                print(f"[flatten] alert dispatch failed: {e}", file=sys.stderr)
        return 2
    if not orders:
        print("[flatten] no resting orders found (nothing to cancel).")
        return 0

    print(f"[flatten] {len(orders)} resting order(s):")
    for o in orders:
        oid = o.get("order_id") or o.get("id")
        print(f"  - {oid}  {o.get('ticker')}  {o.get('side')}  "
              f"{o.get('yes_price') or o.get('no_price') or o.get('price')}¢  x{o.get('remaining_count') or o.get('count')}")

    if not args.execute:
        print("[flatten] DRY-RUN — nothing cancelled. Re-run with --execute to cancel all of the above.")
        return 0

    cancelled, failed = 0, 0
    for o in orders:
        oid = o.get("order_id") or o.get("id")
        if not oid:
            failed += 1
            continue
        resp = cancel_order(str(oid), prod=prod)
        if isinstance(resp, dict) and resp.get("_error"):
            print(f"[flatten] ✗ cancel failed {oid}: HTTP {resp.get('status')} {str(resp.get('body'))[:120]}", file=sys.stderr)
            failed += 1
        else:
            print(f"[flatten] ✓ cancelled {oid}")
            cancelled += 1

    msg = f"flatten_live: cancelled {cancelled}/{len(orders)} resting orders ({failed} failed)"
    print(f"[flatten] {msg}")
    try:
        from trader.notify import alert
        alert("🧯 " + msg, key="flatten_live")
    except Exception:
        pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

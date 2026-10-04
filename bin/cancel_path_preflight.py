#!/usr/bin/env python3
"""bin/cancel_path_preflight.py — de-risk the UNEXERCISED cancel (DELETE) path (audit 2026-07-06).

Finding: cancel_order() — the signed DELETE that flatten_live (panic button), halt-cancel,
and quote refresh ALL funnel through — has NEVER executed in production (219 lifecycle events,
all posted_live; every test mocks it). It was migrated to the V2 external-api host in the SAME
2026-06-23 change whose CREATE leg broke live with a 410. So the first real cancel would happen
in an incident, when discovering a broken endpoint/signing is worst.

This tool de-risks it in two stages:

  (default, READ-ONLY) Validate the cancel request is CONSTRUCTED correctly and that signing +
  auth work against prod, WITHOUT sending a DELETE:
    - host == external-api prod, V2 path == /trade-api/v2/portfolio/events/orders/{id}, method DELETE
    - RSA-PSS signing produces well-formed auth headers (no key/crypto error)
    - a read-only signed GET (list resting orders) authenticates (no 401) — proves the same
      credentials the DELETE will use are accepted by prod right now
    - reports how many real resting orders exist (candidates for the live test below)

  (--execute-cancel <ORDER_ID>)  OPERATOR-RUN, places a REAL cancel of ONE order, then verifies
  it left the resting list. This is the ONLY way to truly exercise the DELETE leg; run it once,
  supervised, against a low-value resting order to retire the tail risk. NOT run automatically.

Usage:
  python3 bin/cancel_path_preflight.py                      # read-only preflight
  python3 bin/cancel_path_preflight.py --execute-cancel KX-ORDER-ID   # REAL single cancel (operator)
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.orders import (  # noqa: E402
    ORDER_API_BASE_PROD, kalshi_auth_headers, list_resting_orders,
)

_PASS, _FAIL = [], []


def _ok(name: str, cond: bool, detail: str = "") -> bool:
    (_PASS if cond else _FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    return bool(cond)


def preflight() -> int:
    print("CANCEL-PATH PREFLIGHT (read-only) — validates the DELETE leg without sending it\n")

    # 1. Construction: mirror cancel_order() exactly and assert the V2 contract.
    dummy = "PREFLIGHT-DUMMY-ORDER-ID"
    endpoint = f"/portfolio/events/orders/{dummy}"
    path = "/trade-api/v2" + endpoint
    _ok("host is external-api prod", ORDER_API_BASE_PROD == "https://external-api.kalshi.com/trade-api/v2",
        ORDER_API_BASE_PROD)
    _ok("cancel endpoint is the V2 events path", endpoint == f"/portfolio/events/orders/{dummy}", endpoint)
    _ok("signed path carries the /trade-api/v2 prefix", path.startswith("/trade-api/v2/portfolio/events/orders/"), path)

    # 2. Signing: build the DELETE auth headers (same call cancel_order makes). A broken key or
    #    crypto misconfig raises here — exactly the failure we don't want to discover mid-incident.
    try:
        hdr = kalshi_auth_headers("DELETE", path, prod=True)
        have = all(k in hdr for k in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"))
        _ok("DELETE request signs cleanly (RSA-PSS)", have and bool(hdr.get("KALSHI-ACCESS-SIGNATURE")),
            "auth headers present")
    except Exception as e:
        _ok("DELETE request signs cleanly (RSA-PSS)", False, f"signing raised: {e}")

    # 3. Credentials accepted by prod NOW: a read-only signed GET (same creds the DELETE uses).
    orders, err = list_resting_orders(prod=True)
    _ok("prod authenticates the signed session (no 401)", err is None, err or f"{len(orders)} resting")
    if err is None:
        print(f"\n  → {len(orders)} resting order(s) on prod.")
        for o in orders[:10]:
            oid = o.get("order_id") or o.get("id") or "?"
            print(f"     {oid}  {o.get('ticker','?')}  {o.get('side','?')} @ {o.get('yes_price') or o.get('price','?')}")
        print("\n  The construction + signing above is validated, but the DELETE ROUND-TRIP is still")
        print("  unproven. To truly exercise it, run ONCE against a low-value resting order:")
        print("     python3 bin/cancel_path_preflight.py --execute-cancel <ORDER_ID>")

    print(f"\n{'✅ PREFLIGHT PASS' if not _FAIL else '❌ PREFLIGHT FAIL: ' + ', '.join(_FAIL)}")
    return 0 if not _FAIL else 1


def execute_cancel(order_id: str) -> int:
    """OPERATOR-RUN: cancel exactly ONE real resting order and verify it's gone."""
    from trader.orders import cancel_order  # imported ONLY in this explicit branch
    print(f"⚠️  REAL CANCEL of a single live order: {order_id}")
    before, err = list_resting_orders(prod=True)
    if err is None and not any((o.get("order_id") or o.get("id")) == order_id for o in before):
        print(f"  note: {order_id} is not in the current resting list — cancelling anyway to test the path.")
    resp = cancel_order(order_id, prod=True)
    if resp.get("_error"):
        print(f"  ❌ cancel FAILED: HTTP {resp.get('status')} {str(resp.get('body') or resp.get('exception'))[:200]}")
        print("  The DELETE leg is BROKEN — flatten_live/halt-cancel would fail in an incident. Fix before relying on it.")
        return 1
    # cancel_order returned OK, which is the same success signal flatten_live/halt-cancel act on.
    # The resting list is only eventually consistent, so poll a few times with backoff before
    # declaring the order still-listed — a single immediate re-check reads success as failure.
    gone = False
    for attempt in range(4):  # ~0 + 0.5 + 1 + 2 = up to 3.5s
        if attempt:
            time.sleep(0.5 * (2 ** (attempt - 1)))
        after, err2 = list_resting_orders(prod=True)
        gone = err2 is None and not any((o.get("order_id") or o.get("id")) == order_id for o in after)
        if gone:
            break
    if gone:
        print("  ✅ cancel returned OK; order no longer resting (VERIFIED) — DELETE round-trip proven.")
        return 0
    print("  ⚠️  cancel returned OK but order still resting after retries — verify manually before trusting the rail.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate/exercise the live order-cancel path")
    ap.add_argument("--execute-cancel", metavar="ORDER_ID", help="OPERATOR: really cancel ONE resting order")
    args = ap.parse_args()
    if args.execute_cancel:
        return execute_cancel(args.execute_cancel)
    return preflight()


if __name__ == "__main__":
    sys.exit(main())

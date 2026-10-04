#!/usr/bin/env python3
"""READ-ONLY prod order-path preflight (2026-07-01 specialized QA, Pass C — repurposed).

Originally a demo order-path smoketest, but Kalshi's demo/sandbox has ~0 liquidity, so this
deployment deliberately points the "demo" config slot at prod creds and trades paper against
REAL prod order books — the demo env can't simulate anything. So a demo place/cancel harness is
useless here. This instead verifies the PROD order-path plumbing WITHOUT placing anything:

  - RSA-PSS request signing + transport authenticate against the prod host (no 401),
  - a currently-resting real order (if any) round-trips with a coherent side/price mapping.

It NEVER places, cancels, or modifies an order — it imports no order-mutating function. The live
create-order BODY mapping (bid/ask + 100-Pc) is already validated in production by the live arm's
fills reconciling to the ledger; the only thing a sandbox would add — server-side idempotency on a
timed-out-then-retried POST — cannot be tested without placing a real order, which this won't do.

Usage:  python3 bin/prod_orderpath_preflight.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

# SAFETY: import ONLY read-only endpoints. place_limit_order / cancel_order are intentionally
# NOT imported here — this module must have no way to mutate an order.
from trader.orders import (  # noqa: E402
    ORDER_API_BASE_PROD, _http_request, kalshi_auth_headers, list_resting_orders,
)

_PASS, _FAIL = [], []


def _ok(name: str, cond: bool, detail: str = "") -> bool:
    (_PASS if cond else _FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return cond


def _balance() -> dict:
    """Signed read-only GET /portfolio/balance on prod."""
    path = "/trade-api/v2/portfolio/balance"
    hdr = kalshi_auth_headers("GET", path, prod=True)
    return _http_request("GET", ORDER_API_BASE_PROD + "/portfolio/balance", headers=hdr)


def main() -> int:
    _PASS.clear()
    _FAIL.clear()
    print(f"PROD order-path preflight (READ-ONLY, no orders placed). base: {ORDER_API_BASE_PROD}")

    # 1. Signed read #1 - resting orders. Proves RSA-PSS signing + transport + auth on prod.
    orders, err = list_resting_orders(prod=True)
    if not _ok("signed_read_resting_orders", err is None, err or f"{len(orders)} resting"):
        print("\nPREFLIGHT FAILED - prod signed read did not authenticate. Check "
              "~/.kalshi/config-prod.yaml (api_key_id) + prod-key-final.pem. No orders touched.")
        return 3

    # 2. Signed read #2 - balance (independent endpoint; confirms auth generalizes).
    bal = _balance()
    bal_ok = isinstance(bal, dict) and not bal.get("_error") and bal.get("balance") is not None
    _ok("signed_read_balance", bal_ok,
        f"balance={bal.get('balance')}" if bal_ok else f"{str(bal)[:120]}")

    # 3. Mapping sanity on any REAL resting order (read-back only): a coherent order has a side
    #    and a price in (0,100). This checks the live side/price representation on real data.
    if orders:
        coherent = 0
        for o in orders:
            side = str(o.get("side", "")).lower()
            px = o.get("yes_price") or o.get("no_price") or o.get("price")
            try:
                px = int(px)
            except (TypeError, ValueError):
                px = None
            if side in ("yes", "no", "bid", "ask") and px is not None and 0 < px < 100:
                coherent += 1
        _ok("resting_orders_mapping_coherent", coherent == len(orders),
            f"{coherent}/{len(orders)} coherent")
    else:
        print("  [SKIP] no resting orders to inspect (mapping is exercised daily by the live arm).")

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed: {_FAIL or 'none'}")
    print("Note: this preflight places NO orders. The live create-order body mapping is validated in "
          "prod by live fills reconciling to the ledger.")
    return 0 if not _FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

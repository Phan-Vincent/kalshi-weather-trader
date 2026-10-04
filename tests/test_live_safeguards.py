#!/usr/bin/env python3
"""Live-path safety rails added after the 2026-06-23 pre-flight audit:
  (a) the halt check fails SAFE — any error → halted (never fail-open into live trading)
  (b) the per-market $ cap is enforced at the actual placement price (check_event_limit)
  (c) list_resting_orders SURFACES API errors so the flatten panic button can't silently no-op
Runs under plain python3.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import trader.halt as halt  # noqa: E402
from trader.risk import RiskGate  # noqa: E402
import trader.orders as orders  # noqa: E402


# ── (a) halt fail-safe ────────────────────────────────────────────────────
def test_is_halted_safe_fails_safe_on_error():
    orig = halt.is_halted
    def boom():
        raise RuntimeError("sentinel read blew up")
    halt.is_halted = boom
    try:
        halted, reason = halt.is_halted_safe()
    finally:
        halt.is_halted = orig
    assert halted is True, "FAIL-OPEN: a broken halt check left live trading enabled"
    assert reason and "error" in reason.lower()


def test_is_halted_safe_passthrough_when_ok():
    orig = halt.is_halted
    halt.is_halted = lambda: (False, None)
    try:
        assert halt.is_halted_safe() == (False, None)
    finally:
        halt.is_halted = orig


# ── (b) per-market $ cap at placement price ───────────────────────────────
def test_per_market_cap_rejects_overcap_at_placement_price():
    with tempfile.TemporaryDirectory() as d:
        r = RiskGate(_state_dir=Path(d))
        r.per_event_max_position_dollars = 5      # $5/market cap (premium-live)
        r._open_positions = []
        # Scanner sized 12 contracts at 40¢ ($4.80); live drifts to 45¢ → 12*45 = $5.40 > $5.
        assert r.check_event_limit("KXT", 12, 45) is False, "over-cap order must be rejected"
        # The clamp the live path applies: max_qty = 500 // 45 = 11 → $4.95 ≤ $5.
        max_qty = (5 * 100) // 45
        assert max_qty == 11
        assert r.check_event_limit("KXT", max_qty, 45) is True, "clamped order must pass"


# ── (c) flatten can't silently no-op on a list failure ────────────────────
def _patch_orders(http_return):
    orig_h, orig_r = orders.kalshi_auth_headers, orders._http_request
    orders.kalshi_auth_headers = lambda *a, **k: {}
    orders._http_request = lambda *a, **k: http_return
    return (orig_h, orig_r)


def _unpatch_orders(saved):
    orders.kalshi_auth_headers, orders._http_request = saved


def test_list_resting_orders_surfaces_api_error():
    saved = _patch_orders({"_error": True, "status": 401, "body": "bad signature"})
    try:
        result, err = orders.list_resting_orders(prod=True)
    finally:
        _unpatch_orders(saved)
    assert result == []
    assert err and "401" in err, "API error must be surfaced, not swallowed as empty list"


def test_list_resting_orders_ok_returns_no_error():
    saved = _patch_orders({"orders": [{"order_id": "abc"}]})
    try:
        result, err = orders.list_resting_orders(prod=True)
    finally:
        _unpatch_orders(saved)
    assert err is None
    assert result and result[0]["order_id"] == "abc"


def test_place_limit_order_no_id_is_failure():
    # A 200 response with no order_id must NOT be a success — phantom-order guard (#2).
    saved = _patch_orders({"status": 200})  # 200, no _error, no order_id
    try:
        res = orders.place_limit_order("KXT", "yes", 50, 1, prod=True, dry_run=False)
    finally:
        _unpatch_orders(saved)
    assert res.success is False, "ID-less 200 wrongly treated as success"
    assert res.order_id is None


if __name__ == "__main__":
    test_is_halted_safe_fails_safe_on_error();                 print("[PASS] halt check fails SAFE on error")
    test_is_halted_safe_passthrough_when_ok();                 print("[PASS] halt check passthrough when OK")
    test_per_market_cap_rejects_overcap_at_placement_price();  print("[PASS] per-market $ cap rejects over-cap at placement price")
    test_list_resting_orders_surfaces_api_error();             print("[PASS] list_resting_orders surfaces API error")
    test_list_resting_orders_ok_returns_no_error();            print("[PASS] list_resting_orders ok → (orders, None)")
    test_place_limit_order_no_id_is_failure();                 print("[PASS] ID-less 200 → order failure (no phantom)")
    print("\nAll live-safeguard tests pass.")

#!/usr/bin/env python3
"""Pin the Kalshi V2 create-order body mapping (orders._v2_order_body).

This is money-critical: a wrong side or price buys the wrong contract. Kalshi V2 is
single-book / YES-leg:  bid=buy YES, ask=sell YES (==buy NO at 1-price), price always
the YES-side value in fixed-point dollars. Verified against the create-order-v2 docs
2026-06-23. Runs under plain `python3`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.orders import _v2_order_body  # noqa: E402


def test_buy_yes_is_bid_at_yes_price():
    b = _v2_order_body("KXHIGHTNY-26JUN23-T60", "yes", 56, 10)
    assert b["side"] == "bid", b
    assert b["price"] == "0.5600", b
    assert b["count"] == "10.00", b
    assert b["ticker"] == "KXHIGHTNY-26JUN23-T60"


def test_buy_no_is_ask_at_one_minus_no_price():
    # Buy NO at 44c == sell YES at 56c → ask, price = YES-side value 0.5600.
    b = _v2_order_body("KXHIGHTNY-26JUN23-T60", "no", 44, 5)
    assert b["side"] == "ask", b
    assert b["price"] == "0.5600", b  # NOT 0.4400
    assert b["count"] == "5.00", b


def test_no_side_mapping_general():
    assert _v2_order_body("KXT", "no", 30, 1)["price"] == "0.7000"   # NO@30 → YES 0.70
    assert _v2_order_body("KXT", "no", 99, 1)["price"] == "0.0100"   # NO@99 → YES 0.01
    assert _v2_order_body("KXT", "yes", 1, 1)["price"] == "0.0100"   # YES@1


def test_required_v2_fields_present_and_typed():
    b = _v2_order_body("KXT", "yes", 50, 3)
    for k in ("ticker", "client_order_id", "side", "count", "price",
              "time_in_force", "self_trade_prevention_type"):
        assert k in b, f"missing {k}"
    assert isinstance(b["count"], str) and isinstance(b["price"], str)  # fixed-point strings
    assert b["time_in_force"] == "good_till_canceled"
    assert b["self_trade_prevention_type"] == "taker_at_cross"
    # legacy fields must be gone (they trigger the 410)
    assert "action" not in b and "type" not in b and "yes_price" not in b and "no_price" not in b


if __name__ == "__main__":
    test_buy_yes_is_bid_at_yes_price();          print("[PASS] buy YES → bid @ YES price")
    test_buy_no_is_ask_at_one_minus_no_price();  print("[PASS] buy NO → ask @ (1-NO) YES price")
    test_no_side_mapping_general();              print("[PASS] NO/YES price mapping general")
    test_required_v2_fields_present_and_typed(); print("[PASS] V2 fields present; legacy fields gone")
    print("\nAll V2 order-body tests pass.")

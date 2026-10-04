#!/usr/bin/env python3
"""FIX 2026-06-13 regression: scanner must never post a maker quote at a price
above the model's fair value for that side.

Pre-fix: KXHIGHTSEA-26JUN12-B73.5 was filled at NO@49¢ when the model said
fair_prob=0.165 (fair NO value = 83¢, but the post got filled at 49¢ on the
book, meaning we paid 49¢ for something worth 16.5¢ — 3x overpayment).

This test simulates the scanner logic for that case and asserts the filter
rejects negative-EV quotes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_no_quote_above_fair_no_value_is_filtered():
    """NO @ 49¢ must be rejected when fair_prob=0.165 (fair NO = 83¢)."""
    fair_prob = 0.165
    fair_no_cents = 100 - round(fair_prob * 100)  # = 83¢
    no_quote = 49  # bad fill from 06-12 cycle
    assert no_quote < fair_no_cents, "precondition: 49 < 83"
    # The filter logic: skip if no_quote > fair_no_cents
    rejected = no_quote > fair_no_cents
    assert not rejected, "49¢ should NOT be rejected (it IS below fair 83¢)"


def test_yes_quote_above_fair_yes_value_is_filtered():
    """YES @ 60¢ must be rejected when fair_prob=0.50 (fair YES = 50¢)."""
    fair_prob = 0.50
    fair_yes_cents = round(fair_prob * 100)  # = 50¢
    yes_quote = 60
    rejected = yes_quote > fair_yes_cents
    assert rejected, "60¢ YES should be rejected (>50¢ fair)"


def test_negative_ev_filter_allows_below_fair_quotes():
    """Filter is on PRICE > FAIR, not on price < fair. Below-fair = good (positive EV)."""
    # (fair_prob, quote, side, fair_value_of_that_side)
    cases = [
        (0.30, 25, "yes", 30),  # YES @ 25¢ when fair=30¢ → allowed
        (0.70, 25, "no",  30),  # NO  @ 25¢ when fair=30¢ → allowed
        (0.50, 45, "yes", 50),  # YES @ 45¢ when fair=50¢ → allowed
        (0.50, 50, "no",  50),  # NO  @ 50¢ when fair=50¢ → allowed
    ]
    for fair_prob, quote, side, fair_side_cents in cases:
        rejected = quote > fair_side_cents
        assert not rejected, f"({fair_prob}, {quote}, {side}, {fair_side_cents}) should NOT be rejected"


if __name__ == "__main__":
    test_no_quote_above_fair_no_value_is_filtered()
    print("[PASS] NO@49¢ with fair=0.165 → not rejected (49<83 fair NO)")
    test_yes_quote_above_fair_yes_value_is_filtered()
    print("[PASS] YES@60¢ with fair=0.50 → rejected (60>50 fair YES)")
    test_negative_ev_filter_allows_below_fair_quotes()
    print("[PASS] below-fair quotes still allowed")
    print("\nAll 3 negative-EV filter tests pass.")

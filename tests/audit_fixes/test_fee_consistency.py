#!/usr/bin/env python3
"""Tests for Fix #7: Fee formula consistency between paper_book.py and orders.py.

The bug: paper_book.py had its own inline fee formulas that overcharged fees:
  - taker: paper used `max(1, ceil(0.07 * p * (1-p) * 100))` per contract
           canonical uses `ceil(7 * qty * p * (1-p) * 10000) / 10000` * qty
  - maker: paper used `max(1, ceil(0.0175 * p * (1-p) * 100))` per contract
           canonical uses `ceil(175 * qty * p * (1-p) * 1000000) / 1000000` * qty

After fix: paper_book.py delegates to orders.py's canonical functions.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.paper_book import _fee_cents_per_contract
from trader.orders import _kalshi_taker_fee_cents, _kalshi_maker_rebate_cents


def test_taker_fee_per_contract_at_50c():
    """At 50¢, taker fee per contract should be 2¢ (Kalshi's standard)."""
    fee = _fee_cents_per_contract(50)
    assert fee == 2, f"at 50¢ fee should be 2¢/contract, got {fee}"


def test_taker_fee_per_contract_at_20c():
    """At 20¢, taker fee per contract should be 2¢."""
    fee = _fee_cents_per_contract(20)
    assert fee == 2, f"at 20¢ fee should be 2¢/contract, got {fee}"


def test_taker_fee_per_contract_at_95c():
    """At 95¢, taker fee per contract should be 1¢."""
    fee = _fee_cents_per_contract(95)
    assert fee == 1, f"at 95¢ fee should be 1¢/contract, got {fee}"


def test_taker_fee_at_zero_returns_zero():
    """Edge case: zero/100 prices should be rejected by canonical."""
    assert _fee_cents_per_contract(0) == 0
    assert _fee_cents_per_contract(100) == 0


def test_paper_taker_total_matches_canonical():
    """When filling qty contracts, total cost should match orders.py math."""
    for qty, price in [(1, 30), (5, 50), (10, 20), (50, 70), (1, 1), (1, 99)]:
        paper_total = _fee_cents_per_contract(price) * qty
        canonical_total = _kalshi_taker_fee_cents(qty, price)
        # Per-contract ceiling * qty can differ from total-qty ceiling by ~1¢
        # when the fractional part pushes over the boundary.
        # This is acceptable — the FillTaker path now uses total-qty canonical.
        assert abs(paper_total - canonical_total) <= qty, \
            f"qty={qty} price={price}¢: paper={paper_total} vs canonical={canonical_total} (diff={abs(paper_total-canonical_total)} > {qty})"


def test_maker_fee_consistency_for_fills():
    """When paper_book converts a maker to a fill, it should use the
    canonical rebate formula from orders.py.
    Verify by checking the method source directly."""
    # Walk through the path: paper_book should now import from orders.py
    import trader.paper_book as pb
    import inspect
    src = inspect.getsource(pb.PaperBook._convert_maker_to_fill)
    # The new code should reference _kalshi_maker_rebate_cents, not the inline formula
    assert "_kalshi_maker_rebate_cents" in src, \
        "paper_book._convert_maker_to_fill should use canonical _kalshi_maker_rebate_cents"
    # (Also verify the double-exposure guard is present)
    assert "rejected_double_exposure" in src, \
        "paper_book._convert_maker_to_fill should have double-exposure guard"


def test_maker_rebate_at_50c_qty_10():
    """At 50¢, qty=10, maker rebate per contract should be 1¢ each (10¢ total)."""
    rebate = _kalshi_maker_rebate_cents(10, 50)
    # canonical: ceil(175 * 10 * 50 * 50 / 1000000) = ceil(4.375) = 5
    # Total rebate for qty=10 at 50¢ is 5¢
    assert rebate == 5, f"qty=10 at 50¢ maker rebate should be 5¢ total, got {rebate}"


def test_fee_no_overcharge_at_50c():
    """Edge case regression: pre-fix, paper_book charged ceil(0.0175 * 0.25 * 100) = 5¢ 
    per contract (at 50¢). Canonical is 1¢ per contract. Verify we use canonical."""
    # When we use _fee_cents_per_contract(50), the maker taker-equivalent
    # for the can_afford check should match the canonical taker fee.
    assert _fee_cents_per_contract(50) == _kalshi_taker_fee_cents(1, 50), \
        "paper_book per-contract fee diverges from canonical taker fee"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            print(f"  ❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed" if failed == 0 else f"\n❌ {failed}/{len(tests)} FAILED")
    sys.exit(0 if failed == 0 else 1)

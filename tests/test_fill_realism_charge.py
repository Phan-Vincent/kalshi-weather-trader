#!/usr/bin/env python3
"""Paper maker fills must carry the realism drag (2026-06-29 audit).

The sim used to fill the whole lot at the EXACT limit with only the maker rebate —
no slippage, no adverse selection — so paper overstated realized edge ~5x vs live
(paper-premium +16.3¢/ct vs live-premium +3.0¢/ct; matched paired gap +6.85¢/ct).
_convert_maker_to_fill now charges slippage (≈+0.84¢) + adverse-selection markout
(≈+5.6¢) per contract as an explicit per-fill debit (folded into fee_total so the
cost = qty*price + fee invariant holds; recorded entry price stays the true limit).
Tunable via KALSHI_WEATHER_FILL_SLIPPAGE_CENTS / _ADVERSE_MARKOUT_CENTS; disabled by
FILL_REALISM=0. Runs under plain python3."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.paper_book import PaperBook  # noqa: E402

FUTURE = "2099-01-01T00:00:00+00:00"


def _fill_one(qty, price, env):
    """Post a YES maker, fill it via a crossing snapshot, return (book, fill_result)."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        d = tempfile.mkdtemp()  # persistent so settle_position()'s _save() can write
        bk = PaperBook(Path(d) / "book.json")
        bk.post_maker("KXTEST-26JUN29-B80", "yes", price, qty,
                      fair_prob=0.5, edge_cents_post_fee=0.0,
                      confidence="high", market_close_time=FUTURE)
        snap = {"KXTEST-26JUN29-B80": {"yes_bid": price + 5, "yes_bid_qty": qty * 4,
                                       "close_time": FUTURE}}
        fills = bk.sweep_pending_makers(snap)
        res = next(f for f in fills if f.get("status") == "filled_maker")
        return bk, res
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_charge_applied_by_default():
    qty, price = 10, 30
    expected = round((0.84 + 5.6) * qty)  # 64
    bk, res = _fill_one(qty, price, {})  # FILL_REALISM defaults on
    assert res["realism_charge_cents"] == expected, res
    pos = bk.open[0]
    # cost = qty*price + (-rebate) + charge  ->  cost - (qty*price - rebate) == charge
    from trader.orders import _kalshi_maker_rebate_cents
    rebate = _kalshi_maker_rebate_cents(qty, price)
    assert pos["cost_cents_total"] == qty * price - rebate + expected, pos
    # entry PRICE stays the true limit (charge lives in fee, not the price)
    assert pos["avg_entry_cents"] == price, pos


def test_charge_reduces_realized_pnl_vs_disabled():
    qty, price = 10, 30
    bk_on, _ = _fill_one(qty, price, {})
    bk_off, res_off = _fill_one(qty, price, {"KALSHI_WEATHER_FILL_REALISM": "0"})
    assert res_off["realism_charge_cents"] == 0, res_off
    # settle a WIN on both, compare pnl
    on = bk_on.settle_position("KXTEST-26JUN29-B80", "yes")[0]["pnl_cents"]
    off = bk_off.settle_position("KXTEST-26JUN29-B80", "yes")[0]["pnl_cents"]
    assert off - on == round((0.84 + 5.6) * qty), (off, on)


def test_per_contract_drag_is_calibrated():
    # combined drag ≈ 6.44¢/ct closes the matched +6.85¢/ct paper-vs-live gap
    for qty in (1, 4, 10, 30):
        _, res = _fill_one(qty, 40, {})
        assert res["realism_charge_cents"] == round(6.44 * qty), (qty, res)


def test_zeroed_knobs_disable_charge():
    _, res = _fill_one(10, 30, {"KALSHI_WEATHER_FILL_SLIPPAGE_CENTS": "0",
                                "KALSHI_WEATHER_FILL_ADVERSE_MARKOUT_CENTS": "0"})
    assert res["realism_charge_cents"] == 0, res


if __name__ == "__main__":
    test_charge_applied_by_default()
    test_charge_reduces_realized_pnl_vs_disabled()
    test_per_contract_drag_is_calibrated()
    test_zeroed_knobs_disable_charge()
    print("ok")

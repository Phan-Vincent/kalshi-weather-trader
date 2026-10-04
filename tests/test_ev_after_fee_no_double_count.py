#!/usr/bin/env python3
"""
tests/test_ev_after_fee_no_double_count.py — Regression for the 2026-07-06 audit
finding that the EV-after-fee tooling double-counted the maker rebate.

settlement-log rows carry pnl_cents ALREADY NET of fees/rebate (live rows are
Kalshi's net realized P&L; paper rows credit the rebate into cost). The three
decision tools subtracted fee_per_contract_cents(maker=True) — a NEGATIVE number —
which re-added the rebate, inflating EV. On the live-premium ledger this flipped
the headline from the true +1.3c/ct toward +1.7c/ct, sitting directly inside the
ADVERSE (ev_after_fee_c < 0) and KILL gates.

These tests pin ev_after_fee == pnl/qty exactly (no fee adjustment).

Run: python3 -m pytest tests/test_ev_after_fee_no_double_count.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import live_fill_quality as lfq
import fill_edge_breakdown as feb
import markout_kill_test as mkt


# A net-negative book: total pnl -100c over 100 contracts = -1.0c/ct. A maker rebate
# double-count would nudge this toward 0 or positive and could mask the ADVERSE signal.
ROWS = [
    {"ticker": "KXHIGHTHOU-26JUL06-B95", "side": "no", "qty": 50, "entry_cents": 30, "pnl_cents": -40},
    {"ticker": "KXHIGHTDAL-26JUL06-B89", "side": "no", "qty": 50, "entry_cents": 20, "pnl_cents": -60},
]
NET_EV_CT = sum(r["pnl_cents"] for r in ROWS) / sum(r["qty"] for r in ROWS)  # -1.0


def test_live_fill_quality_no_rebate_addback():
    r = lfq._realized_edge(ROWS)
    assert abs(r["ev_after_fee_c"] - NET_EV_CT) < 1e-9, (
        f"ev_after_fee_c={r['ev_after_fee_c']} != net {NET_EV_CT} — rebate double-counted"
    )
    assert r["ev_after_fee_c"] < 0, "a net-negative book must still read negative in the ADVERSE gate"


def test_fill_edge_breakdown_ev_equals_after_fee():
    s = feb._slice(ROWS)
    assert abs(s["ev_ct"] - s["ev_after_fee_ct"]) < 1e-9, "ev_ct and ev_after_fee must match (pnl already net)"
    assert abs(s["ev_after_fee_ct"] - NET_EV_CT) < 1e-9


def test_markout_net_edge_no_rebate_addback():
    items = mkt.net_edge_items_by_event(ROWS)
    # weighted mean over the returned per-position net values
    assert items, "expected weather rows to pass the filter"
    got = sum(v for _, v in items) / len(items)
    expect = sum(r["pnl_cents"] / r["qty"] for r in ROWS) / len(ROWS)
    assert abs(got - expect) < 1e-9, f"net edge {got} carries a rebate add-back (expected {expect})"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

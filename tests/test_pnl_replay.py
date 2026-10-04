#!/usr/bin/env python3
"""Price-aware P&L replay tests (quant review 2026-07-01, experiment #9).

Pins the fill-cost invariants (taker pays ask, maker rests at mid with a rebate, maker+markout adds
slippage+adverse), the NO-entry-price convention (a NO stores the NO price, not 100-x), the
lead-time edge gate, the clean settlement win math, the leak filters (same-day + leak1-cutoff), and
the leak-free-vs-contaminated verdict logic (never rules PAYS on contaminated data). Hermetic —
synthetic rows, stubbed actuals, no network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from datetime import datetime  # noqa: E402
import pnl_replay as p  # noqa: E402
from trader.orders import fee_per_contract_cents  # noqa: E402

TODAY = "2026-07-10"


def _row(city, date, asof, fp, mid, thr=80.0, series="KXHIGH"):
    return {"asof_utc": asof, "ticker": f"{series}{city}-26{date}-B{thr}",
            "fair_prob": fp, "market_mid": mid, "bin_kind": "above", "thr": thr, "lo": None, "hi": None}


def test_fill_cost_invariants():
    # taker pays the ask; cost == ask + taker fee
    assert p.fill_cost_ct(1, 50, 52, "taker", 5.0) == 52 + fee_per_contract_cents(p.QTY, 52, maker=False)
    # maker rests at the side's mid (YES → mid_cents) with a rebate (maker fee < 0) → cheaper than taker
    maker = p.fill_cost_ct(1, 50, 52, "maker", 5.0)
    assert maker == 50 + fee_per_contract_cents(p.QTY, 50, maker=True)
    assert maker < p.fill_cost_ct(1, 50, 52, "taker", 5.0)
    # maker+markout = maker + slippage + adverse markout
    assert abs(p.fill_cost_ct(1, 50, 52, "maker+markout", 5.0) - (maker + p.SLIPPAGE_CENTS + 5.0)) < 1e-9


def test_no_side_stores_no_price():
    # fair_prob 0.2 < mid 0.5 → model favors NO; entry must be the NO ask, not flipped to 100-x
    dec = p.decide(0.2, 50, 4)
    assert dec is not None
    side_bit, ask, edge = dec
    assert side_bit == 0 and ask == 52 and edge > 0        # no_ask = (100-50)+2 = 52


def test_min_edge_leadtime_and_flat():
    assert p.min_edge_for("lead_keyed", 3) == 25      # 0-6h
    assert p.min_edge_for("lead_keyed", 20) == 10     # 18-24h
    assert p.min_edge_for("lead_keyed", 40) == 20     # 24h+
    assert p.min_edge_for("flat_10", 3) == 10 and p.min_edge_for("flat_20", 20) == 20


def test_spread_monotonic_taker_cost():
    # wider spread → higher ask → higher taker cost (a sanity guard on the fill model)
    assert p.fill_cost_ct(1, 50, 51, "taker", 5.0) < p.fill_cost_ct(1, 50, 55, "taker", 5.0)


def test_replay_clean_win_pnl():
    # fair_prob 0.9 vs mid 0.5, actual 90 > thr 80 → YES wins → pnl = 100 - cost > 0
    rows = [_row("LAX", "JUL03", "2026-07-01T12:00:00+00:00", 0.9, 0.5)]
    cells, counts = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    assert counts["settleable"] == 1
    key = (4, "lead_keyed", "taker")
    assert key in cells and cells[key][0][1] > 0


def test_dedup_one_trade_per_contract():
    # two scan snapshots of the SAME bin-ticker collapse to one contract; a 2nd ticker adds one more
    rows = [_row("LAX", "JUL03", "2026-06-30T12:00:00+00:00", 0.9, 0.5),   # same ticker, earlier asof
            _row("LAX", "JUL03", "2026-07-02T12:00:00+00:00", 0.9, 0.5),   # same ticker, later asof
            _row("LAX", "JUL04", "2026-07-02T12:00:00+00:00", 0.9, 0.5)]   # different ticker
    _cells, counts = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    assert counts["settleable"] == 2                                       # 2 contracts, not 3 rows


def test_sameday_row_excluded():
    # asof station-local day == market day → clairvoyant forecast, dropped before scoring
    rows = [_row("LAX", "JUL01", "2026-07-01T20:00:00+00:00", 0.9, 0.5)]
    _cells, counts = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    assert counts["settleable"] == 0 and counts["dropped_sameday"] == 1


def test_leaky_00_06z_row_excluded():
    # a 02Z-on-resolution-day row (old_leak / same-day) must never reach settleable
    rows = [_row("LAX", "JUL02", "2026-07-03T02:00:00+00:00", 0.9, 0.5)]
    _cells, counts = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    assert counts["settleable"] == 0
    assert (counts["dropped_sameday"] + counts["dropped_oldleak"]) >= 1


def test_price_floor_drops_extreme_ask():
    # mid 0.9 → chosen YES ask ≈ 92 > 80 ceiling → no trade in any cell, though the contract is settleable
    rows = [_row("LAX", "JUL03", "2026-07-01T12:00:00+00:00", 0.95, 0.9)]
    cells, counts = p.replay(rows, lambda c, m, d: 95.0, TODAY, 5.0, None)
    assert counts["settleable"] == 1
    assert sum(len(v) for v in cells.values()) == 0


def test_mid_at_rail_excluded():
    rows = [_row("LAX", "JUL03", "2026-07-01T12:00:00+00:00", 0.9, 0.0)]   # mid==0 → not 0<mid<1
    _cells, counts = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    assert counts["settleable"] == 0


def test_leak1_cutoff_split_and_awaiting():
    # a clean pre-guard row scores without the cutoff, is dropped WITH it (contaminated-only)
    rows = [_row("LAX", "JUN27", "2026-06-25T12:00:00+00:00", 0.9, 0.5)]
    c1, c_no = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, None)
    cutoff = datetime.fromisoformat("2026-06-30T01:21:00+00:00")
    _c2, c_yes = p.replay(rows, lambda c, m, d: 90.0, TODAY, 5.0, cutoff)
    assert c_no["settleable"] == 1 and sum(len(v) for v in c1.values()) > 0
    assert c_yes["settleable"] == 0 and c_yes["dropped_leak1"] == 1
    # awaiting_actual is substantiated: a clean row with no cached actual is counted, not scored
    _c3, c_await = p.replay(rows, lambda c, m, d: None, TODAY, 5.0, None)
    assert c_await["settleable"] == 0 and c_await["awaiting_actual"] == 1


def _grid(**cells):
    return cells


def test_verdict_inconclusive_when_leakfree_empty():
    cont = {(4, "lead_keyed", "taker"): {"n": 100, "n_events": 50, "ci": (18.0, 1.4, 36.0)}}
    v = p._verdict({}, cont)                                # leak-free empty
    assert v["headline"].startswith("INCONCLUSIVE")
    assert "LEAKAGE" in v["headline"] and "spurious" in v["headline"]


def test_verdict_pays_only_on_leakfree():
    lf = {(8, "lead_keyed", "maker+markout"): {"n": 200, "n_events": 15, "ci": (5.0, 2.0, 9.0)},
          (4, "lead_keyed", "taker"): {"n": 200, "n_events": 15, "ci": (6.0, 3.0, 10.0)},
          (2, "flat_10", "taker"): {"n": 200, "n_events": 15, "ci": (4.0, 1.0, 8.0)}}
    assert p._verdict(lf, {}).__getitem__("headline").startswith("PAYS")


def test_verdict_does_not_pay():
    lf = {(4, "lead_keyed", "taker"): {"n": 200, "n_events": 15, "ci": (0.0, -4.0, 4.0)},   # realistic spans 0
          (2, "flat_10", "taker"): {"n": 200, "n_events": 15, "ci": (-5.0, -9.0, -1.0)}}     # optimistic <0
    assert p._verdict(lf, {})["headline"].startswith("DOES NOT PAY")


def test_verdict_does_not_pay_needs_optimistic_power():
    # optimistic cell is clearly <0 but underpowered → must NOT rule DOES NOT PAY (fix #7)
    lf = {(4, "lead_keyed", "taker"): {"n": 200, "n_events": 15, "ci": (0.0, -4.0, 4.0)},
          (2, "flat_10", "taker"): {"n": 8, "n_events": 4, "ci": (-5.0, -9.0, -1.0)}}
    assert p._verdict(lf, {})["headline"].startswith("INCONCLUSIVE")

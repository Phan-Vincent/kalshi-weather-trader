#!/usr/bin/env python3
"""Property-based money-math invariants (2026-07-01 specialized QA, Pass A).

Randomized-input checks for the money primitives — fees, Kelly sizing, order-body mapping,
settlement P&L, calibration. Seeded `random` for determinism (hypothesis not installed).
Complements the example-based suite. No network; temp dirs only.
"""
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from trader.orders import (           # noqa: E402
    _kalshi_taker_fee_cents, _kalshi_maker_rebate_cents, kelly_qty, _v2_order_body,
)
from trader.paper_book import PaperBook   # noqa: E402

_SEED = 1234
_N = 3000


# ── Fees ──────────────────────────────────────────────────────────────────────
def test_prop_taker_fee_nonneg_bounded_and_zero_out_of_range():
    rng = random.Random(_SEED)
    for _ in range(_N):
        price = rng.randint(-5, 105)      # incl. out-of-range
        qty = rng.randint(0, 500)         # incl. zero
        fee = _kalshi_taker_fee_cents(qty, price)
        assert isinstance(fee, int) and fee >= 0
        if qty <= 0 or price <= 0 or price >= 100:
            assert fee == 0
        else:
            assert fee == math.ceil(7 * qty * price * (100 - price) / 10000)  # exact ceiling
            assert fee / qty <= 2.0 + 1e-9                                     # per-contract ≤ 2¢ (max at 50¢)


def test_prop_maker_rebate_nonneg_and_not_above_taker_fee():
    rng = random.Random(_SEED + 1)
    for _ in range(_N):
        price, qty = rng.randint(-5, 105), rng.randint(0, 500)
        reb = _kalshi_maker_rebate_cents(qty, price)
        assert isinstance(reb, int) and reb >= 0
        assert reb <= _kalshi_taker_fee_cents(qty, price)   # maker rate (175/1e6) < taker rate (7/1e4)


def test_prop_taker_fee_monotonic_nondecreasing_in_qty():
    rng = random.Random(_SEED + 2)
    for _ in range(_N):
        price = rng.randint(1, 99)
        q1 = rng.randint(1, 200)
        q2 = q1 + rng.randint(1, 200)
        assert _kalshi_taker_fee_cents(q2, price) >= _kalshi_taker_fee_cents(q1, price)


# ── Kelly sizing ───────────────────────────────────────────────────────────────
def test_prop_kelly_nonneg_and_within_caps():
    rng = random.Random(_SEED + 3)
    for _ in range(_N):
        p = rng.random()
        price = rng.randint(-5, 105)
        bankroll = rng.randint(0, 5_000_000)
        ev_cap = rng.choice([100, 300, 500, 1000])
        qty = kelly_qty(p, price, bankroll, per_event_cap_cents=ev_cap)
        assert isinstance(qty, int) and qty >= 0
        if 0 < price < 100 and bankroll > 0 and qty > 0:
            # the money-safety invariants: cost never exceeds the per-event cap or 5% of bankroll
            assert qty * price <= ev_cap
            assert qty * price <= bankroll * 0.05 + 1e-6


# ── V2 order body mapping (a wrong side/price loses real money) ─────────────────
def test_prop_v2_order_body_side_price_mapping():
    rng = random.Random(_SEED + 4)
    for _ in range(_N):
        price = rng.randint(1, 99)
        qty = rng.randint(1, 50)
        side = rng.choice(["yes", "no"])
        b = _v2_order_body("KXHIGHNY-26JUL01-T90", side, price, qty)
        pr = float(b["price"])
        assert 0.0 <= pr <= 1.0
        assert b["count"] == f"{qty}.00"
        assert b["client_order_id"]
        if side == "yes":
            assert b["side"] == "bid" and round(pr * 100) == price
        else:
            assert b["side"] == "ask" and round(pr * 100) == 100 - price   # buy NO @Pc == sell YES @(100-Pc)


# ── Settlement P&L identity + no double-count ───────────────────────────────────
def test_prop_settlement_pnl_conservation(tmp_path):
    rng = random.Random(_SEED + 5)
    for i in range(300):
        book = PaperBook(state_path=tmp_path / f"b{i}.json")
        book.cash_cents = 1_000_000
        side = rng.choice(["yes", "no"])
        price, qty = rng.randint(1, 99), rng.randint(1, 20)
        book.fill_taker(ticker="KXHIGHNY-26JUL01-T90", side=side, ask_price_cents=price, qty=qty,
                        fair_prob=0.5, edge_cents_post_fee=1.0, confidence="high",
                        market_close_time="2099-01-01T00:00:00+00:00")
        cost = book.open[0]["cost_cents_total"]
        result = rng.choice(["yes", "no"])
        out = book.settle_position("KXHIGHNY-26JUL01-T90", result)
        payout = (100 * qty) if side == result else 0
        assert out[0]["pnl_cents"] == payout - cost      # exact identity (fees inside cost)
        assert book.open == []                            # settled → removed (no double-count)


def test_prop_void_settlement_refunds_stake_pnl_zero(tmp_path):
    # A voided market (Kalshi cancels, e.g. NWS settlement data unavailable) refunds the stake:
    # pnl exactly 0, cash returns to its pre-fill level, position closed, recorded as 'void'.
    rng = random.Random(_SEED + 6)
    for i in range(200):
        book = PaperBook(state_path=tmp_path / f"v{i}.json")
        book.cash_cents = 1_000_000
        side = rng.choice(["yes", "no"])
        price, qty = rng.randint(1, 99), rng.randint(1, 20)
        book.fill_taker(ticker="KXHIGHNY-26JUL01-T90", side=side, ask_price_cents=price, qty=qty,
                        fair_prob=0.5, edge_cents_post_fee=1.0, confidence="high",
                        market_close_time="2099-01-01T00:00:00+00:00")
        out = book.settle_position("KXHIGHNY-26JUL01-T90", "void")
        assert out[0]["pnl_cents"] == 0                   # a void has no P&L
        assert book.cash_cents == 1_000_000               # full stake refunded (back to pre-fill)
        assert book.open == []                            # position closed
        assert book.closed[-1]["settlement_result"] == "void"


# ── Calibration: monotone non-decreasing, output in [0,1] ───────────────────────
def test_prop_calibrate_monotone_in_unit_interval(tmp_path):
    from model.calibrate import IsotonicCalibrator
    rng = random.Random(_SEED + 6)
    bp = tmp_path / "brier-log.jsonl"
    with open(bp, "w") as f:
        for i in range(400):
            p = round(rng.random(), 3)
            y = "yes" if rng.random() < p else "no"       # roughly-calibrated data
            f.write(json.dumps({"record_id": f"r{i}", "status": "settled",
                                "our_prob": p, "outcome": y}) + "\n")
    cal = IsotonicCalibrator().fit(brier_path=bp, min_trades=10)
    prev = -1.0
    for k in range(0, 101):
        v = cal.calibrate(k / 100.0)
        assert 0.0 <= v <= 1.0
        assert v >= prev - 1e-9                            # isotonic ⇒ non-decreasing
        prev = v

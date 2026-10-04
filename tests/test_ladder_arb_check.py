#!/usr/bin/env python3
"""Tests for bin/ladder_arb_check.py — structural arbitrage detection (no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import ladder_arb_check as a


def test_taker_fee():
    assert a.taker_fee_cents(0.50) == 2.0     # ceil(0.07*.25*100)=ceil(1.75)
    assert a.taker_fee_cents(0.20) == 2.0     # ceil(1.12)
    assert a.taker_fee_cents(0.95) == 1.0     # ceil(0.3325)
    assert a.taker_fee_cents(0.0) == 0.0 and a.taker_fee_cents(1.0) == 0.0


def _lad(thr, b, ask, bs=100, asz=100):
    return {"thr": thr, "yes_bid": b, "yes_ask": ask, "bid_size": bs, "ask_size": asz}


def test_monotonicity_no_arb_on_clean_ladder():
    ladder = [_lad(3, 0.98, 0.99), _lad(4, 0.71, 0.74), _lad(5, 0.36, 0.43)]  # monotone, consistent
    assert a.monotonicity_arbs(ladder, min_net_c=0.0) == []


def test_monotonicity_detects_violation():
    # higher threshold (4) YES bid 0.80 > lower threshold (3) YES ask 0.60 → lock
    ladder = [_lad(3, 0.50, 0.60), _lad(4, 0.80, 0.85)]
    out = a.monotonicity_arbs(ladder, min_net_c=0.0)
    assert len(out) == 1
    v = out[0]
    assert v["buy_yes_thr"] == 3 and v["sell_yes_thr"] == 4
    assert v["gross_c"] == 20.0                      # (0.80-0.60)*100
    assert v["fee_c"] == 4.0                          # taker(0.60)=2 + taker(1-0.80=0.20)=2
    assert v["net_c"] == 16.0 and v["fill_size"] == 100


def test_partition_buy_all_arb():
    bins = [{"yes_bid": 0.28, "yes_ask": 0.30, "bid_size": 50, "ask_size": 40}] * 3   # Σask=0.90
    out = a.partition_arbs(bins, min_net_c=0.0)
    buy = [o for o in out if o["kind"] == "buy_all"]
    assert buy and buy[0]["net_c"] == 4.0 and buy[0]["fill_size"] == 40   # (1-.90)*100 - 3*2 fees


def test_partition_sell_all_arb():
    bins = [{"yes_bid": 0.40, "yes_ask": 0.42, "bid_size": 30, "ask_size": 30}] * 3   # Σbid=1.20
    out = a.partition_arbs(bins, min_net_c=0.0)
    sell = [o for o in out if o["kind"] == "sell_all"]
    assert sell and sell[0]["net_c"] == 14.0            # (1.20-1)*100 - 3*taker(0.60)=6


def test_partition_no_arb_when_straddling_one():
    bins = [{"yes_bid": 0.30, "yes_ask": 0.40, "bid_size": 10, "ask_size": 10}] * 3   # Σbid=.9 Σask=1.2
    assert a.partition_arbs(bins, min_net_c=0.01) == []


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    sys.exit(0 if not failed else 1)

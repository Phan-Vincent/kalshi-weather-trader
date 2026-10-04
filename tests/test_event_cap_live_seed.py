#!/usr/bin/env python3
"""Regression for the 2026-07-08 audit: the premium-LIVE per-event $ cap was inert because
check_event_limit summed an EMPTY _open_positions — the live path loads open_positions=[] from
risk-state and never calls record_order, so the $5/event cap collapsed to a per-ORDER check and
multiple STRIKES of one event stacked across cycles (dedup is same-ticker only).

RiskGate.seed_open_positions (from the synced held book) + add_open_position (per in-run placement)
restore it. These tests exercise those methods THROUGH the exact mapping bin/paper_trade.py applies
from book.open (qty -> count, avg_entry_cents -> avg_cost_cents).

Run: python3 -m pytest tests/test_event_cap_live_seed.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from trader.risk import RiskGate
from paper_trade import _resting_exposure  # noqa: E402


def _gate():
    r = RiskGate(_state_dir=Path(tempfile.mkdtemp()))   # empty dir → loads open_positions=[]
    r.per_event_max_position_dollars = 5                # $5/event (premium-live)
    return r


def _seed_from_book(r, book_open):
    """Exactly the mapping bin/paper_trade.py applies from book.open (which has qty + avg_entry_cents,
    NOT count/avg_cost_cents — the field-name trap the fix had to get right)."""
    r.seed_open_positions([
        {"ticker": p.get("ticker"), "count": p.get("qty", 0), "avg_cost_cents": p.get("avg_entry_cents", 0)}
        for p in book_open
    ])


def test_held_strike_caps_a_different_strike_of_same_event():
    r = _gate()
    # $4.00 held on B91.5 of the NOLA 07-08 event (book.open shape)
    _seed_from_book(r, [{"ticker": "KXHIGHTNOLA-26JUL08-B91.5", "qty": 10, "avg_entry_cents": 40}])
    # a $4.00 candidate on a DIFFERENT strike of the SAME event → $8 > $5 → blocked
    assert r.check_event_limit("KXHIGHTNOLA-26JUL08-B93.5", 10, 40) is False
    # a small candidate that keeps the event total <= $5 still passes ($4.00 + $0.80)
    assert r.check_event_limit("KXHIGHTNOLA-26JUL08-B93.5", 2, 40) is True


def test_empty_seed_is_the_old_per_order_behavior():
    r = _gate()
    r.seed_open_positions([])                 # the live reality before the fix
    # with nothing held, a lone $4 order is correctly allowed (per-order check)
    assert r.check_event_limit("KXHIGHTNOLA-26JUL08-B91.5", 10, 40) is True


def test_add_open_position_aggregates_within_one_run():
    r = _gate()
    r.seed_open_positions([])
    r.add_open_position("KXHIGHTHOU-26JUL08-B93.5", 10, 40)     # placed $4 on strike A this run
    # a second strike of the SAME event later in the run must see the first → $8 > $5 → blocked
    assert r.check_event_limit("KXHIGHTHOU-26JUL08-B95.5", 10, 40) is False
    # a different event is unaffected
    assert r.check_event_limit("KXHIGHTDAL-26JUL08-B99.5", 10, 40) is True


def test_reseeding_replaces_does_not_accumulate():
    r = _gate()
    book = [{"ticker": "KXHIGHTATL-26JUL08-B92.5", "qty": 10, "avg_entry_cents": 40}]
    _seed_from_book(r, book)
    _seed_from_book(r, book)                  # a fresh cycle re-seeds the SAME held book
    # total held must be $4 (not $8): $0.80 candidate fits, $2 candidate breaches
    assert r.check_event_limit("KXHIGHTATL-26JUL08-B94.5", 2, 40) is True
    assert r.check_event_limit("KXHIGHTATL-26JUL08-B94.5", 5, 40) is False


def test_seed_skips_tickerless_rows_and_tolerates_missing_cost():
    r = _gate()
    _seed_from_book(r, [{"qty": 10, "avg_entry_cents": 40},                       # no ticker → skipped
                        {"ticker": "KXHIGHTDC-26JUL08-B88.5", "qty": 5}])          # no cost → 0, no crash
    # only the DC row survived and contributes $0 → a $4 candidate passes (no false block)
    assert r.check_event_limit("KXHIGHTDC-26JUL08-B90.5", 10, 40) is True


# ── QA-01 residual (2026-07-09): resting UNFILLED makers must count toward the event cap ──

def _seed_book_plus_resting(r, book_open, resting, basis=None):
    """Exactly the combined mapping bin/paper_trade.py applies (held book + resting notional)."""
    r.seed_open_positions([
        {"ticker": p.get("ticker"), "count": p.get("qty", 0), "avg_cost_cents": p.get("avg_entry_cents", 0)}
        for p in book_open
    ] + _resting_exposure(resting, basis or {}))


def test_resting_exposure_maps_side_price_and_remaining_count():
    items = _resting_exposure([
        {"ticker": "KXHIGHTHOU-26JUL10-B93.5", "side": "yes", "yes_price": 40, "no_price": 60,
         "remaining_count": 7, "count": 10},                    # partial fill → only 7 resting
        {"ticker": "KXHIGHTHOU-26JUL10-B95.5", "side": "no", "yes_price": 65, "no_price": 35,
         "count": 4},                                           # no remaining_count → count fallback
    ], {})
    assert items == [
        {"ticker": "KXHIGHTHOU-26JUL10-B93.5", "count": 7, "avg_cost_cents": 40},   # yes → yes_price
        {"ticker": "KXHIGHTHOU-26JUL10-B95.5", "count": 4, "avg_cost_cents": 35},   # no → no_price
    ]


def test_resting_exposure_drops_unusable_rows():
    items = _resting_exposure([
        {"side": "yes", "yes_price": 40, "remaining_count": 5},                       # no ticker
        {"ticker": "KXHIGHTDC-26JUL10-B88.5", "side": "sell?", "yes_price": 40,
         "remaining_count": 5},                                                       # bad side
        {"ticker": "KXHIGHTDC-26JUL10-B88.5", "side": "yes", "action": "sell",
         "yes_price": 40, "remaining_count": 5},                                      # exits don't add exposure
        {"ticker": "KXHIGHTDC-26JUL10-B88.5", "side": "yes", "yes_price": 40,
         "remaining_count": 0},                                                       # fully filled
        {"ticker": "KXHIGHTDC-26JUL10-B88.5", "side": "yes", "remaining_count": 5,
         "price": 0.40},                                                              # bare 'price' NEVER read
    ], {})
    assert items == []


def test_resting_exposure_basis_fallback_when_side_price_missing():
    # No yes_price on the order object → fall back to the posted_live basis (never bare 'price')
    items = _resting_exposure(
        [{"ticker": "KXHIGHTATL-26JUL10-B92.5", "side": "yes", "order_id": "oid-1",
          "remaining_count": 5, "price": 0.40}],
        {"oid-1": {"limit_price_cents": 42}},
    )
    assert items == [{"ticker": "KXHIGHTATL-26JUL10-B92.5", "count": 5, "avg_cost_cents": 42}]


def test_resting_maker_on_strike_a_caps_strike_b_of_same_event():
    r = _gate()
    # $4.00 RESTING (unfilled) on strike A — the exact QA-01 residual scenario
    _seed_book_plus_resting(r, [], [{"ticker": "KXHIGHTNOLA-26JUL10-B91.5", "side": "yes",
                                     "yes_price": 40, "remaining_count": 10}])
    # a $4.00 candidate on strike B of the SAME event → $8 > $5 → blocked
    assert r.check_event_limit("KXHIGHTNOLA-26JUL10-B93.5", 10, 40) is False
    # a different event is unaffected
    assert r.check_event_limit("KXHIGHTDAL-26JUL10-B99.5", 10, 40) is True


def test_partial_fill_no_double_count():
    r = _gate()
    # 3 ct FILLED (in book.open) + 7 ct still RESTING on the SAME ticker = 10 ct total, not 13
    _seed_book_plus_resting(
        r,
        [{"ticker": "KXHIGHTSEA-26JUL10-B72.5", "qty": 3, "avg_entry_cents": 40}],
        [{"ticker": "KXHIGHTSEA-26JUL10-B72.5", "side": "yes", "yes_price": 40,
          "remaining_count": 7, "count": 10}],
    )
    # event total = 10 ct × 40¢ = $4.00 → a 2-ct (80¢) candidate fits under $5 …
    assert r.check_event_limit("KXHIGHTSEA-26JUL10-B74.5", 2, 40) is True
    # … but a 3-ct ($1.20) candidate breaches; had we double-counted (13 ct = $5.20)
    # even the 2-ct candidate would have been blocked above
    assert r.check_event_limit("KXHIGHTSEA-26JUL10-B74.5", 3, 40) is False

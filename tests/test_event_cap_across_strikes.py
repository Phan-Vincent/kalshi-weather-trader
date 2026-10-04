#!/usr/bin/env python3
"""
tests/test_event_cap_across_strikes.py — Regression for the 2026-07-06 audit finding
that the "per-event" cap (check_event_limit) matched the EXACT ticker, so as the
premium band drifts across adjacent strikes (B45 one cycle, B47 the next) each new
strike passed the cap and per-event exposure multiplied past the intended limit,
concentrating live losses on a single weather outcome.

The cap now aggregates over the EVENT (series+city+date).

Run: python3 -m pytest tests/test_event_cap_across_strikes.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.risk import RiskGate, _event_key


def _gate(open_positions):
    d = tempfile.mkdtemp()
    r = RiskGate(_state_dir=Path(d))
    r.per_event_max_position_dollars = 5      # $5/event cap (premium-live)
    r._open_positions = open_positions
    return r


def test_event_key_drops_strike():
    assert _event_key("KXHIGHTHOU-26JUL06-B45") == "KXHIGHTHOU-26JUL06"
    assert _event_key("KXHIGHTHOU-26JUL06-B47") == "KXHIGHTHOU-26JUL06"
    assert _event_key("KXTEMPNYCH-26MAY2816-T82") == "KXTEMPNYCH-26MAY2816"


def test_adjacent_strike_counts_against_same_event():
    # $4.50 already held on the B45 strike of the HOU 07-06 event.
    held = [{"ticker": "KXHIGHTHOU-26JUL06-B45", "count": 10, "avg_cost_cents": 45}]
    r = _gate(held)
    # A new order on a DIFFERENT strike (B47) of the SAME event that would push total
    # past $5 must now be rejected (old exact-ticker code let it through).
    assert r.check_event_limit("KXHIGHTHOU-26JUL06-B47", 10, 45) is False, (
        "adjacent strike of the same event must count against the per-event cap"
    )


def test_different_event_not_capped_together():
    held = [{"ticker": "KXHIGHTHOU-26JUL06-B45", "count": 10, "avg_cost_cents": 45}]
    r = _gate(held)
    # A different city/date is a different event — must NOT be blocked by HOU's fill.
    assert r.check_event_limit("KXHIGHTDAL-26JUL06-B89", 10, 45) is True
    assert r.check_event_limit("KXHIGHTHOU-26JUL07-B95", 10, 45) is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

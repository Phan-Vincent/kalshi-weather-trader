#!/usr/bin/env python3
"""
tests/test_daily_window_pacific.py — Regression for the 2026-07-06 audit finding that
the DAILY loss stop keyed its window to the UTC calendar date, which rolls at ~16-17:00
PT — inside the 9:20-19:20 PT slot schedule — so a stop tripped in the afternoon silently
re-armed with a fresh $20 before the evening slots. The window now keys to the Pacific
trading day (rolls at PT midnight, outside trading hours).

Run: python3 -m pytest tests/test_daily_window_pacific.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.risk import _pacific_trading_day


def test_afternoon_pt_stays_same_pt_day_across_utc_midnight():
    # 23:30Z on Jul 6 = 16:30 PT Jul 6 (afternoon, mid-schedule). Must be Jul 6.
    assert _pacific_trading_day(datetime(2026, 7, 6, 23, 30, tzinfo=timezone.utc)) == "2026-07-06"
    # 02:00Z Jul 7 = 19:00 PT Jul 6 (last slot). STILL Jul 6 — the old UTC keying already
    # rolled to Jul 7 here, re-arming the stop mid-evening.
    assert _pacific_trading_day(datetime(2026, 7, 7, 2, 0, tzinfo=timezone.utc)) == "2026-07-06"


def test_rolls_at_pt_midnight_not_utc_midnight():
    # 08:00Z Jul 7 = 01:00 PT Jul 7 — new trading day (PT midnight passed, no slots run then).
    assert _pacific_trading_day(datetime(2026, 7, 7, 8, 0, tzinfo=timezone.utc)) == "2026-07-07"
    # The morning slots (16:20Z = 09:20 PT) are the SAME PT day as the prior evening slots.
    assert _pacific_trading_day(datetime(2026, 7, 7, 16, 20, tzinfo=timezone.utc)) == "2026-07-07"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

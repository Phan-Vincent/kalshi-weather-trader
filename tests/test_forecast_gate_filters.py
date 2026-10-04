#!/usr/bin/env python3
"""Stage-1 gate filters (2026-06-29 rebuild): leak-floor + same-day exclusion + matched pairwise.

The rebuilt gate compares MODEL-EDGE arms forecast-edge vs climo-edge on realized settled
P&L, but only on trades opened on/after the LEAK-1 fix and NOT on same-day markets (post-fix
both arms price same-day on climatology → no forecast signal). Runs under plain python3."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from validate_forecast_gate import (  # noqa: E402
    _ticker_date, _is_sameday, _filter, _matched_pairwise,
)


def _rec(ticker, opened, pnl=0, qty=4, side="yes"):
    return {"ticker": ticker, "opened_utc": opened, "pnl_cents": pnl, "qty": qty, "side": side}


def test_ticker_date_parse():
    assert _ticker_date("KXHIGHTNY-26JUN29-B80.5") == "2026-06-29"
    assert _ticker_date("KXLOWTSEA-26JUL02-T70") == "2026-07-02"
    assert _ticker_date("garbage") is None


def test_is_sameday():
    assert _is_sameday(_rec("KXHIGHTNY-26JUN29-B80", "2026-06-29T18:00:00+00:00")) is True
    assert _is_sameday(_rec("KXHIGHTNY-26JUN30-B80", "2026-06-29T18:00:00+00:00")) is False


def test_filter_since_and_sameday():
    recs = [
        _rec("KXA-26JUN28-B1", "2026-06-28T10:00:00+00:00", 100),  # before since → dropped
        _rec("KXB-26JUN29-B1", "2026-06-29T10:00:00+00:00", 100),  # same-day → dropped
        _rec("KXC-26JUN30-B1", "2026-06-29T10:00:00+00:00", 100),  # opened post-fix, future mkt → kept
        _rec("KXD-26JUL01-B1", "2026-06-30T10:00:00+00:00", 100),  # kept
    ]
    kept = _filter(recs, since="2026-06-29", drop_sameday=True)
    tks = {r["ticker"] for r in kept}
    assert tks == {"KXC-26JUN30-B1", "KXD-26JUL01-B1"}, tks
    # disabling the floor + keeping same-day keeps all (qty>0)
    assert len(_filter(recs, since="", drop_sameday=False)) == 4


def test_matched_pairwise_per_contract_diff():
    # same ticker in both arms; forecast +200c/4 = +50¢/ct, climo +40c/4 = +10¢/ct → diff +40
    f = [_rec("KXE-26JUL01-B1", "2026-06-30T10:00:00+00:00", 200)]
    b = [_rec("KXE-26JUL01-B1", "2026-06-30T10:00:00+00:00", 40)]
    pw = _matched_pairwise(f, b)
    assert pw["matched_markets"] == 1, pw
    pt, lo, hi = pw["diff_ci"]
    assert pt is not None and abs(pt - 40.0) < 1e-6, pw["diff_ci"]
    # no overlap → no matched markets
    b2 = [_rec("KXZ-26JUL01-B1", "2026-06-30T10:00:00+00:00", 40)]
    assert _matched_pairwise(f, b2)["matched_markets"] == 0


if __name__ == "__main__":
    test_ticker_date_parse()
    test_is_sameday()
    test_filter_since_and_sameday()
    test_matched_pairwise_per_contract_diff()
    print("ok")

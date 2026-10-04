#!/usr/bin/env python3
"""P0b (2026-07-04): PaperBook._log_lifecycle must round-trip the posted_live book-context
telemetry, not just its fixed canonical columns. Before the fix, the row was rebuilt from a
whitelist (event/ts/ticker/side/limit_price_cents/qty/paper_order_id/fair_prob_at_post) and every
other caller key — including spread_cents_at_post and the aggr/depth fields the fill model and
bin/markout_kill_test.py depend on — was dropped at write time (0/200 live rows carried them).
Plain python3 (framework, per repo notes)."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

# The exact P0b payload bin/paper_trade.py posts on a successful live placement (~line 884).
POSTED_LIVE_REC = {
    "ticker": "KXHIGHTMIA-26JUN25-B90.5", "side": "yes",
    "limit_price_cents": 33, "qty": 5,
    "paper_order_id": "kalshi-order-abc123",
    "fair_prob_at_post": 0.41,
    "yes_bid_at_post": 30, "yes_ask_at_post": 34,
    "no_bid_at_post": 66, "no_ask_at_post": 70,
    "spread_cents_at_post": 4,
    "side_bid_qty_at_post": 12.0,
}


def _book_in(tmpd):
    os.environ["KALSHI_WEATHER_STATE_DIR"] = tmpd
    from trader.paper_book import PaperBook
    return PaperBook()


def _read_rows(book):
    path = book._lifecycle_path()
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_posted_live_row_round_trips_book_context():
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    try:
        book = _book_in(tmpd)
        book._log_lifecycle("posted_live", dict(POSTED_LIVE_REC))
        rows = _read_rows(book)
        assert len(rows) == 1, rows
        row = rows[0]
        # canonical columns still present & correct
        assert row["event"] == "posted_live"
        assert row["ticker"] == POSTED_LIVE_REC["ticker"]
        assert row["side"] == "yes"
        assert row["limit_price_cents"] == 33
        assert row["fair_prob_at_post"] == 0.41
        # the P0b book-context fields that USED to be dropped now survive the write
        assert row["spread_cents_at_post"] == 4, "spread_cents_at_post must round-trip"
        assert row["yes_bid_at_post"] == 30
        assert row["yes_ask_at_post"] == 34
        assert row["no_bid_at_post"] == 66
        assert row["no_ask_at_post"] == 70
        assert row["side_bid_qty_at_post"] == 12.0
        # markout_kill_test.py's captured-half-spread predicate must now match this row
        assert (
            row.get("event") == "posted_live"
            and isinstance(row.get("spread_cents_at_post"), (int, float))
            and row.get("spread_cents_at_post") > 0
        ), "row must satisfy markout_kill_test's captured-half-spread gate"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


def test_canonical_fields_stay_authoritative():
    # A stray rec key named "event"/"ts" must NOT overwrite the method-controlled columns.
    prev = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    tmpd = tempfile.mkdtemp()
    try:
        book = _book_in(tmpd)
        book._log_lifecycle("posted_live", {"ticker": "T", "side": "no", "event": "SPOOFED", "ts": "1999", "extra": 7})
        row = _read_rows(book)[0]
        assert row["event"] == "posted_live", "caller rec must not override the event column"
        assert row["ts"] != "1999", "caller rec must not override the ts column"
        assert row["extra"] == 7, "unknown scalar keys still pass through"
    finally:
        os.environ.pop("KALSHI_WEATHER_STATE_DIR", None) if prev is None else os.environ.__setitem__("KALSHI_WEATHER_STATE_DIR", prev)
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == "__main__":
    test_posted_live_row_round_trips_book_context(); print("[PASS] posted_live row round-trips spread_cents_at_post + book context")
    test_canonical_fields_stay_authoritative();      print("[PASS] canonical event/ts columns stay authoritative; extra keys pass through")
    print("\nP0b lifecycle pass-through tests pass.")

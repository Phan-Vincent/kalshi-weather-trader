#!/usr/bin/env python3
"""
tests/test_brier_backfill.py — Verify backfill idempotency, dedup,
and settled-count consistency.

Run: python3 tests/test_brier_backfill.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from trader.brier import BrierLogger


def _tmp_brier_path():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return Path(path)


def _tmp_settle_path():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return Path(path)


def _make_backfill(brier_path: Path, settle_path: Path):
    """Import backfill with overridden paths."""
    # We monkey-patch the module-level constants before import
    import bin.backfill_brier as backfill_mod
    backfill_mod.BRIER_PATH = brier_path
    backfill_mod.SETTLE_PATH = settle_path
    return backfill_mod.backfill


def test_backfill_basic_match():
    """A single open record + single settlement should backfill correctly."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        our_prob=0.75,
        market_prob=0.50,
        qty=10,
        price_cents=50,
        mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )

    settlement = {
        "ticker": "KXHIGHTEST-01JAN01-T50",
        "side": "yes",
        "qty": 10,
        "settlement_result": "yes",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 50,
        "fair_prob_at_open": 0.75,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-001",
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(settlement) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    result = backfill()

    assert result["matched"] == 1, f"Expected 1 match, got {result['matched']}"
    assert result["unmatched"] == 0, f"Expected 0 unmatched, got {result['unmatched']}"

    # Verify brier record is now in-place settled
    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    assert len(recs) == 1
    assert recs[0]["status"] == "settled"
    assert recs[0]["actual"] == 1.0
    assert recs[0]["paper_order_id"] == "paper-001"
    assert "our_brier" in recs[0]

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] basic backfill matches and patches in-place")


def test_backfill_idempotent_no_inflation():
    """Running backfill twice on the same data must not inflate settled count."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    for i in range(5):
        brier.log_prediction(
            ticker=f"T{i}", side="yes", our_prob=0.60, market_prob=0.50,
            qty=1, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )

    # Create 3 unique settlements
    for i in range(3):
        s = {
            "ticker": f"T{i}", "side": "yes", "qty": 1,
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": f"paper-{i}",
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)

    # First run
    r1 = backfill()
    assert r1["matched"] == 3
    settled_after_1 = sum(1 for l in open(brier_path) if l.strip() and json.loads(l)["status"] == "settled")
    assert settled_after_1 == 3, f"Expected 3 settled after first run, got {settled_after_1}"

    # Second run — must be a no-op (skip already processed)
    r2 = backfill()
    assert r2.get("skipped") == 3, f"Expected skip=3 on re-run, got {r2}"
    assert r2["matched"] == 0, f"Expected 0 matches on re-run, got {r2['matched']}"

    settled_after_2 = sum(1 for l in open(brier_path) if l.strip() and json.loads(l)["status"] == "settled")
    assert settled_after_2 == 3, f"Expected 3 settled after second run, got {settled_after_2}"

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill idempotent — re-run does not inflate settled count")


def test_backfill_dedupes_duplicate_settlements():
    """Duplicate settlement records (same paper_order_id) must only match once."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    for i in range(3):
        brier.log_prediction(
            ticker=f"T{i}", side="yes", our_prob=0.60, market_prob=0.50,
            qty=1, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )

    # Same paper_order_id duplicated with different qty/pnl
    for _ in range(2):
        s = {
            "ticker": "T0", "side": "yes", "qty": 1,
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": "paper-dup",
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(s) + "\n")

    # One unique settlement for T1
    s2 = {
        "ticker": "T1", "side": "yes", "qty": 1,
        "settlement_result": "yes",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 50, "fair_prob_at_open": 0.60,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-unique",
    }
    with open(settle_path, "a") as f:
        f.write(json.dumps(s2) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    result = backfill()

    # Deduplication should reduce 3 raw settlements to 2 unique
    assert result["matched"] == 2, f"Expected 2 matches after dedup, got {result['matched']}"
    assert result["unmatched"] == 0, f"Expected 0 unmatched, got {result['unmatched']}"
    assert result["deduped_from"] == 3

    # Only 2 brier records should be settled
    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in recs if r["status"] == "settled"]
    open_recs = [r for r in recs if r["status"] == "open"]
    assert len(settled) == 2, f"Expected 2 settled, got {len(settled)}"
    assert len(open_recs) == 1, f"Expected 1 still open, got {len(open_recs)}"

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill deduplicates duplicate settlements by paper_order_id")


def test_backfill_incremental_new_settlements():
    """Adding new settlement records after initial backfill should process only new ones."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    for i in range(4):
        brier.log_prediction(
            ticker=f"T{i}", side="yes", our_prob=0.60, market_prob=0.50,
            qty=1, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )

    # First batch: 2 settlements
    for i in range(2):
        s = {
            "ticker": f"T{i}", "side": "yes", "qty": 1,
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": f"paper-{i}",
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    r1 = backfill()
    assert r1["matched"] == 2

    # Second batch: add 2 more settlements
    for i in range(2, 4):
        s = {
            "ticker": f"T{i}", "side": "yes", "qty": 1,
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": f"paper-{i}",
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(s) + "\n")

    r2 = backfill()
    assert r2["matched"] == 2, f"Expected 2 new matches, got {r2['matched']}"
    assert r2.get("skipped") == 2, f"Expected 2 skipped, got {r2}"

    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in recs if r["status"] == "settled"]
    assert len(settled) == 4, f"Expected 4 settled total, got {len(settled)}"

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] incremental backfill processes only new settlements")


def test_settled_count_matches_settlement_log():
    """After backfill, Brier summary n_trades should equal unique settlement count."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    for i in range(10):
        brier.log_prediction(
            ticker=f"T{i}", side="yes", our_prob=0.60, market_prob=0.50,
            qty=1, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )

    # 7 unique settlements + 3 duplicates
    unique_oids = [f"paper-{i}" for i in range(7)]
    for oid in unique_oids:
        s = {
            "ticker": oid.replace("paper-", "T"), "side": "yes", "qty": 1,
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": oid,
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(s) + "\n")

    # Add 3 duplicates of paper-0
    for _ in range(3):
        dup = {
            "ticker": "T0", "side": "yes", "qty": 99,  # wrong qty
            "settlement_result": "yes",
            "settled_at_utc": "2026-01-02T00:00:00+00:00",
            "entry_cents": 50, "fair_prob_at_open": 0.60,
            "opened_utc": "2026-01-01T00:00:00+00:00",
            "paper_order_id": "paper-0",
        }
        with open(settle_path, "a") as f:
            f.write(json.dumps(dup) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    result = backfill()
    assert result["matched"] == 7

    summary = brier.summary()
    assert summary["n_trades"] == 7, f"Expected n_trades=7, got {summary['n_trades']}"
    assert summary["n_open"] == 3, f"Expected n_open=3, got {summary['n_open']}"

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] settled count in summary matches deduped settlement count")


def test_backfill_brier_polarity_yes_side():
    """Backfilled YES outcome on YES side: our_brier should be (our_prob - 1)^2."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.70, market_prob=0.60,
        qty=1, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )

    s = {
        "ticker": "T", "side": "yes", "qty": 1,
        "settlement_result": "yes",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 60, "fair_prob_at_open": 0.70,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-yes",
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    backfill()

    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in recs if r["status"] == "settled"][0]
    expected = round((0.70 - 1.0) ** 2, 6)  # 0.09
    assert abs(settled["our_brier"] - expected) < 1e-6, \
        f"YES-side YES-outcome: expected {expected}, got {settled['our_brier']}"
    assert settled["our_prob_for_outcome"] == 0.70
    assert settled["actual"] == 1.0

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill Brier polarity correct: YES side, YES outcome")


def test_backfill_brier_polarity_no_side():
    """Backfilled NO outcome on NO side: our_brier should be (our_prob_for_no - 1)^2."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="T", side="no", our_prob=0.70, market_prob=0.60,
        qty=1, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    # our_prob=0.70 means P(yes)=0.70, so P(no)=0.30.
    # We traded NO at 60 cents. Outcome = NO won.
    # our_prob_for_outcome = P(no) = 0.30.
    # Brier = (0.30 - 1.0)^2 = 0.49

    s = {
        "ticker": "T", "side": "no", "qty": 1,
        "settlement_result": "no",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 60, "fair_prob_at_open": 0.70,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-no",
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    backfill()

    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in recs if r["status"] == "settled"][0]
    expected = round((0.30 - 1.0) ** 2, 6)  # 0.49
    assert abs(settled["our_brier"] - expected) < 1e-6, \
        f"NO-side NO-outcome: expected {expected}, got {settled['our_brier']}"
    assert abs(settled["our_prob_for_outcome"] - 0.30) < 1e-6
    assert settled["actual"] == 0.0

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill Brier polarity correct: NO side, NO outcome")


def test_backfill_brier_polarity_yes_side_no_outcome():
    """Backfilled NO outcome on YES side: our_brier should be (1 - our_prob - 1)^2 = our_prob^2."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.70, market_prob=0.60,
        qty=1, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    # our_prob=0.70 means P(yes)=0.70. Outcome = NO won.
    # our_prob_for_outcome = P(no) = 0.30.
    # Brier = (0.30 - 1.0)^2 = 0.49

    s = {
        "ticker": "T", "side": "yes", "qty": 1,
        "settlement_result": "no",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 60, "fair_prob_at_open": 0.70,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-yes-no",
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    backfill()

    with open(brier_path) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    settled = [r for r in recs if r["status"] == "settled"][0]
    expected = round((0.30 - 1.0) ** 2, 6)  # 0.49
    assert abs(settled["our_brier"] - expected) < 1e-6, \
        f"YES-side NO-outcome: expected {expected}, got {settled['our_brier']}"
    assert abs(settled["our_prob_for_outcome"] - 0.30) < 1e-6
    assert settled["actual"] == 0.0

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill Brier polarity correct: YES side, NO outcome")


def test_backfill_legacy_guard_prevents_unsafe_rerun():
    """If brier log has legacy settled records (with actual but no paper_order_id),
    backfill must abort to prevent accidental re-matching."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.60, market_prob=0.50,
        qty=1, price_cents=50, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    # Manually create a legacy settled append record (has actual but no paper_order_id)
    with open(brier_path, "a") as f:
        f.write(json.dumps({
            "record_id": rid,
            "status": "settled",
            "outcome": "yes",
            "actual": 1.0,
            "our_brier": 0.01,
            "market_brier": 0.04,
            "our_prob_for_outcome": 0.60,
            "market_prob_for_outcome": 0.50,
        }) + "\n")

    s = {
        "ticker": "T", "side": "yes", "qty": 1,
        "settlement_result": "yes",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 50, "fair_prob_at_open": 0.60,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        "paper_order_id": "paper-legacy",
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    result = backfill()

    assert result.get("aborted") is True, f"Expected abort for legacy state, got {result}"
    assert result["legacy_settled_count"] == 1
    assert result["settlements"] == 1

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] legacy guard prevents unsafe re-backfill on old state")


def test_backfill_no_paper_order_id_fallback():
    """Settlements without paper_order_id should still be processed (no dedup)."""
    brier_path = _tmp_brier_path()
    settle_path = _tmp_settle_path()

    brier = BrierLogger(log_path=brier_path)
    brier.log_prediction(
        ticker="T", side="yes", our_prob=0.60, market_prob=0.50,
        qty=1, price_cents=50, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )

    s = {
        "ticker": "T", "side": "yes", "qty": 1,
        "settlement_result": "yes",
        "settled_at_utc": "2026-01-02T00:00:00+00:00",
        "entry_cents": 50, "fair_prob_at_open": 0.60,
        "opened_utc": "2026-01-01T00:00:00+00:00",
        # No paper_order_id
    }
    with open(settle_path, "w") as f:
        f.write(json.dumps(s) + "\n")

    backfill = _make_backfill(brier_path, settle_path)
    result = backfill()
    assert result["matched"] == 1
    assert result["unmatched"] == 0

    os.unlink(brier_path)
    os.unlink(settle_path)
    print("[PASS] backfill handles settlements without paper_order_id")


if __name__ == "__main__":
    test_backfill_basic_match()
    test_backfill_idempotent_no_inflation()
    test_backfill_dedupes_duplicate_settlements()
    test_backfill_incremental_new_settlements()
    test_settled_count_matches_settlement_log()
    test_backfill_brier_polarity_yes_side()
    test_backfill_brier_polarity_no_side()
    test_backfill_brier_polarity_yes_side_no_outcome()
    test_backfill_legacy_guard_prevents_unsafe_rerun()
    test_backfill_no_paper_order_id_fallback()
    print("\nAll brier backfill tests passed.")

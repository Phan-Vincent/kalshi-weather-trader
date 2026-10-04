#!/usr/bin/env python3
"""
tests/test_brier.py — Unit tests for trader/brier.py.

Run with pytest or directly: python3 tests/test_brier.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from trader.brier import BrierLogger


def _tmp_logger():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return BrierLogger(log_path=Path(path)), path


def test_log_prediction_writes_record_and_returns_uuid():
    brier, path = _tmp_logger()
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
    assert rid is not None
    try:
        uuid.UUID(rid)
    except ValueError:
        raise AssertionError("record_id is not a valid UUID")

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["status"] == "open"
    assert rec["ticker"] == "KXHIGHTEST-01JAN01-T50"
    assert rec["side"] == "yes"
    assert rec["our_prob"] == 0.75
    assert rec["market_prob"] == 0.50
    assert rec["qty"] == 10
    assert rec["price_cents"] == 50
    assert rec["mode"] == "taker"
    assert rec["source"] == "live"
    os.unlink(path)
    print("[PASS] log_prediction writes JSONL record with required fields, returns valid uuid")


def test_record_outcome_appends_without_rewrite():
    brier, path = _tmp_logger()
    rid = brier.log_prediction(
        ticker="KXHIGHTEST-01JAN01-T50",
        side="yes",
        our_prob=0.70,
        market_prob=0.60,
        qty=10,
        price_cents=60,
        mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )

    # Record outcome
    brier.record_outcome(rid, outcome=True)

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 2

    open_rec = json.loads(lines[0])
    settle_rec = json.loads(lines[1])
    assert open_rec["status"] == "open"
    assert settle_rec["status"] == "settled"
    assert settle_rec["record_id"] == rid
    assert settle_rec["outcome"] == "yes"

    os.unlink(path)
    print("[PASS] record_outcome appends settle record without rewriting file")


def test_summary_pairs_open_and_settled():
    brier, path = _tmp_logger()
    rid1 = brier.log_prediction(
        ticker="T1", side="yes", our_prob=0.70, market_prob=0.60,
        qty=10, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    rid2 = brier.log_prediction(
        ticker="T2", side="yes", our_prob=0.80, market_prob=0.70,
        qty=10, price_cents=70, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid1, outcome=True)
    # rid2 stays open

    summary = brier.summary()
    assert summary["n_trades"] == 1
    assert summary["n_open"] == 1
    os.unlink(path)
    print("[PASS] summary correctly pairs open+settled records, ignores still-open")


def test_brier_math_yes_outcome():
    """p=0.7 for YES, outcome=YES → brier = (0.7-1)^2 = 0.09"""
    brier, path = _tmp_logger()
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.70, market_prob=0.60,
        qty=10, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid, outcome=True)

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    settle = json.loads(lines[1])
    assert abs(settle["our_brier"] - 0.09) < 1e-6, f"Expected 0.09, got {settle['our_brier']}"
    os.unlink(path)
    print("[PASS] Brier math: p=0.7 YES, outcome=YES → brier=0.09")


def test_brier_math_no_outcome():
    """p=0.7 for YES, outcome=NO → our_prob_for_outcome = 0.3, brier = (0.3-1.0)^2 = 0.49"""
    brier, path = _tmp_logger()
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.70, market_prob=0.60,
        qty=10, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid, outcome=False)

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    settle = json.loads(lines[1])
    # our_prob_for_outcome = 1 - 0.70 = 0.30, scored against 1.0 (winning outcome occurred)
    assert abs(settle["our_brier"] - 0.49) < 1e-6, f"Expected 0.49, got {settle['our_brier']}"
    os.unlink(path)
    print("[PASS] Brier math: p=0.7 YES, outcome=NO → brier=0.49")


def test_bss_positive_when_we_beat_market():
    """Our Brier < Market Brier → BSS > 0"""
    brier, path = _tmp_logger()
    # We predict YES at 0.70, market says 0.60. Outcome = YES.
    # our_brier = (0.70 - 1)^2 = 0.09
    # market_brier = (0.60 - 1)^2 = 0.16
    # BSS = 1 - 0.09/0.16 = 0.4375 > 0
    rid = brier.log_prediction(
        ticker="T", side="yes", our_prob=0.70, market_prob=0.60,
        qty=10, price_cents=60, mode="taker",
        timestamp_utc="2026-01-01T00:00:00+00:00",
    )
    brier.record_outcome(rid, outcome=True)

    summary = brier.summary()
    assert summary["brier_skill_score"] > 0, f"Expected BSS > 0, got {summary['brier_skill_score']}"
    os.unlink(path)
    print("[PASS] BSS positive when our_brier < market_brier")


def test_calibration_buckets_approx_zero_diff():
    """100 synthetic predictions at p=0.5 with 50% true rate →
    bucket [40-50%] or [50-60%] should have diff≈0"""
    brier, path = _tmp_logger()
    import random
    random.seed(42)
    for i in range(100):
        rid = brier.log_prediction(
            ticker="T", side="yes", our_prob=0.50, market_prob=0.50,
            qty=10, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        outcome = random.random() < 0.5
        brier.record_outcome(rid, outcome=outcome)

    summary = brier.summary()
    # Find the bucket containing 0.5
    target_bucket = None
    for b in summary["calibration_buckets"]:
        lo, hi = b["range"].replace("%", "").split("-")
        lo = int(lo) / 100.0
        hi = int(hi) / 100.0
        if lo <= 0.5 < hi or (hi == 1.0 and 0.5 == 1.0):
            target_bucket = b
            break

    assert target_bucket is not None, "Could not find bucket for p=0.5"
    assert target_bucket["n"] > 0, "Bucket for p=0.5 should have items"
    assert abs(target_bucket["diff"]) < 0.15, f"Expected diff≈0, got {target_bucket['diff']}"
    os.unlink(path)
    print("[PASS] Calibration buckets: 100 predictions at p=0.5 with 50% true → diff≈0")


def test_gate_status():
    brier, path = _tmp_logger()
    # Less than 150 trades → WAITING
    for i in range(10):
        rid = brier.log_prediction(
            ticker="T", side="yes", our_prob=0.60, market_prob=0.50,
            qty=10, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        brier.record_outcome(rid, outcome=True)
    summary = brier.summary()
    assert summary["gate_status"] == "WAITING", f"Expected WAITING, got {summary['gate_status']}"

    # Now backfill to simulate >=150 with our_brier < market_brier
    # We need to wipe and create 150 records where we beat market
    os.unlink(path)
    brier2, path2 = _tmp_logger()
    for i in range(150):
        rid = brier2.log_prediction(
            ticker="T", side="yes", our_prob=0.90, market_prob=0.50,
            qty=10, price_cents=50, mode="taker",
            timestamp_utc="2026-01-01T00:00:00+00:00",
        )
        brier2.record_outcome(rid, outcome=True)
    summary2 = brier2.summary()
    assert summary2["gate_status"] in ("GREEN", "LIGHT_GREEN"), \
        f"Expected GREEN or LIGHT_GREEN, got {summary2['gate_status']}"
    os.unlink(path2)
    print("[PASS] Gate status: <150 trades → WAITING; ≥150 with positive BSS → GREEN/LIGHT_GREEN")


def test_no_brier_record_id_raises():
    brier, path = _tmp_logger()
    try:
        brier.record_outcome("nonexistent-uuid", outcome=True)
        assert False, "Expected ValueError for missing record"
    except ValueError as e:
        assert "No open record found" in str(e)
    os.unlink(path)
    print("[PASS] record_outcome raises ValueError for missing record_id")


if __name__ == "__main__":
    test_log_prediction_writes_record_and_returns_uuid()
    test_record_outcome_appends_without_rewrite()
    test_summary_pairs_open_and_settled()
    test_brier_math_yes_outcome()
    test_brier_math_no_outcome()
    test_bss_positive_when_we_beat_market()
    test_calibration_buckets_approx_zero_diff()
    test_gate_status()
    test_no_brier_record_id_raises()
    print("\nAll brier tests passed.")

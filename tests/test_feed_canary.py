#!/usr/bin/env python3
"""Tests for bin/feed_canary.py — per-field zero-vs-null degradation (QA 2026-07-03).

Pins the rules that keep the canary from false-paging now that run-cycle.sh runs it
every cycle (17 paper + 6 live/day): for ZERO_OK_FIELDS (flat weather book, all
resting quotes filled/expired) a genuine 0 is a HEALTHY reading — only null (the
CLI/API call itself failed) counts as degraded and a historical 0 breaks a null
streak — while must-be-nonzero fields (balance.available, ...) still treat 0 AND
null as degraded, exactly as before. Isolation: CANARY_LOG is patched to a tmp file
(the real one lives under state/, which tests must never touch), snapshot() is
monkeypatched (no kalshi-cli / network / trader.orders), trader.notify.alert is
captured, and KALSHI_WEATHER_ALERTS=0 ambient — no test can ever page the operator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import feed_canary as fc  # noqa: E402


def _run(monkeypatch, tmp_path, hist_values, snap, min_streak=2):
    """Run evaluate() against synthetic history + snapshot with alerting captured.

    hist_values: list of {field: value} dicts, oldest first — written to a tmp
    CANARY_LOG in the exact jsonl shape evaluate() itself appends."""
    monkeypatch.setenv("KALSHI_WEATHER_ALERTS", "0")   # belt: never deliver
    log = tmp_path / "feed-canary.jsonl"
    with open(log, "w") as f:
        for vals in hist_values:
            rec = {"ts": "2026-07-03T00:00:00+00:00",
                   "fields": {k: {"value": v, "is_zero": v == 0, "is_null": v is None}
                              for k, v in vals.items()}}
            f.write(json.dumps(rec) + "\n")
    monkeypatch.setattr(fc, "CANARY_LOG", log)          # braces: never touch state/
    monkeypatch.setattr(fc, "snapshot", lambda state_dir: dict(snap))
    sent = []
    import trader.notify as notify
    monkeypatch.setattr(notify, "alert",
                        lambda text, key=None, dedup_seconds=3600, **k: sent.append((key, text)))
    res = fc.evaluate("live-premium", min_streak=min_streak, alert=True)
    return res, sent


# ── ZERO_OK_FIELDS: a legitimate 0 must never page ────────────────────────────

def test_flat_book_zero_does_not_page(monkeypatch, tmp_path):
    # The recurring nighttime reality: weather book flat ('Synced 0 positions')
    # and no resting quotes for 2+ cycles after a non-zero baseline. Healthy.
    hist = [{"positions.weather_count": 5, "orders.resting_count": 2},
            {"positions.weather_count": 0, "orders.resting_count": 0}]
    snap = {"positions.weather_count": 0, "orders.resting_count": 0}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 0 and res["degraded"] == []
    assert sent == []


def test_null_still_pages_for_zero_ok_field(monkeypatch, tmp_path):
    # None = the CLI/API call itself failed — that IS degradation even for a
    # zero-ok field, and must still fire at min_streak consecutive nulls.
    hist = [{"positions.weather_count": 5}, {"positions.weather_count": None}]
    snap = {"positions.weather_count": None}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 1
    assert [d["field"] for d in res["degraded"]] == ["positions.weather_count"]
    assert len(sent) == 1 and sent[0][0] == "feed_canary_positions.weather_count"


def test_zero_breaks_null_streak_for_zero_ok_field(monkeypatch, tmp_path):
    # A genuine 0 between nulls is a GOOD reading for a zero-ok field: the null
    # streak restarts, so a single trailing null stays below min_streak.
    hist = [{"positions.market_count": 3},
            {"positions.market_count": None},
            {"positions.market_count": 0}]
    snap = {"positions.market_count": None}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 0 and res["degraded"] == []
    assert sent == []


# ── must-be-nonzero fields: pre-existing 0-trigger behavior unchanged ─────────

def test_must_nonzero_field_zero_still_pages(monkeypatch, tmp_path):
    # balance.available dropping to 0 for 2 cycles after a real baseline is the
    # original 'real today, 0 tomorrow' failure — must keep firing.
    hist = [{"balance.available": 1234}, {"balance.available": 0}]
    snap = {"balance.available": 0}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 1
    assert [d["field"] for d in res["degraded"]] == ["balance.available"]
    assert len(sent) == 1 and sent[0][0] == "feed_canary_balance.available"


def test_never_good_field_stays_quiet(monkeypatch, tmp_path):
    # ever_good gate: a field that has NEVER read non-zero (e.g. a feed degraded
    # since before the baseline) is not a NEW degradation — no page.
    hist = [{"balance.available": 0}, {"balance.available": 0}]
    snap = {"balance.available": 0}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 0 and sent == []


def test_single_transient_bad_reading_stays_quiet(monkeypatch, tmp_path):
    # min_streak=2 means one transient bad cycle (null after a good reading)
    # never fires on its own.
    hist = [{"fair_values.records": 400}]
    snap = {"fair_values.records": None}
    res, sent = _run(monkeypatch, tmp_path, hist, snap)
    assert res["severity"] == 0 and sent == []

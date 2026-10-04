#!/usr/bin/env python3
"""Tests for bin/check_cycle_cadence.py — the cycle-cadence watchdog (D1 2026-07-03).

Pins the properties that make the watchdog safe to run unattended from launchd:
the overnight window (22:00→06:00 PT) generates ZERO expected slots (no false
alarms while the crons are legitimately silent); a slot only becomes due after
its grace period; the LIVE :20 slots exist on the right hours only; log parsing
handles the local-date file naming (a 22:04 PT start lives in that LOCAL date's
file under a NEXT-day UTC timestamp); and the shortfall decision alerts at >=2
missed slots, stays quiet at 1, and never lets one actual start satisfy two
adjacent slots. All log I/O is against a synthetic tmp log dir (module LOG_DIR
patched); KALSHI_WEATHER_ALERTS=0 ambient + trader.notify.alert monkeypatched,
so no test can ever page the operator. No network.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import check_cycle_cadence as cc  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def _pt(y, mo, d, h, mi=0, s=0):
    """LA-local aware datetime (cron slots are defined on this wall clock)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


def _write_log(log_dir: Path, starts_pt: list[datetime]):
    """Write synthetic cycle-<LOCAL-date>.log files exactly the way run-cycle.sh
    does: file named by the LOCAL date at cycle start, marker timestamp in UTC."""
    log_dir.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list[str]] = {}
    for dt in starts_pt:
        fname = f"cycle-{dt.date().isoformat()}.log"
        marker = f"=== {dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z cycle start ==="
        by_file.setdefault(fname, []).extend([marker, "Series: KXHIGHAUS", "--- 1. Build ---"])
    for fname, lines in by_file.items():
        (log_dir / fname).write_text("\n".join(lines) + "\n")


def _eval(monkeypatch, tmp_path, now_pt, starts_pt, **kw):
    """Run evaluate() against a synthetic log dir with alerting captured."""
    monkeypatch.setenv("KALSHI_WEATHER_ALERTS", "0")   # belt: never deliver
    log_dir = tmp_path / "logs"
    _write_log(log_dir, starts_pt)
    monkeypatch.setattr(cc, "LOG_DIR", log_dir)        # braces: never read prod logs
    sent = []
    import trader.notify as notify
    monkeypatch.setattr(notify, "alert",
                        lambda text, key=None, dedup_seconds=3600, **k: sent.append((key, text)))
    res = cc.evaluate(now=now_pt.astimezone(UTC), **kw)
    return res, sent


# ── expected-slot generation ──────────────────────────────────────────────────

def test_overnight_window_has_zero_expected_slots():
    # 03:00 PT, 3h window → 00:00-03:00 PT: no paper (06-22) and no LIVE slots.
    now = _pt(2026, 7, 3, 3, 0)
    slots = cc.expected_slots(now.astimezone(UTC) - timedelta(hours=3),
                              now.astimezone(UTC), timedelta(minutes=20))
    assert slots == []


def test_daytime_slots_paper_hourly_plus_live_20():
    # 15:00 PT, 3h window → paper 12:00/13:00/14:00 + LIVE 13:20, all past grace.
    now = _pt(2026, 7, 2, 15, 0)
    slots = cc.expected_slots(now.astimezone(UTC) - timedelta(hours=3),
                              now.astimezone(UTC), timedelta(minutes=20))
    local = [s.astimezone(TZ).strftime("%H:%M") for s in slots]
    assert local == ["12:00", "13:00", "13:20", "14:00"]


def test_grace_defers_the_newest_slot():
    # At 14:10 PT the 14:00 slot's 20-min grace hasn't elapsed → NOT yet due;
    # the 13:20 LIVE slot (due 13:40) is.
    now = _pt(2026, 7, 2, 14, 10)
    slots = cc.expected_slots(now.astimezone(UTC) - timedelta(hours=3),
                              now.astimezone(UTC), timedelta(minutes=20))
    local = [s.astimezone(TZ).strftime("%H:%M") for s in slots]
    assert "14:00" not in local
    assert local == ["11:20", "12:00", "13:00", "13:20"]


def test_live_slots_only_on_live_hours():
    # Full day sweep: :20 slots exist exactly on 9,11,13,15,17,19 — not 12:20 etc.
    now = _pt(2026, 7, 2, 23, 59)
    slots = cc.expected_slots(now.astimezone(UTC) - timedelta(hours=24),
                              now.astimezone(UTC), timedelta(minutes=20))
    live = sorted({s.astimezone(TZ).hour for s in slots if s.astimezone(TZ).minute == 20})
    assert live == [9, 11, 13, 15, 17, 19]
    paper = sorted({s.astimezone(TZ).hour for s in slots if s.astimezone(TZ).minute == 0})
    assert paper == list(range(6, 23))


# ── actual-start log parsing ──────────────────────────────────────────────────

def test_local_date_file_boundary(tmp_path):
    # A 22:04 PT July-1 start is written to cycle-2026-07-01.log with a UTC
    # timestamp of 2026-07-02T05:04Z. The parser must still find it.
    log_dir = tmp_path / "logs"
    _write_log(log_dir, [_pt(2026, 7, 1, 22, 4, 45)])
    now = _pt(2026, 7, 1, 22, 30).astimezone(UTC)
    starts = cc.actual_starts(now - timedelta(hours=3), now, log_dir=log_dir)
    assert len(starts) == 1
    assert starts[0].strftime("%Y-%m-%dT%H:%M:%S") == "2026-07-02T05:04:45"


# ── shortfall decision ────────────────────────────────────────────────────────

def test_all_slots_started_no_gap(monkeypatch, tmp_path):
    # Starts a few minutes late (within grace) for every due slot → severity 0.
    starts = [_pt(2026, 7, 2, 12, 0, 5), _pt(2026, 7, 2, 13, 0, 7),
              _pt(2026, 7, 2, 13, 20, 10), _pt(2026, 7, 2, 14, 0, 4)]
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), starts)
    assert res["severity"] == 0 and res["shortfall"] == 0
    assert sent == []


def test_silent_no_start_gap_alerts(monkeypatch, tmp_path):
    # The 7/1 failure mode: cron says ok, run-cycle never invoked → empty log.
    # 4 due slots, 0 starts → shortfall 4 >= 2 → severity 1 + one alert.
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), [])
    assert res["severity"] == 1 and res["shortfall"] == 4
    assert len(sent) == 1 and sent[0][0] == "cycle_cadence_gap"


def test_single_miss_stays_below_threshold(monkeypatch, tmp_path):
    # One missed slot (13:20 LIVE) with default min-shortfall 2 → quiet.
    starts = [_pt(2026, 7, 2, 12, 0, 5), _pt(2026, 7, 2, 13, 0, 7),
              _pt(2026, 7, 2, 14, 0, 4)]
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), starts)
    assert res["severity"] == 0 and res["shortfall"] == 1
    assert res["missed_pt"] == ["07-02 13:20 PT"]
    assert sent == []


def test_one_start_cannot_cover_two_slots(monkeypatch, tmp_path):
    # A single start at 13:20 sits in BOTH the 13:00 slot's grace window and the
    # 13:20 slot's — one-to-one matching must count exactly one slot as covered.
    starts = [_pt(2026, 7, 2, 12, 0, 5), _pt(2026, 7, 2, 13, 20, 0),
              _pt(2026, 7, 2, 14, 0, 4)]
    res, _ = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), starts)
    assert res["shortfall"] == 1


def test_overnight_empty_logs_no_alert(monkeypatch, tmp_path):
    # 03:00 PT with NO log files at all: zero expected slots → severity 0. This is
    # the every-30-min launchd steady state overnight — must never page.
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 3, 3, 0), [])
    assert res["severity"] == 0
    assert res["expected"] == [] and sent == []


def test_min_shortfall_override(monkeypatch, tmp_path):
    # min_shortfall=1 (env-overridable knob) makes a single miss alert.
    starts = [_pt(2026, 7, 2, 12, 0, 5), _pt(2026, 7, 2, 13, 0, 7),
              _pt(2026, 7, 2, 14, 0, 4)]
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), starts,
                      min_shortfall=1)
    assert res["severity"] == 1
    assert len(sent) == 1 and sent[0][0] == "cycle_cadence_gap"


# ── lock-stall diagnosis in the alert text (QA 2026-07-03) ────────────────────
# run-cycle.sh's lock-skip path exits BEFORE the tee block, so slots covered by a
# long-stalled cycle (the 7/2 Open-Meteo aggregate stall held the lock for 2.5h)
# count as missed. The alert must then point at the stalled lock holder, not send
# the operator to the OpenClaw gateway while a live cycle is wedged.

def _mk_lock(tmp_path: Path, pid_text: str | None):
    """Create logs/.cycle.lock exactly as run-cycle.sh does (dir + pid file)."""
    lock = tmp_path / "logs" / ".cycle.lock"
    lock.mkdir(parents=True)
    if pid_text is not None:
        (lock / "pid").write_text(pid_text)
    return lock


def test_gap_with_live_lock_blames_stalled_cycle(monkeypatch, tmp_path):
    # Lock held by a LIVE pid (this test process) → diagnosis is the stalled
    # cycle, and the gateway hypothesis must NOT appear.
    _mk_lock(tmp_path, f"{os.getpid()}\n")
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), [])
    assert res["severity"] == 1 and len(sent) == 1
    assert sent[0][0] == "cycle_cadence_gap"
    assert "holding logs/.cycle.lock" in sent[0][1]
    assert "OpenClaw gateway" not in sent[0][1]


def test_gap_with_acquiring_lock_blames_stalled_cycle(monkeypatch, tmp_path):
    # Empty pid file = holder mkdir'd but hasn't written its pid yet. Mirrors
    # run-cycle.sh's don't-steal rule: treat as alive → stall diagnosis.
    _mk_lock(tmp_path, "")
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), [])
    assert res["severity"] == 1 and len(sent) == 1
    assert "holding logs/.cycle.lock" in sent[0][1]


def test_gap_with_stale_lock_blames_gateway(monkeypatch, tmp_path):
    # DEAD lock owner = stale lock run-cycle.sh will reclaim, not a stall →
    # keep the original gateway/model-fallback hypothesis.
    p = subprocess.Popen(["true"])
    p.wait()
    _mk_lock(tmp_path, f"{p.pid}\n")
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), [])
    assert res["severity"] == 1 and len(sent) == 1
    assert "OpenClaw gateway" in sent[0][1]
    assert "holding logs/.cycle.lock" not in sent[0][1]


def test_gap_without_lock_blames_gateway(monkeypatch, tmp_path):
    # No lock at all (the 7/1 silent no-start mode) → gateway hypothesis stands.
    res, sent = _eval(monkeypatch, tmp_path, _pt(2026, 7, 2, 15, 0), [])
    assert res["severity"] == 1 and len(sent) == 1
    assert "OpenClaw gateway" in sent[0][1]
    assert "holding logs/.cycle.lock" not in sent[0][1]

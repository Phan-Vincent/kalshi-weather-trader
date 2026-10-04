#!/usr/bin/env python3
"""Tests for bin/healthcheck.py (trading-outcome health, alert-only).

Monkeypatches the module's paths at a temp dir so checks run against synthetic
signals. Asserts per-check severity (robust to incidental real-state checks).
Plain `python3` runner.
"""
import os
import sys
import time
import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
os.environ["KALSHI_WEATHER_DIR"] = str(_TMP)
os.environ["KALSHI_WEATHER_ALERTS"] = "0"
os.environ["KALSHI_WEATHER_HEALTH_SKIP_IDLE"] = "1"  # don't read the real live arm

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT.parent))

import healthcheck as hc  # noqa: E402


def _wire_tmp():
    """Point the module's paths at a fresh temp dir and return it."""
    d = Path(tempfile.mkdtemp())
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "state" / "live-premium").mkdir(parents=True, exist_ok=True)
    hc.ROOT = d
    hc.STATE = d / "state"
    hc.LOGS = d / "logs"
    hc.HEALTH_STATE = d / "state" / "health-state.json"
    hc.ALERTS_LOG = d / "logs" / "alerts.jsonl"
    return d


def _sev(checks, name):
    for n, s, _ in checks:
        if n == name:
            return s
    return None


def _set_mtime(p: Path, minutes_ago: float):
    t = time.time() - minutes_ago * 60
    os.utime(p, (t, t))


def test_fresh_signals_ok():
    d = _wire_tmp()
    (d / "logs" / "cycle-2026-06-23.log").write_text("=== cycle end ===\n")  # fresh
    (d / "fair-values.json").write_text("{}")  # fresh
    sev, checks, _ = hc.evaluate()
    assert _sev(checks, "cycle_liveness") == 0, checks
    assert _sev(checks, "fair_values_fresh") == 0, checks
    assert _sev(checks, "live_posting") == 0, checks


def test_stale_fair_values_degraded():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    fv = d / "fair-values.json"; fv.write_text("{}"); _set_mtime(fv, 600)  # 10h old
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "fair_values_fresh") == 1, checks


def test_no_cycle_log_critical():
    _wire_tmp()  # logs dir empty
    sev, checks, _ = hc.evaluate()
    assert _sev(checks, "cycle_liveness") == 2, checks
    assert sev == 2


def test_recent_410_critical_old_410_ignored():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": (now - timedelta(hours=1)).isoformat(), "key": "live_order_fail", "text": "HTTP 410"},
    ]
    (d / "logs" / "alerts.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "live_posting") == 2, "recent 410 must be CRITICAL"

    # Now only an OLD failure (5h ago, outside the 4h window) → not flagged.
    d2 = _wire_tmp()
    (d2 / "logs" / "cycle-x.log").write_text("end")
    (d2 / "fair-values.json").write_text("{}")
    old = [{"ts": (now - timedelta(hours=5)).isoformat(), "key": "live_order_fail", "text": "HTTP 410"}]
    (d2 / "logs" / "alerts.jsonl").write_text("\n".join(json.dumps(r) for r in old))
    _, checks2, _ = hc.evaluate()
    assert _sev(checks2, "live_posting") == 0, "old 410 outside window must NOT flag"


def test_never_writes_halt_and_persists_state():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    hc.evaluate()
    assert (d / "state" / "health-state.json").exists(), "health-state must be written"
    assert not (d / "state" / "LIVE_HALT.json").exists(), "healthcheck must NEVER set LIVE_HALT"


def _sup_log(*runs) -> str:
    """Build a supervisor-log body. Each run = (minutes_ago, failed: bool)."""
    now = datetime.now(timezone.utc)
    out = []
    for mins, failed in runs:
        ts = now - timedelta(minutes=mins)
        out.append(f"=== {ts:%Y-%m-%dT%H:%M:%S}Z supervisor start ===")
        out.append("  [refresh-watch] 2 live refresh_cancelled seen; first-firing alerted=True")
        if failed:
            out.append("Failed to authenticate. API Error: 401 Invalid authentication credentials")
        else:
            out.append("Supervisor report — cycle fired, all checks pass.")
        end = ts + timedelta(seconds=8)
        out.append(f"=== {end:%Y-%m-%dT%H:%M:%S}Z supervisor end (rc=0) ===")
    return "\n".join(out) + "\n"


def test_supervisor_401_degraded():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    # 3 recent runs, all 401 (the 2026-07-11 outage shape).
    (d / "logs" / "supervisor-2026-07-11.log").write_text(_sup_log((240, True), (120, True), (10, True)))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") == 1, checks
    detail = next(dt for n, _, dt in checks if n == "supervisor_auth")
    assert "3 run(s)" in detail, detail          # counts the consecutive failing tail
    assert "setup-token" in detail, detail        # carries the remediation pointer


def test_supervisor_recovered_ok():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    # Older runs failed, but the most-recent run is clean → OK (token fix landed).
    (d / "logs" / "supervisor-2026-07-12.log").write_text(_sup_log((240, True), (120, True), (10, False)))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") == 0, checks


def test_supervisor_stale_401_not_flagged():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    # Last run 401'd but 20h ago (> 18h window): supervisor is idle/off, don't ping forever.
    (d / "logs" / "supervisor-2026-07-01.log").write_text(_sup_log((20 * 60, True)))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") is None, "stale historical 401 must not flag"


def test_supervisor_absent_when_no_log():
    d = _wire_tmp()  # no supervisor-*.log at all (fresh deploy)
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") is None, "no supervisor log → nothing to assert"


def test_supervisor_naive_ts_does_not_crash():
    # A start line with a tz-NAIVE but ISO-parseable timestamp (format drift / corruption / a
    # model-echoed line without the trailing Z) must NOT crash the healthcheck — the age math
    # once raised TypeError out of the "exception-safe" parser, discarding checks 1-10. The naive
    # ts is rejected → mtime fallback (file just written, within window) → the run still flags.
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    body = (
        "=== 2026-07-12T05:00:00 supervisor start ===\n"          # naive: no trailing Z
        "  [refresh-watch] ...\n"
        "Failed to authenticate. API Error: 401 Invalid authentication credentials\n"
        "=== 2026-07-12T05:00:08 supervisor end (rc=0) ===\n"
    )
    (d / "logs" / "supervisor-2026-07-12.log").write_text(body)
    sev, checks, _ = hc.evaluate()  # must not raise
    assert _sev(checks, "supervisor_auth") == 1, checks


def test_supervisor_phrase_in_report_body_not_flagged():
    # A CLEAN run (auth OK) whose report body quotes the phrase mid-line must NOT trip the check —
    # the match is anchored to line start, so only the CLI's flush-left error line counts.
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    now = datetime.now(timezone.utc)
    ts = now - timedelta(minutes=10)
    end = ts + timedelta(seconds=8)
    body = (
        f"=== {ts:%Y-%m-%dT%H:%M:%S}Z supervisor start ===\n"
        "  [refresh-watch] ...\n"
        "Supervisor report: live order failed — 'Invalid authentication credentials' per logs.\n"
        f"=== {end:%Y-%m-%dT%H:%M:%S}Z supervisor end (rc=0) ===\n"
    )
    (d / "logs" / "supervisor-2026-07-12.log").write_text(body)
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") == 0, "phrase in report body must not false-flag"


def test_supervisor_fail_ok_fail_tail_counts_one():
    # Tail = fail, ok, fail(most-recent): nfail counts only the consecutive failing tail = 1.
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end")
    (d / "fair-values.json").write_text("{}")
    (d / "logs" / "supervisor-2026-07-12.log").write_text(_sup_log((240, True), (120, False), (10, True)))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "supervisor_auth") == 1, checks
    detail = next(dt for n, _, dt in checks if n == "supervisor_auth")
    assert "1 run(s)" in detail, detail


# ── fair-values overnight-aware freshness (2026-07-13 audit: no daily false DEGRADED) ──

def _cycle_log(d, *starts_min_ago):
    """Write a cycle log with `=== <ts> cycle start ===` markers at the given minutes-ago."""
    now = datetime.now(timezone.utc)
    lines = []
    for m in starts_min_ago:
        ts = now - timedelta(minutes=m)
        lines.append(f"=== {ts:%Y-%m-%dT%H:%M:%S}Z cycle start ===")
        lines.append("Series: KXHIGHNY")
    (d / "logs" / "cycle-2026-07-13.log").write_text("\n".join(lines) + "\n")


def test_fair_values_stale_overnight_gap_not_flagged():
    # First cycle after the ~8h overnight idle: fair-values is 8h old, but the PREVIOUS cycle also
    # ran 8h ago, so the staleness is fully explained by the idle (FV rebuilds this cycle, in step 1).
    # Must NOT page — this was the daily false DEGRADED→OK pair.
    d = _wire_tmp()
    _cycle_log(d, 480, 1)  # previous cycle 8h ago, current cycle just started
    fv = d / "fair-values.json"; fv.write_text("{}"); _set_mtime(fv, 470)  # ~7.8h old, < 8h gap + margin
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "fair_values_fresh") == 0, checks


def test_fair_values_stale_while_cycles_running_flagged():
    # Cycles running at a normal ~1h cadence but fair-values is 5h old → rebuilds are actually failing.
    # Must still page DEGRADED (the check's real purpose; overnight exemption must not mask this).
    d = _wire_tmp()
    _cycle_log(d, 60, 1)  # previous cycle 1h ago
    fv = d / "fair-values.json"; fv.write_text("{}"); _set_mtime(fv, 300)  # 5h old >> 1h gap + margin
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "fair_values_fresh") == 1, checks


# ── OpenClaw cron oversight lane watcher (check 12) ──

def _wire_cron(tmp, jobs):
    """jobs = [(name, enabled, consecutiveErrors)]. Writes jobs.json (LIST shape) + jobs-state.json
    (DICT keyed by id) and points the healthcheck at them via KALSHI_WEATHER_CRON_DIR."""
    cron = tmp / "cron"; cron.mkdir(parents=True, exist_ok=True)
    cfg_jobs, state_jobs = [], {}
    for i, (name, enabled, ce) in enumerate(jobs):
        jid = f"job-{i}"
        cfg_jobs.append({"id": jid, "name": name})
        state_jobs[jid] = {
            "scheduleIdentity": json.dumps({"version": 1, "enabled": enabled}),
            "state": {"consecutiveErrors": ce, "lastStatus": "error" if ce else "ok"},
        }
    (cron / "jobs.json").write_text(json.dumps({"version": 1, "jobs": cfg_jobs}))
    (cron / "jobs-state.json").write_text(json.dumps({"version": 1, "jobs": state_jobs}))
    os.environ["KALSHI_WEATHER_CRON_DIR"] = str(cron)


def test_cron_oversight_lane_down_flagged():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _wire_cron(d, [("Kalshi Weather Nightly Review", True, 6),
                   ("Kalshi Dashboard Live Data", True, 60),
                   ("Some Other Project Job", True, 99)])   # non-kalshi → ignored
    try:
        _, checks, _ = hc.evaluate()
        assert _sev(checks, "cron_oversight") == 1, checks
        detail = next(dt for n, _, dt in checks if n == "cron_oversight")
        assert "2 enabled kalshi cron" in detail, detail    # the 2 kalshi jobs, not the other project
    finally:
        os.environ.pop("KALSHI_WEATHER_CRON_DIR", None)


def test_cron_oversight_ok_and_disabled_ignored():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _wire_cron(d, [("Kalshi Weather Nightly Review", True, 0),      # healthy
                   ("Kalshi Weather Paper ($5K)", False, 38)])       # failing but DISABLED → ignored
    try:
        _, checks, _ = hc.evaluate()
        assert _sev(checks, "cron_oversight") == 0, checks
    finally:
        os.environ.pop("KALSHI_WEATHER_CRON_DIR", None)


# ── restic secondary-backup coverage (check 13) ──

def test_restic_backup_ok():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    rk = d / "state" / "restic-health.json"
    rk.write_text(json.dumps({"status": "ok", "detail": "restic backup ok"}))   # fresh
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "restic_backup") == 0, checks


def test_restic_backup_failed_status_flagged():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    rk = d / "state" / "restic-health.json"
    rk.write_text(json.dumps({"status": "fail", "detail": "restic backup failed — see log"}))
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "restic_backup") == 1, checks


def test_restic_backup_stale_flagged():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    rk = d / "state" / "restic-health.json"; rk.write_text(json.dumps({"status": "ok"}))
    _set_mtime(rk, 40 * 60)   # 40h old > 30h → daily backup stopped writing
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "restic_backup") == 1, checks


def test_restic_backup_absent_not_asserted():
    d = _wire_tmp()  # no restic-health.json (never run) → skipped, no false OK/WARN
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "restic_backup") is None, checks


# ── error-model bias-cap saturation (check 14) ──

def _wire_error_model(d, cities):
    (d / "state" / "error-model.json").write_text(json.dumps({"version": 1, "cities": cities}))


def test_error_model_saturation_flagged():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _wire_error_model(d, {
        "HOU": {"bias_f": -2.31, "n_effective": 41},   # over cap + well-sampled → flag
        "MIA": {"bias_f": -1.86, "n_effective": 45},   # under cap → not flagged
        "XYZ": {"bias_f": -3.5, "n_effective": 4},      # over cap but too few obs → not flagged
    })
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "error_model_saturation") == 1, checks
    detail = next(dt for n, _, dt in checks if n == "error_model_saturation")
    assert "HOU" in detail and "MIA" not in detail and "XYZ" not in detail, detail


def test_error_model_saturation_clean_ok():
    d = _wire_tmp()
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _wire_error_model(d, {"MIA": {"bias_f": -1.86, "n_effective": 45}})   # all under the cap
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "error_model_saturation") == 0, checks


def test_error_model_saturation_absent_skipped():
    d = _wire_tmp()  # no error-model.json → skipped, no false OK/WARN
    (d / "logs" / "cycle-x.log").write_text("end"); (d / "fair-values.json").write_text("{}")
    _, checks, _ = hc.evaluate()
    assert _sev(checks, "error_model_saturation") is None, checks


if __name__ == "__main__":
    test_fresh_signals_ok();                  print("[PASS] fresh signals → OK")
    test_stale_fair_values_degraded();        print("[PASS] stale fair-values → DEGRADED")
    test_no_cycle_log_critical();             print("[PASS] no cycle log → CRITICAL")
    test_recent_410_critical_old_410_ignored();print("[PASS] recent 410 CRITICAL; old 410 ignored")
    test_never_writes_halt_and_persists_state();print("[PASS] never writes LIVE_HALT; persists health-state")
    test_supervisor_401_degraded();           print("[PASS] supervisor 401 → DEGRADED (+ remediation)")
    test_supervisor_recovered_ok();           print("[PASS] supervisor recovered (clean last run) → OK")
    test_supervisor_stale_401_not_flagged();  print("[PASS] stale historical 401 → not flagged")
    test_supervisor_absent_when_no_log();     print("[PASS] no supervisor log → not asserted")
    test_supervisor_naive_ts_does_not_crash();print("[PASS] naive/malformed start-ts → no crash, still flags")
    test_supervisor_phrase_in_report_body_not_flagged();print("[PASS] phrase in report body → not flagged")
    test_supervisor_fail_ok_fail_tail_counts_one();print("[PASS] fail/ok/fail tail → counts 1 run")
    print("\nAll healthcheck tests pass.")

#!/usr/bin/env python3
"""Snapshot-fallback tests for bin/triangulate_balance.py (2026-07-12).

Pins the safety contract of the sandbox fallback: it may only RELAY a fresh Mac-side read —
never fabricate one (missing/stale/torn snapshot ⇒ exit 2), never be written without a live
transport (--write-snapshot without kalshi-cli ⇒ exit 2, no file), and it must preserve the
snapshot's severity as the exit code so DIVERGENCE/ZERO findings survive the relay. Hermetic:
snapshots in tmp dirs via monkeypatched ROOT; transport presence monkeypatched; no network.
"""
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import triangulate_balance as tb  # noqa: E402


def _res(severity=0):
    return {"severity": severity, "state_dir": "live-premium",
            "leg_a_balance": {"available_cents": 57999}, "leg_b_balance": {"available_cents": 57999},
            "leg_c": {"available_cents": 57999, "realized_cents": -981, "open_count": 20},
            "leg_a_positions": {"weather_count": 20, "non_weather_count": 1},
            "leg_b_positions": {"weather_count": 20},
            "findings": ["all legs agree (balance to the cent; weather positions ticker/qty exact)"],
            "note": "Leg D (Kalshi web account) is the only fully-independent ground truth — verify by hand."}


def _fake_root(tmp_path, monkeypatch, state_dir="live-premium"):
    (tmp_path / "state" / state_dir).mkdir(parents=True)
    monkeypatch.setattr(tb, "ROOT", tmp_path)


def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["triangulate_balance.py"] + argv)
    return tb.main()


def test_snapshot_roundtrip_and_age(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    p = tb.write_snapshot(_res(), "live-premium")
    assert p.is_file() and not p.with_suffix(".tmp").exists()      # atomic: no tmp left behind
    snap = tb.read_snapshot("live-premium")
    assert snap["severity"] == 0 and "ts_utc" in snap
    age = tb.snapshot_age_min(snap)
    assert age is not None and 0 <= age < 1


def test_fallback_relays_fresh_snapshot_and_its_severity(tmp_path, monkeypatch, capsys):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    for sev in (0, 1, 2):
        tb.write_snapshot(_res(severity=sev), "live-premium")
        rc = _main(monkeypatch, ["--dir", "live-premium"])
        assert rc == sev                                            # severity survives the relay
    assert "[SNAPSHOT from Mac sync" in capsys.readouterr().out


def test_fallback_missing_snapshot_exits_2(tmp_path, monkeypatch, capsys):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_fallback_stale_snapshot_exits_2(tmp_path, monkeypatch, capsys):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    old = dict(_res(), ts_utc=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3))
               .strftime("%Y-%m-%dT%H:%M:%SZ"))
    tb.snapshot_path("live-premium").write_text(json.dumps(old))
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2
    assert "stale" in capsys.readouterr().out


def test_fallback_torn_snapshot_exits_2(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    tb.snapshot_path("live-premium").write_text('{"severity": 0, "truncat')   # torn write
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2


def test_write_snapshot_refused_without_transport(tmp_path, monkeypatch):
    # a snapshot must be a real Mac-side read — never derived while in fallback conditions
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    assert _main(monkeypatch, ["--dir", "live-premium", "--write-snapshot"]) == 2
    assert tb.read_snapshot("live-premium") is None                 # nothing written


def test_no_snapshot_fallback_forces_live_attempt(tmp_path, monkeypatch):
    # with the flag, we must NOT read the (fresh, severity-0) snapshot — the live attempt runs
    # and, with all legs erroring in this hermetic env, exits 2.
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    tb.write_snapshot(_res(severity=0), "live-premium")
    monkeypatch.setattr(tb, "run", lambda d: dict(_res(severity=2), findings=["Leg A balance: ERROR x"]))
    assert _main(monkeypatch, ["--dir", "live-premium", "--no-snapshot-fallback"]) == 2


# ── 2026-07-12 QA fixes: future-dated rejection + torn-vs-missing message ──────────────────────

def test_fallback_future_dated_snapshot_rejected(tmp_path, monkeypatch, capsys):
    # A snapshot stamped in the reader's future (writer clock ahead / NTP jump) has NEGATIVE age;
    # it must NOT pass the freshness gate as "fresh forever" — reject and exit 2.
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    future = dict(_res(severity=0), ts_utc=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=10))
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
    tb.snapshot_path("live-premium").write_text(json.dumps(future))
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2
    out = capsys.readouterr().out
    assert "UNAVAILABLE" in out and "future-dated" in out
    assert "[SNAPSHOT from Mac sync" not in out    # was NOT relayed as OK


def test_fallback_torn_snapshot_labeled_torn_not_missing(tmp_path, monkeypatch, capsys):
    # A present-but-corrupt file must be reported as unreadable/torn, not "missing" (which would
    # misdirect the operator away from a file that IS there and being produced corrupt).
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    tb.snapshot_path("live-premium").write_text('{"severity": 0, "truncat')   # torn write
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2
    out = capsys.readouterr().out
    assert "unreadable/torn" in out and "snapshot missing" not in out


def test_fallback_truly_missing_still_labeled_missing(tmp_path, monkeypatch, capsys):
    # regression guard for the split: no file at all is still "missing"
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_have_transport", lambda: False)
    assert _main(monkeypatch, ["--dir", "live-premium"]) == 2
    assert "snapshot missing" in capsys.readouterr().out

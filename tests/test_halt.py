#!/usr/bin/env python3
"""Tests for the manual live halt (trader/halt.py). Plain `python3` runner."""
import json
import os
import sys
import tempfile
from pathlib import Path

# Isolate the sentinel + alerts to a temp dir BEFORE importing the module
# (halt._HALT_PATH is computed from KALSHI_WEATHER_DIR at import time).
_TMP = tempfile.mkdtemp()
os.environ["KALSHI_WEATHER_DIR"] = _TMP
os.environ["KALSHI_WEATHER_ALERTS"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader import halt  # noqa: E402

# halt._HALT_PATH is frozen from KALSHI_WEATHER_DIR at *import* time and the module
# is import-cached, so if another test module imported trader.halt first (e.g. under
# a different pytest collection order) _HALT_PATH points at *that* module's tmp, not
# ours. Re-pin it to this test's dir — mirrors halt.py's own
# <KALSHI_WEATHER_DIR>/state/LIVE_HALT.json layout — so these tests are order-independent.
# In production KALSHI_WEATHER_DIR is stable per-process, so this only affects the test.
halt._HALT_PATH = Path(_TMP) / "state" / "LIVE_HALT.json"
# Same re-pin for the clear_halt() episode archive (added after the 2026-07-01 'x'
# incident) so these tests never append to another module's tmp — or the real repo —
# logs/halt-history.jsonl.
halt._HALT_HISTORY = Path(_TMP) / "logs" / "halt-history.jsonl"


def test_default_not_halted():
    halt.clear_halt()
    h, r = halt.is_halted()
    assert h is False and r is None


def test_set_and_clear_roundtrip():
    halt.clear_halt()
    assert halt.set_halt("posting broken")
    h, r = halt.is_halted()
    assert h is True and r == "posting broken"
    assert halt.clear_halt()
    h, _ = halt.is_halted()
    assert h is False


def test_set_is_idempotent_keeps_first_reason():
    halt.clear_halt()
    halt.set_halt("first reason")
    halt.set_halt("second reason")  # should NOT overwrite
    _, r = halt.is_halted()
    assert r == "first reason"
    halt.clear_halt()


def test_sentinel_lives_at_root_state():
    halt.clear_halt()
    halt.set_halt("x")
    assert (Path(_TMP) / "state" / "LIVE_HALT.json").exists()
    halt.clear_halt()


def test_set_halt_payload_has_provenance():
    halt.clear_halt()
    assert halt.set_halt("prov check", source="unit-test")
    data = json.loads(halt._HALT_PATH.read_text())
    # Original fields intact…
    assert data["reason"] == "prov check" and data["source"] == "unit-test"
    assert data["tripped_utc"]
    # …plus provenance (2026-07-01 'x' incident: a bare sentinel was unattributable).
    assert data["pid"] == os.getpid()
    assert data["argv"] == list(sys.argv)
    assert data["hostname"]
    assert data["cwd"]
    assert data["under_pytest"] == ("PYTEST_CURRENT_TEST" in os.environ)
    halt.clear_halt()


def test_clear_halt_archives_episode():
    halt.clear_halt()
    hist = halt._HALT_HISTORY
    n_before = len(hist.read_text().splitlines()) if hist.exists() else 0
    halt.set_halt("archive me", source="unit-test")
    assert halt.clear_halt()
    lines = hist.read_text().splitlines()
    assert len(lines) == n_before + 1, "clear_halt must append exactly one archive line"
    rec = json.loads(lines[-1])
    # Full sentinel content survives the unlink…
    assert rec["reason"] == "archive me" and rec["source"] == "unit-test"
    assert rec["tripped_utc"]
    # …plus who/when cleared it.
    assert rec["cleared_utc"]
    assert rec["cleared_by"]["pid"] == os.getpid()
    assert rec["cleared_by"]["argv"] == list(sys.argv)
    assert rec["cleared_by"]["hostname"]


def test_pytest_guard_refuses_prod_sentinel_writes():
    # Recreate the 2026-07-01 incident shape: _HALT_PATH at the module default (prod)
    # while pytest is running. Point BOTH the live path and the saved prod constant at
    # a scratch "fake prod" file so even a guard regression cannot touch the REAL
    # sentinel from this test.
    fake_prod = Path(_TMP) / "prod-state" / "LIVE_HALT.json"
    orig_path, orig_prod = halt._HALT_PATH, halt._PROD_HALT_PATH
    halt._HALT_PATH = fake_prod
    halt._PROD_HALT_PATH = fake_prod
    try:
        if "PYTEST_CURRENT_TEST" in os.environ:
            assert halt.set_halt("guard probe") is False
            assert not fake_prod.exists(), "guard let a pytest write reach the prod sentinel"
            assert halt.clear_halt() is False
        else:
            # Plain `python3 tests/test_halt.py` runner: not a pytest process, so the
            # guard must NOT interfere with normal operation on the prod path.
            assert halt.set_halt("guard probe") is True
            assert fake_prod.exists()
            assert halt.clear_halt() is True
            assert not fake_prod.exists()
    finally:
        halt._HALT_PATH, halt._PROD_HALT_PATH = orig_path, orig_prod


def test_pytest_guard_allows_monkeypatched_tmp_path():
    # _HALT_PATH (re-pinned to _TMP above) differs from _PROD_HALT_PATH, so the guard
    # must stand down and let tmp-path writes through — even under pytest.
    halt.clear_halt()
    assert halt.set_halt("tmp path ok") is True
    assert halt._HALT_PATH.exists()
    assert halt.clear_halt() is True
    assert not halt._HALT_PATH.exists()


if __name__ == "__main__":
    test_default_not_halted();                 print("[PASS] default not halted")
    test_set_and_clear_roundtrip();            print("[PASS] set/clear round-trip")
    test_set_is_idempotent_keeps_first_reason();print("[PASS] set is idempotent (keeps first reason)")
    test_sentinel_lives_at_root_state();       print("[PASS] sentinel at root state/")
    test_set_halt_payload_has_provenance();    print("[PASS] sentinel payload has provenance")
    test_clear_halt_archives_episode();        print("[PASS] clear archives episode to halt-history")
    test_pytest_guard_refuses_prod_sentinel_writes(); print("[PASS] pytest guard (prod path)")
    test_pytest_guard_allows_monkeypatched_tmp_path(); print("[PASS] pytest guard allows tmp path")
    print("\nAll halt tests pass.")

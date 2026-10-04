#!/usr/bin/env python3
"""
tests/test_alerts_prod_write_guard.py — Regression for the 2026-07-06 audit finding
that trader.notify had no prod-write guard (unlike trader.halt). The conftest fix
monkeypatches notify._ALERTS_LOG in-process only, so a test's __main__ block or the
legacy non-pytest runner could append a synthetic row (e.g. live_order_fail) to the
REAL prod alerts.jsonl, which healthcheck turns into CRITICAL "halt + flatten"
severity by row count — paging the operator over a test artifact.

notify._log_local now refuses to write when, under pytest, _ALERTS_LOG still points
at the real prod path.

Run: python3 -m pytest tests/test_alerts_prod_write_guard.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trader.notify as notify


def test_refuses_write_to_prod_log_under_pytest(monkeypatch):
    # Simulate a test that forgot to redirect: point _ALERTS_LOG back at the prod path.
    monkeypatch.setattr(notify, "_ALERTS_LOG", notify._PROD_ALERTS_LOG)
    assert notify._refuse_pytest_prod_write() is True

    wrote = {"n": 0}
    # If it tried to open the file, this would raise; assert it never gets there.
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote to prod log!")))
    notify._log_local({"key": "live_order_fail", "msg": "synthetic test row"})
    # No exception → _log_local returned early. (wrote stays 0 by construction.)


def test_allows_write_to_redirected_tmp_log(tmp_path, monkeypatch):
    redirected = tmp_path / "alerts.jsonl"
    monkeypatch.setattr(notify, "_ALERTS_LOG", redirected)
    assert notify._refuse_pytest_prod_write() is False
    notify._log_local({"key": "test", "msg": "hello"})
    assert redirected.exists() and "hello" in redirected.read_text()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

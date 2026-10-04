#!/usr/bin/env python3
"""QA-09 (2026-07-01): coverage for the emergency panic button bin/flatten_live.py.

Guards the four behaviors a regression could silently break (all previously untested — a
regression would still pass the whole suite): dry-run default cancels nothing, --execute cancels
every order, a list-failure refuses-safe (rc=2, cancels nothing), and an oid-less order is counted
as failed rather than silently skipped. Monkeypatched CLI/notify; no network, no real orders.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import flatten_live          # noqa: E402
import trader.notify         # noqa: E402


def _setup(monkeypatch, resting, list_err=None, cancel_resp=None):
    calls = {"cancel": []}
    monkeypatch.setattr(flatten_live, "list_resting_orders", lambda prod=True: (resting, list_err))

    def _cancel(oid, prod=True):
        calls["cancel"].append(oid)
        return cancel_resp if cancel_resp is not None else {"order": {"order_id": oid}}

    monkeypatch.setattr(flatten_live, "cancel_order", _cancel)
    monkeypatch.setattr(trader.notify, "alert", lambda *a, **k: True)   # never actually alert
    return calls


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["flatten_live.py", *argv])
    return flatten_live.main()


def test_dry_run_default_cancels_nothing(monkeypatch):
    calls = _setup(monkeypatch, [{"order_id": "A1", "ticker": "KXHIGHNY-26JUL01-T90"}])
    assert _run(monkeypatch, []) == 0            # no --execute
    assert calls["cancel"] == []                 # DRY-RUN must not touch real orders


def test_execute_cancels_all(monkeypatch):
    calls = _setup(monkeypatch, [{"order_id": "A1"}, {"id": "B2"}])
    assert _run(monkeypatch, ["--execute"]) == 0
    assert calls["cancel"] == ["A1", "B2"]       # both oids, incl. the id-fallback


def test_list_failure_returns_2_and_cancels_nothing(monkeypatch):
    calls = _setup(monkeypatch, [], list_err="HTTP 401: unauthorized")
    assert _run(monkeypatch, ["--execute"]) == 2  # refuse-safe: never cancel blindly
    assert calls["cancel"] == []


def test_no_resting_orders_returns_0(monkeypatch):
    calls = _setup(monkeypatch, [])
    assert _run(monkeypatch, ["--execute"]) == 0
    assert calls["cancel"] == []


def test_cancel_error_returns_1(monkeypatch):
    _setup(monkeypatch, [{"order_id": "A1"}], cancel_resp={"_error": True, "status": 500})
    assert _run(monkeypatch, ["--execute"]) == 1  # a failed cancel is surfaced, not swallowed


def test_oidless_order_counted_failed_not_ignored(monkeypatch):
    calls = _setup(monkeypatch, [{"ticker": "KXHIGHNY-26JUL01-T90"}])   # no order_id / id
    assert _run(monkeypatch, ["--execute"]) == 1  # oid-less → failed (leaves order resting; must be loud)
    assert calls["cancel"] == []                   # never attempted (no oid to cancel)

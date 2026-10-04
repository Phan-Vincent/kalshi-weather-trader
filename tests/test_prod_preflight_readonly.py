#!/usr/bin/env python3
"""Safety + behavior tests for bin/prod_orderpath_preflight.py (2026-07-01, Pass C repurpose).

The load-bearing property: the preflight is READ-ONLY — it must have no way to place or cancel a
real order. Plus it should PASS on clean signed reads and FAIL loudly on an auth error. Monkeypatched
reads; no network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import prod_orderpath_preflight as pf   # noqa: E402


def test_no_order_mutating_functions_imported():
    # The module must not import place_limit_order / cancel_order at all — a structural interlock
    # that this diagnostic can never move money.
    assert not hasattr(pf, "place_limit_order"), "preflight must NOT import place_limit_order"
    assert not hasattr(pf, "cancel_order"), "preflight must NOT import cancel_order"
    src = (ROOT / "bin" / "prod_orderpath_preflight.py").read_text()
    # only the SAFETY comment may mention them; no call syntax
    assert "place_limit_order(" not in src and "cancel_order(" not in src


def test_pass_on_clean_reads(monkeypatch):
    monkeypatch.setattr(pf, "list_resting_orders", lambda prod=True: ([], None))
    monkeypatch.setattr(pf, "kalshi_auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(pf, "_http_request", lambda *a, **k: {"balance": 45000})
    assert pf.main() == 0


def test_fail_rc3_on_auth_error(monkeypatch):
    monkeypatch.setattr(pf, "list_resting_orders", lambda prod=True: ([], "HTTP 401: authentication_error"))
    assert pf.main() == 3   # refuse-safe: signed read failed → exit 3, nothing else attempted


def test_mapping_coherence_on_resting_orders(monkeypatch):
    monkeypatch.setattr(pf, "list_resting_orders",
                        lambda prod=True: ([{"side": "yes", "yes_price": 42},
                                            {"side": "no", "no_price": 71}], None))
    monkeypatch.setattr(pf, "kalshi_auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(pf, "_http_request", lambda *a, **k: {"balance": 45000})
    assert pf.main() == 0


def test_balance_failure_is_a_soft_fail_not_a_crash(monkeypatch):
    monkeypatch.setattr(pf, "list_resting_orders", lambda prod=True: ([], None))
    monkeypatch.setattr(pf, "kalshi_auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(pf, "_http_request", lambda *a, **k: {"_error": True, "status": 500})
    assert pf.main() == 1   # resting-read passed, balance failed → non-zero, no exception

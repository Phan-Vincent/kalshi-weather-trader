#!/usr/bin/env python3
"""
tests/test_order_retry_idempotency.py — Regression for the 2026-07-06 audit finding
that _http_request retried ANY exception (incl. a timeout-after-accept) with backoff,
so a non-idempotent order CREATE that timed out after Kalshi accepted it would rest a
SECOND live GTC order on retry. Order-create now passes idempotent=False: a timeout or
5xx returns {_error, ambiguous:True} WITHOUT retrying.

Run: python3 -m pytest tests/test_order_retry_idempotency.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trader.orders as orders


class _Boom(Exception):
    pass


def _count_calls(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(*a, **k):
        calls["n"] += 1
        raise _Boom("simulated timeout")

    monkeypatch.setattr(orders.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_idempotent_request_retries(monkeypatch):
    calls = _count_calls(monkeypatch)
    r = orders._http_request("GET", "https://x/y", retries=3, idempotent=True)
    assert calls["n"] == 3, "idempotent GET should retry up to `retries` times"
    assert r.get("_error") and not r.get("ambiguous")


def test_non_idempotent_create_does_not_retry(monkeypatch):
    calls = _count_calls(monkeypatch)
    r = orders._http_request("POST", "https://x/y", retries=3, idempotent=False, body=b"{}")
    assert calls["n"] == 1, "a non-idempotent create must NOT retry an ambiguous timeout"
    assert r.get("_error") and r.get("ambiguous") is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

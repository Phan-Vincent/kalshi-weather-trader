#!/usr/bin/env python3
"""
tests/test_cancel_path_construction.py — Pin the cancel (DELETE) request construction
(audit 2026-07-06). cancel_order() has never executed in prod; the offline preflight
(bin/cancel_path_preflight.py) validates the construction, and this test guards that the
real cancel_order builds exactly that V2 request (host + /portfolio/events/orders/{id} +
DELETE) so the preflight can't silently drift from what actually gets sent.

Run: python3 -m pytest tests/test_cancel_path_construction.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trader.orders as orders


def test_cancel_order_builds_v2_delete(monkeypatch):
    captured = {}

    def fake_http(method, url, headers=None, body=None, **kw):
        captured.update(method=method, url=url, headers=headers or {})
        return {"ok": True}

    # Don't actually sign/network — just capture what cancel_order constructs.
    monkeypatch.setattr(orders, "_http_request", fake_http)
    monkeypatch.setattr(orders, "kalshi_auth_headers",
                        lambda m, p, prod=False: {"_method": m, "_path": p, "_prod": prod})

    orders.cancel_order("ORD-123", prod=True)

    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders/ORD-123"
    # The SIGNED path must include the /trade-api/v2 prefix and the events path (what the
    # 2026-06-23 V2 migration moved). A regression here silently breaks every risk rail.
    assert captured["headers"]["_path"] == "/trade-api/v2/portfolio/events/orders/ORD-123"
    assert captured["headers"]["_method"] == "DELETE"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

#!/usr/bin/env python3
"""Regression tests for the bounded HTTP body read (data/weather_data._read_body_bounded).

2026-07-10 incident: urlopen(timeout=30) only bounds each socket operation, so an Open-Meteo
response TRICKLING bytes (each recv under the timeout) held resp.read() open 28-88 minutes
inside the fair-value build, starving the 15:20/17:20/19:20 PT live slots behind the cycle
lock — and because the hung process was SIGKILLed by the lock's max-age reclaim before
_http_json returned, the failure was never recorded, so the OM fail-cache/breaker never
opened and every later cycle re-hung.

These tests stand up a real local HTTP server that trickles the body and prove:
  • _http_json fails FAST with _BodyReadTimeout (bounded by the read deadline, not the body);
  • a body-deadline trip is NOT retried (elapsed ~1 deadline, not retries x deadline);
  • a normal (non-trickling) response still parses fine through the bounded path.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.weather_data import _BodyReadTimeout, _http_json  # noqa: E402


class _TrickleHandler(BaseHTTPRequestHandler):
    """Sends headers immediately, then trickles the body one byte per 100ms — each recv
    lands well under the per-op socket timeout, so only a TOTAL deadline can stop the read."""
    BODY = (b'{"hourly": {"time": []}}' + b" " * 4096)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        try:
            for i in range(len(self.BODY)):
                self.wfile.write(self.BODY[i:i + 1])
                self.wfile.flush()
                time.sleep(0.1)          # 4KB body -> ~7 min if unbounded
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                          # client hit its deadline and closed — expected

    def log_message(self, *args):        # keep pytest output clean
        pass


class _NormalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "n": 42}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def _server():
    servers = []

    def _start(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/"

    yield _start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_trickling_body_fails_fast_and_is_not_retried(_server, monkeypatch):
    url = _server(_TrickleHandler)
    monkeypatch.setenv("KALSHI_WEATHER_HTTP_READ_DEADLINE_SEC", "1")
    t0 = time.monotonic()
    with pytest.raises(_BodyReadTimeout):
        _http_json(url, retries=3, timeout=2.0)
    elapsed = time.monotonic() - t0
    # One deadline (~1s) + at most one socket-op block; 3 retries would be >3s of deadlines
    # plus 2^1+2^2 backoff — well over this bound. Proves both "bounded" and "not retried".
    assert elapsed < 6.0, f"body-deadline path took {elapsed:.1f}s — retried or unbounded"


def test_normal_response_unaffected(_server, monkeypatch):
    url = _server(_NormalHandler)
    monkeypatch.setenv("KALSHI_WEATHER_HTTP_READ_DEADLINE_SEC", "60")
    assert _http_json(url, retries=1, timeout=5.0) == {"ok": True, "n": 42}


def test_deadline_zero_disables_bound(_server, monkeypatch):
    """0 must fall back to a plain unbounded read (documented escape hatch) — prove it still
    completes a normal request rather than erroring on the disabled path."""
    url = _server(_NormalHandler)
    monkeypatch.setenv("KALSHI_WEATHER_HTTP_READ_DEADLINE_SEC", "0")
    assert _http_json(url, retries=1, timeout=5.0) == {"ok": True, "n": 42}

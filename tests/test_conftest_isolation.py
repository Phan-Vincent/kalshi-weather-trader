#!/usr/bin/env python3
"""Regression tests for conftest.py's autouse notify/halt PATH isolation.

Before 2026-07-03 every pytest run appended synthetic ``status='disabled'`` alert
records to the PRODUCTION logs/alerts.jsonl (notify._ALERTS_LOG is frozen at import
time; KALSHI_WEATHER_ALERTS=0 stops the send but not the local log — by design).
Hundreds of test lines accumulated, drowning real alert history and camouflaging
the 2026-07-01 'x' halt incident. conftest.py now redirects the notify/halt path
constants per test; these tests pin that behavior so it can't silently rot.

pytest-only ON PURPOSE (no plain-``python3`` __main__ runner): the assertions are
about the conftest autouse fixture, which only exists under pytest. Run standalone
this module must do nothing — in particular it must NEVER call trader.notify.alert
outside pytest, where _ALERTS_LOG really does point at the production log.
"""
import json
from pathlib import Path

_PROD_ROOT = Path.home() / ".openclaw/workspace/automations/kalshi-weather"


def test_alert_from_a_test_never_touches_prod_log():
    import trader.notify as notify

    # The autouse fixture must have pointed both constants away from prod…
    assert notify._ALERTS_LOG != _PROD_ROOT / "logs" / "alerts.jsonl"
    assert notify._DEDUP_STATE != _PROD_ROOT / "logs" / "alert-dedup.json"

    # …and a disabled alert (_isolate_weather_env forces KALSHI_WEATHER_ALERTS=0)
    # must land in the per-test tmp log, not the production one.
    assert notify.alert("conftest isolation probe", key="conftest_isolation_probe") is False
    rec = json.loads(notify._ALERTS_LOG.read_text().splitlines()[-1])
    assert rec["status"] == "disabled" and "conftest isolation probe" in rec["text"]


def test_halt_paths_never_point_at_prod_under_pytest():
    import trader.halt as halt

    # Regardless of collection order, one of two mechanisms applies — test_halt.py's
    # module-level re-pin, or conftest's conditional redirect — and under BOTH the
    # halt constants must not target the real prod sentinel/archive.
    assert halt._HALT_PATH != halt._PROD_HALT_PATH
    assert halt._HALT_HISTORY != _PROD_ROOT / "logs" / "halt-history.jsonl"

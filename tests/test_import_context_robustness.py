#!/usr/bin/env python3
"""Regression: lazy `from data.weather_data import ...` helpers must work under BOTH import contexts.

2026-07-02 QA-all found build_fair_values.py failing EVERY cycle since 2026-07-01 with
`ModuleNotFoundError: No module named 'data'`: it puts only `automations/` on sys.path and imports
`kalshi_weather.model.calibrate`, so a bare `from data.weather_data import ...` inside a helper
(QA-12's _extract_city) couldn't resolve → fair-values silently went stale. This pins that the
extract_city helpers resolve when the module is loaded via the `kalshi_weather.*` symlink path with
ROOT absent from sys.path (the exact build_fair_values context), not just when run as a bin script.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> kalshi-weather/ -> automations/
_AUTOMATIONS = str(Path(__file__).resolve().parent.parent.parent)

_CASES = {
    "calibrate": "from kalshi_weather.model.calibrate import _extract_city as f",
    "brier": "from kalshi_weather.trader.brier import _extract_city as f",
}


@pytest.mark.parametrize("name,imp", list(_CASES.items()))
def test_extract_city_resolves_under_kalshi_weather_import(name, imp):
    # Reproduce build_fair_values' sys.path: automations/ present, repo ROOT absent (no PYTHONPATH).
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = f"import sys; sys.path.insert(0, {_AUTOMATIONS!r}); {imp}; print(f('KXHIGHNY-26JUL01-B80.5'))"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 0, f"{name} raised under kalshi_weather import: {proc.stderr}"
    assert "No module named 'data'" not in proc.stderr, f"{name} still has the bare-import bug"
    assert proc.stdout.strip(), f"{name} extract_city returned nothing — import likely failed"

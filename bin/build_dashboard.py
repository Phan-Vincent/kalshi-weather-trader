#!/usr/bin/env python3
"""Build the kalshi-weather dashboard v4 by generating fresh data files.
v4 template loads data via fetch() from data.json + data-live.json — no HTML injection needed.
"""
import json, os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
DASH.mkdir(parents=True, exist_ok=True)

PAPER_STATE = os.environ.get("KALSHI_WEATHER_STATE_DIR", str(ROOT / "state" / "paper"))

# ── 1. Generate paper data ──
rc = os.system(f'KALSHI_WEATHER_STATE_DIR="{PAPER_STATE}" python3 {ROOT}/bin/generate_dashboard_data.py 2>/dev/null')
if rc != 0:
    print("⚠ generate_dashboard_data.py failed, using stale data.json")

# ── 2. Generate live data ──
rc = os.system(f'python3 {ROOT}/bin/generate_live_data.py 2>/dev/null')
if rc != 0:
    print("⚠ generate_live_data.py failed, using stale data-live.json")

# ── 2b. Generate A/B per-arm data (all paper variant arms) ──
rc = os.system(f'python3 {ROOT}/bin/generate_ab_data.py 2>/dev/null')
if rc != 0:
    print("⚠ generate_ab_data.py failed, using stale ab-data.json")

# ── 3. Load and print summary ──
try:
    with open(DASH / "data.json") as f:
        P = json.load(f)
except Exception:
    P = {}

try:
    with open(DASH / "data-live.json") as f:
        L = json.load(f)
except Exception:
    L = {}

try:
    with open(DASH / "live-history.json") as f:
        hist = json.load(f)
except Exception:
    hist = []

live_total = L.get("weather_total_dollars", L.get("total_balance_dollars", 0))
settlement_count = len(P.get("settlements", []))

size_kb = (DASH / "index.html").stat().st_size / 1024 if (DASH / "index.html").exists() else 0
print(f"v4 Dashboard built: {size_kb:.0f} KB")
print(f"  Paper $5K: ${P.get('cash',0):,.2f} cash, ${P.get('total_pnl',0):+,.2f} PnL, {P.get('win_rate',0):.1f}% WR")
print(f"  Live: ${live_total:,.2f} total, {len(L.get('open_positions',[]))} positions")
print(f"  Brier: {P.get('brier_avg',0):.4f}, BSS: {P.get('bss_all',0):+.4f}, {P.get('brier_settled',0)} settled")
print(f"  Settlements: {settlement_count} trades for v4 charts")
print(f"  Live history: {len(hist)} snapshots")
print(f"  Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

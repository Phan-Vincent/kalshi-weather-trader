#!/usr/bin/env bash
# Nightly performance review for Kalshi Weather Paper Trader
# Runs at 10:30 PM PDT after the last trading cycle
# Writes: ~/.openclaw/workspace/automations/kalshi-weather/reviews/YYYY-MM-DD-review.md
set -euo pipefail

ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
REVIEW_DIR="$ROOT/reviews"
mkdir -p "$REVIEW_DIR"

cd "$ROOT"

# Under cron, bare `python3` resolves to /usr/bin/python3 (3.9), but these scripts use PEP 604
# `X | None` union syntax that requires 3.10+. Pin to Homebrew 3.13, fall back to `python3` for
# environments without it (dev laptops, tests).
PY="/opt/homebrew/bin/python3"
[[ -x "$PY" ]] || PY="python3"

# Refresh the per-city grid→station actual corrections (audit 2026-07-06 #7). Runs once/day
# here (not per-cycle) because it fetches archive actuals; the persistent state/actuals-cache.json
# means only NEW settled events are fetched. Feeds fetch_actual_temp(correction_city=) so the bias
# EWMA / price track Kalshi's settlement station rather than the colder grid cell. Never fatal.
"$PY" bin/learn_actual_corrections.py --live-dir state/paper 2>&1 | tail -20 || true

# Populate the STATION-CORRECTED skill/CRPS actuals cache (crps-actuals-cache-localday-stationcorr.json).
# The per-cycle leakfree_skill runs --no-fetch (read-only), so without this nightly fetching pass the
# corrected cache — freshly versioned 2026-07-06 — would never fill and the skill reports would go dark.
# Incremental after the first run; archive lags ~6-7d so recent events fill in over time. Never fatal.
"$PY" bin/crps_report.py 2>&1 | tail -3 || true

"$PY" bin/nightly-review.py

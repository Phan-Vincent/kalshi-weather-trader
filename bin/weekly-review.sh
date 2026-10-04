#!/usr/bin/env bash
# Weekly performance review for Kalshi Weather Paper Trader
# Writes: ~/.openclaw/workspace/automations/kalshi-weather/reviews/weekly-YYYY-MM-DD.md
# Computes directly from live state files (not from daily review markdowns)
set -euo pipefail

ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
REVIEW_DIR="$ROOT/reviews"
mkdir -p "$REVIEW_DIR"

cd "$ROOT"
python3 bin/weekly-review.py

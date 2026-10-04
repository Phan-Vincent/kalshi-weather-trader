#!/usr/bin/env python3
"""
bin/watch_positions.py — Simple loop that polls portfolio positions and
prints mark-to-market for weather markets every 60s.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# Weather market tickers: KX*HIGHT*, KX*LOWT*, KXTEMP*H
WEATHER_RE = re.compile(r"^KX.*(?:HIGHT|LOWT|TEMP.*H)", re.IGNORECASE)


def get_positions() -> list[dict]:
    """Call kalshi-cli --prod portfolio positions --json."""
    try:
        result = subprocess.run(
            ["kalshi-cli", "--prod", "portfolio", "positions", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"[watch] kalshi-cli error: {result.stderr[:200]}", file=sys.stderr)
            return []
        data = json.loads(result.stdout) if result.stdout else {}
        return data.get("positions", []) or data if isinstance(data, list) else []
    except Exception as e:
        print(f"[watch] exception: {e}", file=sys.stderr)
        return []


def fmt_pnl(position: dict) -> str:
    """Format a weather position with mark-to-market PnL."""
    ticker = position.get("ticker", "?")
    count = int(position.get("count", 0))
    side = position.get("side", "?")
    avg_cost = int(position.get("avg_cost_cents", 0))

    # Approx mark-to-market: for YES positions, current value ~ avg_cost (we don't have live price here)
    # Instead show cost basis and side. True PnL requires market price from a separate call.
    # We show the position basics; the user can mentally mark to market.
    return (
        f"  {ticker:<30} {side.upper():>4} {count:>4} contracts @ {avg_cost:>3}¢ "
        f"(cost ${count * avg_cost / 100:.2f})"
    )


def main() -> None:
    print("[watch_positions] Starting weather position monitor (60s interval)")
    print("[watch_positions] Press Ctrl+C to stop.")
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        positions = get_positions()
        weather = [p for p in positions if WEATHER_RE.match(p.get("ticker", ""))]

        if weather:
            print(f"\n[{ts}] Weather positions ({len(weather)}):")
            for p in weather:
                print(fmt_pnl(p))
        else:
            print(f"[{ts}] No weather positions.")

        try:
            time.sleep(60)
        except KeyboardInterrupt:
            print("\n[watch_positions] Stopped.")
            break


if __name__ == "__main__":
    main()

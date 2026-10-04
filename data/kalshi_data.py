#!/usr/bin/env python3
"""
data/kalshi_data.py — Kalshi market data layer using real prod orderbooks.

The legacy kalshi-cli `markets list` and `markets get` return deprecated
yes_bid/yes_ask fields that are ALWAYS 0 for the new orderbook_fp markets.
This module fetches the real orderbook from the public REST endpoint and
derives top-of-book prices and sizes.

Public endpoint (no auth required):
  GET https://api.elections.kalshi.com/trade-api/v2/markets/<ticker>/orderbook
"""
from __future__ import annotations

import concurrent.futures
import json
import ssl
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

import certifi

PROD_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Reusable SSL context using certifi CA bundle + opener for connection pooling
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CTX)
)


def fetch_orderbook(ticker: str, timeout: int = 10, retries: int = 2) -> Optional[Dict[str, Any]]:
    """Fetch raw orderbook for a single ticker. Returns None on failure."""
    url = f"{PROD_API_BASE}/markets/{ticker}/orderbook"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method="GET")
            with _OPENER.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def derive_book(orderbook_fp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Derive top-of-book from orderbook_fp structure.

    yes_dollars  = list of [price_dollars_str, qty_str] for YES bids
    no_dollars   = list of [price_dollars_str, qty_str] for NO bids

    Best YES bid  = max(price for p,q in yes_dollars)
    Best NO bid   = max(price for p,q in no_dollars)
    Best YES ask  = 1.00 - best_no_bid   (someone buying NO at $0.98 = selling YES at $0.02)
    Best NO ask   = 1.00 - best_yes_bid
    """
    yes_levels = orderbook_fp.get("yes_dollars") or []
    no_levels = orderbook_fp.get("no_dollars") or []

    best_yes_bid_cents = max((round(float(p) * 100) for p, q in yes_levels), default=0)
    best_no_bid_cents = max((round(float(p) * 100) for p, q in no_levels), default=0)

    best_yes_ask_cents = 100 - best_no_bid_cents if best_no_bid_cents else 0
    best_no_ask_cents = 100 - best_yes_bid_cents if best_yes_bid_cents else 0

    yes_bid_qty = sum(float(q) for p, q in yes_levels if round(float(p) * 100) == best_yes_bid_cents)
    no_bid_qty = sum(float(q) for p, q in no_levels if round(float(p) * 100) == best_no_bid_cents)

    spread_cents = best_yes_ask_cents - best_yes_bid_cents if best_yes_ask_cents else 0

    return {
        "yes_bid": best_yes_bid_cents,
        "yes_ask": best_yes_ask_cents,
        "no_bid": best_no_bid_cents,
        "no_ask": best_no_ask_cents,
        "yes_bid_qty": round(yes_bid_qty, 2),
        "no_bid_qty": round(no_bid_qty, 2),
        "yes_ask_qty": round(no_bid_qty, 2),   # derived: YES ask depth = NO bid depth at complementary price
        "no_ask_qty": round(yes_bid_qty, 2),    # derived: NO ask depth = YES bid depth at complementary price
        "spread_cents": spread_cents,
        "raw_orderbook": orderbook_fp,
    }


def yes_mark_from_book(yes_bid, yes_ask):
    """YES-side mark price (cents) from already-derived top-of-book bid/ask, or None if unusable.

    Single source of truth for one-sided-book valuation (2026-07-13). derive_book() encodes a
    MISSING side as the sentinel 0 (real Kalshi quotes are 1-99), so a one-sided book must NOT be
    averaged as (bid+ask)/2 — that fabricates a price at real_side/2 and SIGN-FLIPS a near-settled
    position's P&L (a NO holder in a market bid YES 99c is ~worthless, not worth ~50c). Mark to the
    honest liquidation value instead:

      • two-sided book  -> standard mid (yes_bid + yes_ask) / 2
      • only YES bids   -> yes_bid   (market near-resolved YES; that bid is the real floor)
      • only NO bids     -> yes_ask   (= 100 - no_bid; market near-resolved NO)
      • empty/invalid    -> None

    Callers side-normalize: cur = mark if side == "yes" else 100.0 - mark.
    """
    if not (isinstance(yes_bid, (int, float)) and isinstance(yes_ask, (int, float))):
        return None
    if yes_bid and yes_ask:
        return (yes_bid + yes_ask) / 2.0
    if yes_bid:
        return float(yes_bid)
    if yes_ask:
        return float(yes_ask)
    return None


def enrich_market_with_orderbook(market: Dict[str, Any]) -> Dict[str, Any]:
    """
    Given a raw market dict (from kalshi-cli), fetch its orderbook and
    overwrite the deprecated yes_bid/yes_ask/no_bid/no_ask fields with
    real derived values.
    """
    ticker = market.get("ticker", "")
    if not ticker:
        return market

    ob = fetch_orderbook(ticker)
    if not ob or not ob.get("orderbook_fp"):
        # Orderbook unavailable — return original market unchanged
        return market

    book = derive_book(ob["orderbook_fp"])
    enriched = {**market}
    enriched["yes_bid"] = book["yes_bid"]
    enriched["yes_ask"] = book["yes_ask"]
    enriched["no_bid"] = book["no_bid"]
    enriched["no_ask"] = book["no_ask"]
    enriched["yes_bid_qty"] = book["yes_bid_qty"]
    enriched["no_bid_qty"] = book["no_bid_qty"]
    enriched["spread_cents"] = book["spread_cents"]
    enriched["orderbook_source"] = "orderbook_fp"
    return enriched


def enrich_markets(
    markets: List[Dict[str, Any]],
    sleep_secs: float = 0.05,
    progress_every: int = 25,
    max_workers: int = 12,
) -> List[Dict[str, Any]]:
    """
    Enrich a list of market dicts with real orderbook data.

    Concurrent over a bounded thread pool reusing the module-global pooled _OPENER
    (each fetch is an independent stateless GET, so this is thread-safe). Output order
    matches the input. Was serial with a 50ms sleep each (~70s for ~230 markets); the
    pool bounds API pressure instead. `sleep_secs` is retained for backward compat but
    no longer used (fetch_orderbook already retries/backs off on its own).
    """
    n = len(markets)
    if n == 0:
        return []
    out: List[Optional[Dict[str, Any]]] = [None] * n
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        futs = {ex.submit(enrich_market_with_orderbook, m): i for i, m in enumerate(markets)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:
                out[i] = markets[i]  # fall back to the unenriched market on error
            done += 1
            if progress_every and done % progress_every == 0:
                print(f"[kalshi_data] enriched {done}/{n} markets", file=sys.stderr)
    return out  # type: ignore[return-value]


def quick_orderbook_check(ticker: str) -> Optional[Dict[str, Any]]:
    """One-liner: fetch + derive for a single ticker. Used for debugging."""
    ob = fetch_orderbook(ticker)
    if not ob:
        return None
    return derive_book(ob.get("orderbook_fp", {}))

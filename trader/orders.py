#!/usr/bin/env python3
"""
trader/orders.py — Order placement, fee math, maker quote logic.
Uses urllib.request (stdlib) since requests is not installed.
All prices in cents, all money in cents internally.
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

# NOTE: PyJWT (`import jwt`) is imported lazily inside get_jwt_token() because it
# is ONLY needed for the LIVE prod order path. Paper trading (scanner/paper_trade)
# imports this module too and must not hard-require PyJWT just to compute edges.


# ── API URLs ────────────────────────────────────────────────────────────
DEMO_API_BASE = "https://demo-api.kalshi.co/trade-api/v2"
# V2 host split (validated live 2026-06-24): api.elections serves READS only
# (markets/orderbook/balance/positions). ALL order ops moved to the external-api host —
# create POST + cancel DELETE on /portfolio/events/orders, list GET on /portfolio/orders —
# the old api.elections POST /portfolio/orders now 410s "deprecated_v1_order_endpoint".
PROD_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# V2 order-creation host (single-book bid/ask, fixed-point dollar prices).
ORDER_API_BASE_PROD = "https://external-api.kalshi.com/trade-api/v2"
ORDER_API_BASE_DEMO = DEMO_API_BASE

# Auth key paths
_KALSHI_CONFIG = Path.home() / ".kalshi/config.yaml"
_KALSHI_PROD_CONFIG = Path.home() / ".kalshi/config-prod.yaml"
# Fallback keys used only if the config file omits private_key_path. The prod
# default must point at a file that actually exists (the old prod-key-newest.pem
# default did not, which silently broke any path that didn't read the config).
_DEFAULT_PROD_KEY = Path.home() / ".kalshi/prod-key-final.pem"
_DEFAULT_DEMO_KEY = Path.home() / ".kalshi/demo-key-fixed.pem"


def _load_key_id(prod: bool = False) -> str:
    """Read api_key_id. Prod reads config-prod.yaml, demo reads config.yaml."""
    cfg = _KALSHI_PROD_CONFIG if prod else _KALSHI_CONFIG
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml and cfg.exists():
        with open(cfg) as f:
            data = yaml.safe_load(f)
        return str(data.get("api_key_id", ""))
    if cfg.exists():
        with open(cfg) as f:
            for line in f:
                if line.strip().startswith("api_key_id:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _load_private_key(prod: bool = False) -> str:
    """Load the signing private key. Reads private_key_path from the matching
    config (config-prod.yaml for prod, config.yaml for demo); falls back to a
    default key that actually exists if the config omits the path."""
    cfg = _KALSHI_PROD_CONFIG if prod else _KALSHI_CONFIG
    key_path: Optional[Path] = None
    if cfg.exists():
        try:
            import yaml
            with open(cfg) as f:
                data = yaml.safe_load(f) or {}
            kp = data.get("private_key_path")
            if kp:
                key_path = Path(kp)
        except ImportError:
            with open(cfg) as f:
                for line in f:
                    if "private_key_path:" in line:
                        kp = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if kp:
                            key_path = Path(kp)
                            break
    if key_path is None:
        key_path = _DEFAULT_PROD_KEY if prod else _DEFAULT_DEMO_KEY
    with open(key_path) as f:
        return f.read()


# ── Kalshi request signing (RSA-PSS) ───────────────────────────────────
# Kalshi authenticates writes by signing `timestamp_ms + METHOD + path` with the
# account RSA private key (PSS / SHA256), base64-encoded — NOT JWT. (The old JWT
# path was rejected by Kalshi, which is why orders had fallen back to kalshi-cli.)
_key_cache: dict = {}


def _signing_key(prod: bool):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    ck = "prod" if prod else "demo"
    if ck not in _key_cache:
        _key_cache[ck] = load_pem_private_key(_load_private_key(prod=prod).encode(), password=None)
    return _key_cache[ck]


def kalshi_auth_headers(method: str, path: str, prod: bool = False) -> dict:
    """Signed headers for a Kalshi REST call. `path` is the full request path
    including the /trade-api/v2 prefix, without query string."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _signing_key(prod).sign(
        (ts + method.upper() + path).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _load_key_id(prod=prod),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── HTTP client ────────────────────────────────────────────────────────

def _http_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
    timeout: int = 10,
    retries: int = 3,
    idempotent: bool = True,
) -> dict:
    """Make an HTTP request with exponential backoff retries.

    idempotent=False (order CREATE): a timeout or 5xx is AMBIGUOUS — the request may
    have reached Kalshi and rested a GTC order before the response was lost. Blindly
    retrying (old behavior) would rest a SECOND order (audit 2026-07-06). So for a
    non-idempotent call we do NOT retry those; we return {_error, ambiguous:True} and
    let the caller reconcile via the next position sync. 429 (rejected before the book
    is touched) is still safe to retry either way."""
    req_headers = headers or {}
    for attempt in range(retries):
        try:
            ctx = ssl.create_default_context()
            # macOS bundled Python may need the certifi CA bundle
            try:
                import certifi
                ctx.load_verify_locations(certifi.where())
            except ImportError:
                pass
            req = urllib.request.Request(url, method=method, headers=req_headers, data=body)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
            if e.code == 429:
                # Rate limited — retry honoring Retry-After (don't treat as a
                # permanent client error; previously 429 fell into the 4xx branch
                # below and the order silently failed with no backoff).
                if attempt == retries - 1:
                    return {"_error": True, "status": 429, "body": body_text}
                retry_after = None
                try:
                    retry_after = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                except Exception:
                    retry_after = None
                try:
                    wait = float(retry_after) if retry_after else (2 ** attempt)
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                time.sleep(min(wait, 30))
                continue
            if 400 <= e.code < 500:
                # Client error — don't retry
                return {"_error": True, "status": e.code, "body": body_text}
            # 5xx: ambiguous for a non-idempotent create (the order may have rested) →
            # do NOT retry; surface the ambiguity so the caller reconciles.
            if not idempotent:
                return {"_error": True, "status": e.code, "body": body_text, "ambiguous": True}
            if attempt == retries - 1:
                return {"_error": True, "status": e.code, "body": body_text}
            time.sleep(2 ** attempt)
        except Exception as e:
            # Network error / timeout. For a non-idempotent create this is the classic
            # timeout-after-accept: the order may be resting. Never auto-retry it.
            if not idempotent:
                return {"_error": True, "exception": str(e), "ambiguous": True}
            if attempt == retries - 1:
                return {"_error": True, "exception": str(e)}
            time.sleep(2 ** attempt)
    return {"_error": True, "exception": "all retries exhausted"}


# ── Fee math ───────────────────────────────────────────────────────────

def _kalshi_taker_fee_cents(qty: int, price_cents: int) -> int:
    """
    Total taker fee in cents for `qty` contracts at `price_cents`.

    Kalshi's per-contract taker fee is: ceil(0.07 * P * (1-P)) where P = price/100.
    At 50¢: ceil(0.07 * 0.5 * 0.5) = ceil(0.0175) = 2¢ per contract.
    At 20¢: ceil(0.07 * 0.2 * 0.8) = ceil(0.0112) = 2¢ per contract.
    At 95¢: ceil(0.07 * 0.95 * 0.05) = ceil(0.003325) = 1¢ per contract.

    This is NOT a flat 7¢ fee. The formula ceiling-clamps to protect exchange
    from rounding losses at thin prices. Do NOT "fix" this to flat 7¢ or you
    will undercount fees by 5-6¢ per contract on typical 20-50¢ markets.

    Exact integer formula: ceil(7 * qty * price_cents * (100 - price_cents) / 10000)
    """
    if qty <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    numerator = 7 * qty * price_cents * (100 - price_cents)
    return (numerator + 9999) // 10000  # ceiling via integer math


def _kalshi_maker_rebate_cents(qty: int, price_cents: int) -> int:
    """
    Total maker rebate in cents (negative fee = you receive money).

    Kalshi's per-contract maker rebate is: ceil(0.00175 * P * (1-P)) * 100? —
    Actually the formula is an integer approximation: ceil(175 * qty * P * (1-P) / 1000000).
    At 50¢: ceil(175 * 1 * 50 * 50 / 1000000) = ceil(0.4375) = 1¢ rebate per contract.
    At 20¢: ceil(175 * 1 * 20 * 80 / 1000000) = ceil(0.28) = 1¢ rebate per contract.

    CAUTION: The per-contract rebate passed to the caller is THIS TOTAL divided by qty,
    so it may round to 0.0 for small qty/price combos (e.g. 1 contract @ 50¢ returns 1¢/1 = 1.0¢).
    Do NOT change this to a flat rate — it is intentionally proportional to price.

    Exact integer formula: ceil(175 * qty * price_cents * (100 - price_cents) / 1000000)
    """
    if qty <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    numerator = 175 * qty * price_cents * (100 - price_cents)
    return (numerator + 999_999) // 1_000_000  # ceiling via integer math


def fee_per_contract_cents(qty: int, price_cents: int, maker: bool = False) -> float:
    """Fee (positive) or rebate (negative) per contract in cents."""
    if maker:
        return -(_kalshi_maker_rebate_cents(qty, price_cents) / qty)
    return _kalshi_taker_fee_cents(qty, price_cents) / qty


# ── Fractional-Kelly sizing ─────────────────────────────

def kelly_qty(
    fair_prob_for_side: float,
    price_cents: int,
    bankroll_cents: int,
    fraction: float = 0.25,
    per_event_cap_cents: int = 500,
    per_trade_cap_cents: int = 5000,
    max_bankroll_pct: float = 0.05,
) -> int:
    """
    Quarter-Kelly contract count for a binary contract.

    f* = (p - m) / (1 - m)   where m = price/100 and p is fair prob for the side we're buying.
    Stake = bankroll * fraction * f*, capped by:
      - per-event max ($5 default)
      - per-trade max ($50 default, from PDF)
      - max_bankroll_pct (5% default)
    Returns integer contract count (≥1 if any positive Kelly, else 0).
    """
    if price_cents <= 0 or price_cents >= 100 or bankroll_cents <= 0:
        return 0
    m = price_cents / 100.0
    p = max(0.0, min(1.0, fair_prob_for_side))
    if p <= m:
        return 0
    f_star = (p - m) / (1.0 - m)
    stake_cents = bankroll_cents * fraction * f_star
    stake_cents = min(
        stake_cents,
        per_event_cap_cents,
        per_trade_cap_cents,
        bankroll_cents * max_bankroll_pct,
    )
    qty = int(stake_cents // price_cents)
    return max(0, qty)


# ── Post-fee edge computation ─────────────────────────────────────────

def compute_post_fee_edge(fair_prob_yes: float, yes_ask_cents: int, no_ask_cents: int, qty: int) -> dict:
    """
    Compare YES vs NO taker edges net of fees.
    Returns the BETTER side, edge in cents/contract, expected profit per contract.
    All prices in cents (0-100).

    FIX 2026-06-11 (audit-trade-lifecycle Finding 2.1): validate inputs.
    Zero or negative ask prices would silently produce ~50¢ of imaginary edge.
    Caller should treat the result as "no trade" when status != "ok".
    """
    if yes_ask_cents <= 0 or no_ask_cents <= 0 or qty <= 0:
        return {
            "status": "invalid_input",
            "side": "none",
            "limit_price_cents": 0,
            "edge_cents_per_contract": -999.0,
            "est_profit_per_contract_cents": -999.0,
            "fee_per_contract_cents": 0.0,
            "raw_edge_cents": 0.0,
        }
    fair_yes_cents = max(1, min(99, round(fair_prob_yes * 100)))

    # YES side: buy YES at yes_ask, fair value = fair_yes_cents
    yes_fee = fee_per_contract_cents(qty, yes_ask_cents, maker=False)
    yes_edge = fair_yes_cents - yes_ask_cents - yes_fee

    # NO side: buy NO at no_ask, fair value = 100 - fair_yes_cents
    no_fee = fee_per_contract_cents(qty, no_ask_cents, maker=False)
    no_edge = (100 - fair_yes_cents) - no_ask_cents - no_fee

    if yes_edge >= no_edge:
        return {
            "side": "yes",
            "limit_price_cents": yes_ask_cents,
            "edge_cents_per_contract": round(yes_edge, 2),
            "est_profit_per_contract_cents": round(yes_edge, 2),
            "fee_per_contract_cents": round(yes_fee, 2),
            "raw_edge_cents": round(fair_yes_cents - yes_ask_cents, 2),
        }
    else:
        return {
            "side": "no",
            "limit_price_cents": no_ask_cents,
            "edge_cents_per_contract": round(no_edge, 2),
            "est_profit_per_contract_cents": round(no_edge, 2),
            "fee_per_contract_cents": round(no_fee, 2),
            "raw_edge_cents": round((100 - fair_yes_cents) - no_ask_cents, 2),
        }


# ── Maker quote ────────────────────────────────────────────────────────

def compute_maker_quote(fair_prob_yes: float, current_yb: int, current_ya: int) -> Tuple[int, int]:
    """
    For market-making mode: post one tick inside the spread on the side
    with positive edge after maker rebate.
    Returns (yes_quote_cents, no_quote_cents).
    Only one side may be quotable; the other returns -1.
    """
    fair_yes_cents = max(1, min(99, round(fair_prob_yes * 100)))
    spread = current_ya - current_yb
    if spread <= 0:
        return -1, -1

    yes_quote = -1
    no_quote = -1

    # Try to bid YES (buy YES) one tick above current best YES bid
    if current_yb + 1 < current_ya:
        proposed_yes_bid = current_yb + 1
        # Edge if we get filled at proposed_yes_bid: fair - proposed - maker_rebate
        rebate = fee_per_contract_cents(qty=1, price_cents=proposed_yes_bid, maker=True)
        edge = fair_yes_cents - proposed_yes_bid + rebate  # rebate is negative, so +rebate = -|rebate|
        if edge > 0:
            yes_quote = proposed_yes_bid

    # Try to bid NO (buy NO) one tick above current best NO bid
    # NO bid = 100 - current_ya (since YES ask = NO bid in a tight book)
    current_no_bid = 100 - current_ya
    current_no_ask = 100 - current_yb
    if current_no_bid + 1 < current_no_ask:
        proposed_no_bid = current_no_bid + 1
        rebate = fee_per_contract_cents(qty=1, price_cents=proposed_no_bid, maker=True)
        edge = (100 - fair_yes_cents) - proposed_no_bid + rebate
        if edge > 0:
            no_quote = proposed_no_bid

    return yes_quote, no_quote


# ── Order placement ───────────────────────────────────────────────────

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    error: Optional[str]
    dry_run: bool
    ticker: str
    side: str
    price_cents: int
    qty: int


def _v2_order_body(market_ticker: str, side: str, price_cents: int, qty: int) -> dict:
    """Build the Kalshi V2 create-order body (POST /portfolio/events/orders).

    The bot speaks (side='yes'|'no', price_cents = the price for THAT side). Kalshi's
    single-book V2 quotes everything from the YES leg:
      buy YES at Pc  -> side='bid', price = Pc/100         (the YES price)
      buy NO  at Pc  -> side='ask', price = (100-Pc)/100   (sell YES at 100-Pc; selling
                                                            YES at X == buying NO at 1-X)
    `price` and `count` are fixed-point dollar strings. DO NOT change this mapping
    without re-verifying against Kalshi's create-order-v2 docs — a wrong side or price
    loses real money.
    """
    import uuid
    if side == "yes":
        v2_side, yes_price_cents = "bid", int(price_cents)
    else:
        v2_side, yes_price_cents = "ask", 100 - int(price_cents)
    return {
        "ticker": market_ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": v2_side,
        "count": f"{int(qty)}.00",
        "price": f"{yes_price_cents / 100:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }


def place_limit_order(
    market_ticker: str,
    side: str,
    price_cents: int,
    qty: int,
    prod: bool = False,
    dry_run: bool = True,
) -> OrderResult:
    """
    Place a limit order. If dry_run, prints structured JSON to stderr and returns a
    synthetic order id. If live, POSTs to Kalshi's /portfolio/orders with RSA-PSS
    request-signing (kalshi_auth_headers).
    """
    if dry_run:
        synthetic_id = f"dry-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:-3]}"
        record = {
            "dry_run": True,
            "ticker": market_ticker,
            "side": side,
            "price_cents": price_cents,
            "qty": qty,
            "order_id": synthetic_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(record), file=sys.stderr)
        return OrderResult(
            success=True,
            order_id=synthetic_id,
            error=None,
            dry_run=True,
            ticker=market_ticker,
            side=side,
            price_cents=price_cents,
            qty=qty,
        )

    # ── LIVE path: RSA-PSS-signed V2 order create ──
    # Kalshi deprecated POST /portfolio/orders (410 deprecated_v1_order_endpoint, 2026-06-23).
    # V2 create-order: external-api host + /portfolio/events/orders, single-book bid/ask,
    # fixed-point dollar prices. Mapping built/tested in _v2_order_body.
    base = ORDER_API_BASE_PROD if prod else ORDER_API_BASE_DEMO
    endpoint = "/portfolio/events/orders"
    path = "/trade-api/v2" + endpoint  # path component to sign
    body = _v2_order_body(market_ticker, side, price_cents, qty)
    try:
        headers = kalshi_auth_headers("POST", path, prod=prod)
        # idempotent=False: on a timeout/5xx the order MIGHT have rested — do not auto-retry
        # (would double-post); reconcile via the next sync instead.
        resp = _http_request("POST", base + endpoint, headers=headers,
                             body=json.dumps(body).encode(), idempotent=False)
    except Exception as e:
        return OrderResult(False, None, f"sign/send error: {e}", False, market_ticker, side, price_cents, qty)
    if resp.get("_error"):
        detail = str(resp.get("body") or resp.get("exception") or "")[:300]
        if resp.get("ambiguous") and prod:
            # The create may have rested despite the error. Page loudly so the next sync
            # reconciles rather than us silently assuming FAILED while an order rests.
            try:
                from trader.notify import alert
                alert(f"⚠️ AMBIGUOUS live order {market_ticker} {side} {qty}@{price_cents}¢ — "
                      f"timeout/5xx after send ({detail[:120]}); order MAY be resting, sync will reconcile",
                      key="live_order_ambiguous")
            except Exception:
                pass
        if prod:
            try:
                from trader.notify import alert
                alert(f"LIVE order FAILED {market_ticker} {side} {qty}@{price_cents}¢ — "
                      f"HTTP {resp.get('status')}: {detail[:160]}", key="live_order_fail")
            except Exception as e:
                print(f"[orders] live_order_fail alert dispatch failed: {e}", file=sys.stderr)
        return OrderResult(False, None, f"HTTP {resp.get('status')}: {detail}", False, market_ticker, side, price_cents, qty)
    order = resp.get("order", resp) if isinstance(resp, dict) else {}
    oid = order.get("order_id") or order.get("id")
    if oid:
        return OrderResult(True, str(oid), None, False, market_ticker, side, price_cents, qty)
    # A 200 with no order_id is NOT a success — treat as failure so the caller never logs a
    # phantom (uncancelable) order with id=None (#2, 2026-06-24 review).
    return OrderResult(False, None, "no_order_id_in_response", False, market_ticker, side, price_cents, qty)


def cancel_order(order_id: str, prod: bool = False) -> dict:
    """Cancel (reduce to 0) a resting order — signed DELETE.
    V2: external-api host + /portfolio/events/orders/{id} (moved from /portfolio/orders/{id}, 2026-06-23)."""
    base = ORDER_API_BASE_PROD if prod else ORDER_API_BASE_DEMO
    endpoint = f"/portfolio/events/orders/{order_id}"
    path = "/trade-api/v2" + endpoint
    return _http_request("DELETE", base + endpoint, headers=kalshi_auth_headers("DELETE", path, prod=prod))


def list_resting_orders(prod: bool = False) -> tuple[list, Optional[str]]:
    """List resting (open, not-yet-filled) orders — signed GET /portfolio/orders?status=resting.
    V2: external-api host; the LIST path stays /portfolio/orders (only create/cancel moved to
    /portfolio/events/orders).

    Returns (orders, error): error is None on success, else a string describing the API
    failure — so a caller (bin/flatten_live.py) can tell "no resting orders" apart from "the
    list call failed". A panic button that silently cancels nothing because list returned []
    on a 401/404 is worse than a loud one. Single page only: the bot holds at most a few dozen
    resting orders, well under Kalshi's page size; add cursor pagination if that changes."""
    base = ORDER_API_BASE_PROD if prod else ORDER_API_BASE_DEMO
    endpoint = "/portfolio/orders?status=resting"
    sign_path = "/trade-api/v2/portfolio/orders"  # sign the path WITHOUT the query string
    resp = _http_request("GET", base + endpoint, headers=kalshi_auth_headers("GET", sign_path, prod=prod))
    if not isinstance(resp, dict) or resp.get("_error"):
        detail = ""
        if isinstance(resp, dict):
            detail = f"HTTP {resp.get('status')}: {str(resp.get('body') or resp.get('exception') or '')[:160]}"
        return [], (detail or "list_resting_orders failed")
    return (resp.get("orders") or resp.get("resting_orders") or []), None

#!/usr/bin/env python3
"""bin/paper_trade.py — Paper-trade Kalshi weather markets against LIVE prod prices.

Why not use kalshi-cli demo? Demo Kalshi has zero volume on weather markets,
so paper fills there are meaningless. This script:
  1. Reads live prod orderbooks via the public REST API (read-only, no auth).
  2. Routes scanner signals through the PaperBook for simulated fills at
     the LIVE prod ask (taker) or queues maker quotes against the real book.
  3. Sweeps pending maker orders on each run to see if any got filled.
  4. NEVER writes to prod. No order create, no portfolio mutation.

Usage:
  python3 bin/paper_trade.py --fair-values fair-values.json --mode taker
  python3 bin/paper_trade.py --fair-values fair-values.json --mode mm
  python3 bin/paper_trade.py --report                      # just print book state
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))  # for `kalshi-weather` namespace

from kalshi_weather.data.kalshi_data import fetch_orderbook, derive_book, yes_mark_from_book
from trader.scanner import scan
from trader.risk import RiskGate
from trader.paper_book import PaperBook
from trader.brier import BrierLogger
from trader.orders import place_limit_order, OrderResult


KALSHI_CLI = os.environ.get("KALSHI_CLI", "kalshi-cli")


def _market_mid_prob(bid_cents, ask_cents, fallback_cents):
    """Market-implied probability for the side traded, from the MID — NOT our execution price —
    so Brier/BSS scores against the real market baseline, not the favorable fill we got
    (audit #4, 2026-06-25). Falls back to the execution price when a side of the book is missing."""
    if isinstance(bid_cents, (int, float)) and isinstance(ask_cents, (int, float)) and ask_cents > 0:
        return ((bid_cents + ask_cents) / 2.0) / 100.0
    return (fallback_cents or 0) / 100.0


def _stderr(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", file=sys.stderr)


def _pinned_live_bankroll(default_cents: int) -> int:
    """Use allocated weather capital instead of account-wide available cash.

    Unrelated settlements can inflate risk limits if account cash is used.
    A valid environment pin supplies allocated capital; otherwise use the default.
    Paper arms never call this helper.
    """
    raw = os.environ.get("KALSHI_WEATHER_LIVE_BANKROLL_CENTS", "").strip()
    if not raw:
        return default_cents
    try:
        pinned = int(raw)
    except ValueError:
        _stderr(f"LIVE bankroll pin ignored (not an int): {raw!r}")
        return default_cents
    if pinned <= 0:
        _stderr(f"LIVE bankroll pin ignored (non-positive): {pinned}")
        return default_cents
    _stderr(f"LIVE bankroll pinned: {pinned}c (account cash {default_cents}c ignored for risk %)")
    return pinned


# ── P0a: cross-process common-random-numbers (CRN) book cache ────────────────────
# When KALSHI_WEATHER_SHARED_BOOK=1 + KALSHI_WEATHER_CYCLE_ID is set, every PAPER arm of a cycle
# reads the SAME per-cycle order-book snapshot per ticker, so the paired A/B difference cancels the
# cross-arm "each arm fetched a slightly different snapshot seconds apart" noise (run_variants runs
# arms as separate sequential subprocesses). The LIVE arm runs with SHARED_BOOK=0 (run_variants
# forces it) and ALWAYS fetches fresh — real orders must never price off a cached/stale book.
# Cache is per-cycle (cycle_id in the filename) so it is never reused across cycles; freshness is the
# filename, not a wall-clock TTL. Read-through in fetch_live_market (the per-ticker primitive both paths use).
_SHARED_CACHE = None            # in-proc memo of the per-cycle cache (each arm is one process)
_SHARED_CACHE_PATH = None
_SHARED_PRUNED = False


def _shared_book_path():
    if os.environ.get("KALSHI_WEATHER_SHARED_BOOK") != "1":
        return None
    cid = os.environ.get("KALSHI_WEATHER_CYCLE_ID", "")
    if not cid:
        return None
    return ROOT / "state" / "_shared" / f"book-cache-{cid}.json"


def _shared_cache(path):
    global _SHARED_CACHE, _SHARED_CACHE_PATH
    if _SHARED_CACHE is not None and _SHARED_CACHE_PATH == path:
        return _SHARED_CACHE
    c = {}
    # Trust the per-cycle filename (cycle_id = timestamp+pid, unique per run) for freshness — NOT a
    # wall-clock TTL. A TTL only fired MID-cycle on a slow (>TTL) cycle, discarding earlier arms'
    # snapshots and silently reducing the CRN variance-reduction benefit; it never guarded correctness
    # (a cycle can't outlive its own id, and prune bounds cross-cycle files). (QA fix 2026-07-01)
    try:
        if path.exists():
            c = json.loads(path.read_text())
    except Exception:
        c = {}
    _SHARED_CACHE, _SHARED_CACHE_PATH = c, path
    return c


def _shared_cache_put(path, ticker, m):
    global _SHARED_PRUNED
    c = _shared_cache(path)
    c[ticker] = m
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        os.replace(tmp, path)
        if not _SHARED_PRUNED:   # once per process: keep the dir bounded across cycles
            _SHARED_PRUNED = True
            olds = sorted(path.parent.glob("book-cache-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in olds[20:]:
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def fetch_live_market(ticker: str) -> dict:
    """Read-only prod fetch via public orderbook endpoint (no auth). Read-through the per-cycle CRN
    book cache for PAPER arms (see the P0a block above); LIVE arms bypass it (SHARED_BOOK=0)."""
    path = _shared_book_path()
    if path is not None:
        c = _shared_cache(path)
        if ticker in c:
            return c[ticker]
    ob = fetch_orderbook(ticker)
    if not ob or not ob.get("orderbook_fp"):
        _stderr(f"fetch_live_market failed for {ticker}: no orderbook data")
        return {}                    # transient failure: do NOT cache the empty
    book = derive_book(ob["orderbook_fp"])
    # Return a market-shaped dict so downstream code works unchanged
    m = {
        "ticker": ticker,
        "yes_bid": book["yes_bid"],
        "yes_ask": book["yes_ask"],
        "no_bid": book["no_bid"],
        "no_ask": book["no_ask"],
        "yes_bid_qty": book["yes_bid_qty"],
        "no_bid_qty": book["no_bid_qty"],
        "yes_ask_qty": book["yes_ask_qty"],
        "no_ask_qty": book["no_ask_qty"],
        "spread_cents": book["spread_cents"],
        "orderbook_source": "orderbook_fp",
    }
    if path is not None:
        _shared_cache_put(path, ticker, m)
    return m


def fetch_live_markets_for_tickers(tickers: list[str]) -> dict:
    """Best-effort: one orderbook call per ticker."""
    out = {}
    for t in tickers:
        m = fetch_live_market(t)
        if m and m.get("ticker"):
            out[m["ticker"]] = m
    return out


def _live_ask(market: dict, side: str) -> int:
    """Return live ask in cents for the requested side. 0 means no liquidity."""
    if side == "yes":
        return int(market.get("yes_ask", 0) or 0)
    return int(market.get("no_ask", 0) or 0)


def _maker_would_cross(side: str, price_cents: int, market: dict) -> bool:
    """True if a maker bid on `side` at price_cents would cross the current book and fill
    as a TAKER — i.e. the bid is at or above the best ask on that side (audit 2026-07-06).
    A YES/NO bid resting >= the {side}_ask crosses. 0 ask = no liquidity, cannot cross."""
    side_ask = _live_ask(market, side)
    return side_ask > 0 and price_cents >= side_ask


def _extract_city_from_ticker(ticker: str) -> str:
    """City code from a Kalshi weather ticker. Handles BOTH the T-suffixed series (KXHIGHT/KXLOWT/
    KXTEMP) AND the bare daily-high/low series (KXHIGH<city>/KXLOW<city>, e.g. KXHIGHNY) — the bare
    series were previously missed, silently skipping their orders in the halt-cancel + refresh (QA
    2026-06-25). Regex mirrors settle_paper._extract_city_from_ticker; un-aliased (returns 'NY' for
    KXHIGHNY) so it matches how the risk gate keys the city."""
    import re as _re
    m = _re.match(r"^KX(?:HIGHT|LOWT|HIGH|LOW|TEMP)([A-Z]+)-", ticker)
    if not m:
        return ""
    city = m.group(1)
    _MONTH_CODES = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
    if city in _MONTH_CODES or not (2 <= len(city) <= 5):
        return ""
    return city


def _cancel_halted_city_makers(book: PaperBook, risk: RiskGate) -> int:
    """Cancel/reduce pending maker quotes in cities with reduced risk allocation.

    Full halt (multiplier=0.0): cancel entirely.
    Partial halt (multiplier<1.0): reduce qty proportionally.
    """
    kept = []
    cancelled = 0
    reduced = 0
    for pm in book.pending_makers:
        city = _extract_city_from_ticker(pm.get("ticker", ""))
        mult, reason = risk.get_city_risk_multiplier(city)
        if mult == 0.0:
            cancelled += 1
            _stderr(f"Cancelled pending maker {pm.get('ticker')} {pm.get('side', '').upper()}: {reason}")
            continue
        elif mult < 1.0:
            orig_qty = pm.get("qty", 0)
            new_qty = max(1, int(round(orig_qty * mult)))
            if new_qty < orig_qty:
                reduced += 1
                pm["qty"] = new_qty
                _stderr(f"Reduced pending maker {pm.get('ticker')} qty {orig_qty}→{new_qty}: {reason} x{mult:.2f}")
        kept.append(pm)
    if cancelled or reduced:
        book.pending_makers = kept
        book._save()
    return cancelled


def _cancel_halted_city_live_orders(book: PaperBook, risk: RiskGate, prod: bool = True) -> int:
    """Cancel LIVE Kalshi resting orders in cities whose circuit breaker is fully open (mult==0.0).

    _cancel_halted_city_makers only drops PAPER pending_makers; a halted city's LIVE resting orders
    were otherwise auto-cancelled by NOTHING (only the manual bin/flatten_live.py), so a city the
    breaker had flagged as losing kept filling adverse orders until a human intervened. This closes
    that kill-chain gap each live cycle (QA halt-guard gap, 2026-06-25; user-authorized default-on).
    Fail-closed on a list error (cancel nothing + alert). The city is extracted exactly as the risk
    gate keys it (no alias) so the halt lookup matches the breaker's own keying."""
    from trader.orders import list_resting_orders, cancel_order
    resting, err = list_resting_orders(prod=prod)
    if err:
        _stderr(f"  [halt-cancel] list_resting_orders failed ({err}) — fail-closed, cancel nothing")
        try:
            from trader.notify import alert
            alert(f"halt-cancel: list_resting_orders failed: {err}", key="halt_cancel_list_failed")
        except Exception:
            pass
        return 0
    cancelled = 0
    for o in resting or []:
        tk = o.get("ticker") or o.get("market_ticker") or ""
        city = _extract_city_from_ticker(tk)
        if not city:
            continue
        mult, reason = risk.get_city_risk_multiplier(city)
        if mult != 0.0:
            continue                                     # not halted → leave resting
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        cres = cancel_order(oid, prod=prod)
        if isinstance(cres, dict) and not cres.get("_error"):
            cancelled += 1
            book._log_lifecycle("halt_cancelled_live", {"ticker": tk, "city": city, "order_id": oid,
                "remaining_count": int(o.get("remaining_count") or o.get("count") or 0)})
            _stderr(f"  [halt-cancel] LIVE {tk} cancelled (city {city} halted: {reason})")
        else:
            _stderr(f"  [halt-cancel] cancel FAILED for {tk} ({oid}) — order left resting")
    return cancelled


# ── quote-staleness refresh (Stream 3, band-keyed 2026-06-25) ───────────────────────────────────
# Resting premium quotes are good-till-cancelled and were never refreshed when the BOOK moves → a
# quote left above the new market gets adversely filled. The premium edge is structural/book-derived
# (NOT the model — bin/tail_edge.py shows the forecast is ANTI-predictive for it), so staleness is
# keyed on the order book via scanner.premium_quote_price (the SAME pricing the scanner posts).
# CANCEL-ONLY: the normal placement loop re-posts fresh quotes where the strategy still wants them —
# so no requote double-exposure / fill-rate denominator inflation / slippage mis-attribution. Flag-
# gated (KALSHI_WEATHER_QUOTE_REFRESH, default off → no behavior change); fail-closed; skips halts.

def _refresh_cfg():
    return (int(os.environ.get("KALSHI_WEATHER_REFRESH_STALE_CENTS", "2")),
            int(os.environ.get("KALSHI_WEATHER_REFRESH_MAX_PER_CYCLE", "8")))


def _band_stale(side: str, resting_px: int, market: dict):
    """Is a resting premium quote at resting_px on `side` STALE vs the CURRENT book? Returns
    (stale, fresh_px). A one-sided / degenerate snapshot (a side momentarily empty in a thin book) is
    NOT treated as stale — leave the quote resting rather than cancel on bad data. Otherwise stale
    when the (two-sided) book is no longer premium-quotable, the strategy would now quote the OTHER
    side, or our resting price is >= STALE cents richer than a fresh quote (the book moved against us
    → adverse-fill risk). Keyed on the book (premium_quote_price), not the model."""
    from trader.scanner import premium_quote_price, _extract_yes_asks
    stale_c, _ = _refresh_cfg()
    yes_bid, yes_ask = _extract_yes_asks(market)
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask <= yes_bid:
        return False, None                                # degenerate/one-sided snapshot → don't act
    pq = premium_quote_price(market)
    if pq is None:
        return True, None                                 # two-sided but no longer premium-quotable → stale
    fresh_side, fresh_px = pq
    if fresh_side != side:
        return True, fresh_px                             # strategy wants the other side now → stale
    if int(resting_px or 0) - fresh_px >= stale_c:
        return True, fresh_px                             # our quote is now too rich vs the book → stale
    return False, fresh_px


def _resting_price(o: dict, side: str, basis: dict) -> int:
    """Resting limit price (cents) for a Kalshi order — read the SIDE's price field (yes_price for a
    yes order, no_price for a no order, as bin/flatten_live.py does), else fall back to the posted_live
    basis we logged at placement. Deliberately does NOT read a bare 'price' (the YES-leg in fixed-point
    dollars — wrong units + wrong side) or 'limit_price_cents' (our own log field, never on a Kalshi
    API order object). (QA 2026-06-25)"""
    v = o.get("yes_price") if side == "yes" else o.get("no_price")
    if v:
        return int(v)
    b = basis.get(o.get("order_id") or o.get("id"))
    return int(b.get("limit_price_cents") or 0) if b else 0


def _resting_exposure(orders: list, basis: dict) -> list:
    """Map the arm's OWN resting live maker orders → check_event_limit items ({ticker, count,
    avg_cost_cents}) so the per-event $ cap counts UNFILLED exposure too (QA-01 residual,
    2026-07-09): a $4 resting maker on strike A was invisible when a strike-B signal for the
    same event arrived next cycle. Price via _resting_price (side-aware, posted_live-basis
    fallback — never the bare 'price' field); qty = remaining_count so the filled part, already
    counted from book.open, is not double-counted. BUY makers only (a sell reduces exposure).
    Rows with no usable price/qty are dropped — same-ticker dedup still blocks those.
    Deliberately NOT filtered to weather tickers: non-weather resting orders (sports etc. on the
    shared account) map to non-weather _event_keys and sit inert in the cap sum, whereas a
    weather-classifier false-negative would silently EXCLUDE real exposure (the unsafe direction)."""
    items = []
    for o in orders or []:
        tk = o.get("ticker") or o.get("market_ticker") or ""
        side = (o.get("side") or "").lower()
        if not tk or side not in ("yes", "no"):
            continue
        if (o.get("action") or "").lower() == "sell":
            continue
        qty = int(o.get("remaining_count") or o.get("count") or 0)
        px = _resting_price(o, side, basis)
        if qty <= 0 or px <= 0:
            continue
        items.append({"ticker": tk, "count": qty, "avg_cost_cents": px})
    return items


def _read_lifecycle(book: PaperBook) -> list:
    p = book._lifecycle_path()
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _refresh_stale_live_quotes(book: PaperBook, risk: RiskGate, prod: bool = True) -> int:
    """Cancel LIVE resting premium quotes the order book has moved against (band/book-keyed,
    CANCEL-ONLY — the placement loop re-posts fresh quotes where the strategy still wants them).
    Fail-closed on a list error; skips halted cities (halt-cancel owns those)."""
    if os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH", "0") != "1":
        return 0
    from trader.orders import list_resting_orders, cancel_order
    resting, err = list_resting_orders(prod=prod)
    if err:
        _stderr(f"  [refresh] list_resting_orders failed ({err}) — fail-closed, no refresh")
        try:
            from trader.notify import alert
            alert(f"quote-refresh: list_resting_orders failed: {err}", key="refresh_list_failed")
        except Exception:
            pass
        return 0
    if not resting:
        return 0
    _, MAX = _refresh_cfg()
    tickers = sorted({(o.get("ticker") or o.get("market_ticker") or "") for o in resting} - {""})
    snap = fetch_live_markets_for_tickers(tickers)
    basis = {r.get("paper_order_id"): r for r in _read_lifecycle(book)
             if r.get("event") == "posted_live" and r.get("paper_order_id")}
    cancelled = 0
    for o in resting:
        if cancelled >= MAX:
            break
        tk = o.get("ticker") or o.get("market_ticker") or ""
        side = (o.get("side") or "").lower()
        if not tk or side not in ("yes", "no"):
            continue
        if (o.get("action") or "").lower() == "sell":     # only refresh our maker BUYs, never exits
            continue
        mult, _r = risk.get_city_risk_multiplier(_extract_city_from_ticker(tk))
        if mult == 0.0:                                   # halted city → halt-cancel owns it
            continue
        mkt = snap.get(tk)
        if mkt is None:                                   # no book this cycle → leave resting (safe)
            continue
        resting_px = _resting_price(o, side, basis)
        if resting_px <= 0:                               # can't determine our price → leave (safe)
            continue
        stale, fresh_px = _band_stale(side, resting_px, mkt)
        if not stale:
            continue
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        cres = cancel_order(oid, prod=prod)
        if not (isinstance(cres, dict) and not cres.get("_error")):
            _stderr(f"  [refresh] cancel FAILED for {tk} {side.upper()} ({oid}) — left resting")
            continue
        cancelled += 1
        book._log_lifecycle("refresh_cancelled", {"ticker": tk, "side": side,
            "limit_price_cents": resting_px, "fresh_price_cents": fresh_px, "order_id": oid,
            "remaining_count": int(o.get("remaining_count") or o.get("count") or 0)})
        _stderr(f"  [refresh] LIVE {tk} {side.upper()} {resting_px}c cancelled (stale vs book; fresh={fresh_px})")
    return cancelled


def _refresh_stale_paper_makers(book: PaperBook) -> int:
    """PAPER A/B sibling: drop pending makers the book has moved against (same band/book-keyed,
    CANCEL-ONLY logic as live; they rest in book.pending_makers, swept locally — not on Kalshi)."""
    if os.environ.get("KALSHI_WEATHER_QUOTE_REFRESH", "0") != "1" or not book.pending_makers:
        return 0
    _, MAX = _refresh_cfg()
    tickers = sorted({pm.get("ticker") for pm in book.pending_makers if pm.get("ticker")})
    snap = fetch_live_markets_for_tickers(tickers)
    kept, cancelled = [], 0
    for pm in book.pending_makers:
        tk, side = pm.get("ticker"), (pm.get("side") or "").lower()
        mkt = snap.get(tk) if tk else None
        if cancelled >= MAX or mkt is None or side not in ("yes", "no"):
            kept.append(pm)
            continue
        stale, _fresh = _band_stale(side, int(pm.get("limit_price_cents") or 0), mkt)
        if stale:
            cancelled += 1
            book._log_lifecycle("refresh_cancelled", pm)
            continue                                      # drop the stale quote
        kept.append(pm)
    if cancelled:
        book.pending_makers = kept
        book._save()
    return cancelled


def _format_summary(book: PaperBook, snap: dict) -> str:
    s = book.summary(snap)
    return (
        f"Bank ${s['starting_bank_dollars']:.2f} | "
        f"Cash ${s['cash_dollars']:.2f} | "
        f"Equity ${s['equity_dollars']:.2f} | "
        f"NetPnL ${s['net_pnl_dollars']:+.2f} | "
        f"Realized ${s['realized_pnl_dollars']:+.2f} | "
        f"Open {s['open_positions']} | Pending {s['pending_makers']} | Closed {s['trades_closed']}"
    )


def cmd_report() -> int:
    book = PaperBook()
    # Refresh open positions with live mids for accurate equity
    tickers = list({p["ticker"] for p in book.open + book.pending_makers})
    snap = fetch_live_markets_for_tickers(tickers) if tickers else {}

    # 2026-06-20: persist an open-positions snapshot (current mid + unrealized P&L +
    # settlement time) for the dashboard Positions tab. Reuses the mids fetched above
    # — no extra network. Best-effort.
    try:
        import json as _json
        from pathlib import Path as _Path
        state_dir = _Path(os.environ.get("KALSHI_WEATHER_STATE_DIR",
                                         _Path(__file__).resolve().parent.parent / "state"))
        out = []
        for p in book.open:
            live = snap.get(p["ticker"], {})
            yb, ya = live.get("yes_bid"), live.get("yes_ask")
            entry = p.get("avg_entry_cents", 0)
            qty = p.get("qty", 0)
            side = p.get("side", "yes")
            cur = unreal = None
            yes_mark = yes_mark_from_book(yb, ya)
            if yes_mark is not None:
                cur = yes_mark if side == "yes" else 100.0 - yes_mark
                unreal = round((cur - entry) * qty / 100.0, 2)
            out.append({
                "ticker": p["ticker"], "side": side, "qty": qty,
                "entry_cents": entry, "cur_cents": round(cur, 1) if cur is not None else None,
                "unreal_pnl": unreal, "settle": p.get("market_close_time"),
                "fair_prob": p.get("fair_prob_at_open"),
            })
        _json.dump(out, open(state_dir / "positions-live.json", "w"), indent=2)

        # Markout snapshot (2026-06-21): append the post-fill mid path for each open
        # position so bin/premium_fill_report.py can measure adverse selection —
        # whether the market drifts against us right after we get filled. Reuses the
        # live mids already fetched above; one row per open position per report call.
        now = datetime.now(timezone.utc)
        with open(state_dir / "markout-log.jsonl", "a") as _mf:
            for p in book.open:
                live = snap.get(p["ticker"], {})
                yb, ya = live.get("yes_bid"), live.get("yes_ask")
                yes_mark = yes_mark_from_book(yb, ya)
                if yes_mark is None:
                    continue
                side = p.get("side", "yes")
                cur = yes_mark if side == "yes" else 100.0 - yes_mark
                try:
                    age_h = round((now - datetime.fromisoformat(
                        p["opened_utc"].replace("Z", "+00:00"))).total_seconds() / 3600.0, 2)
                except Exception:
                    age_h = None
                _mf.write(_json.dumps({
                    "ts": now.isoformat(), "ticker": p["ticker"], "side": side,
                    "qty": p.get("qty", 0), "fill_px": p.get("avg_entry_cents", 0),
                    "opened_utc": p.get("opened_utc"), "cur_mid": round(cur, 1),
                    "age_hours": age_h, "paper_order_id": p.get("paper_order_id"),
                }) + "\n")
    except Exception as e:
        _stderr(f"cmd_report snapshot/markout write failed (non-fatal): {e}")

    print(_format_summary(book, snap))
    if book.open:
        print("\n📂 OPEN POSITIONS:")
        for p in book.open:
            live = snap.get(p["ticker"], {})
            yb = live.get("yes_bid", "?")
            ya = live.get("yes_ask", "?")
            # Paper-only fields (fair_prob_at_open, market_close_time) are absent on positions
            # synced from Kalshi — render defensively so the live report can't crash (2026-06-24).
            fair = p.get('fair_prob_at_open')
            fair_str = f"{fair:.2f}" if isinstance(fair, (int, float)) else "n/a"
            close = (p.get('market_close_time') or '')[:16] or "n/a"
            print(f"  {p.get('ticker','?'):32s} {p.get('side','?').upper():3s} qty={p.get('qty',0):>3d} entry={p.get('avg_entry_cents','?')}¢ "
                  f"yb/ya={yb}/{ya} fair={fair_str} close={close}")
    if book.pending_makers:
        print("\n⏳ PENDING MAKER QUOTES:")
        for p in book.pending_makers:
            live = snap.get(p.get("ticker", ""), {})
            yb = live.get("yes_bid", "?")
            nb = live.get("no_bid", "?")
            fair = p.get("fair_prob")
            fair_str = f"{fair:.2f}" if isinstance(fair, (int, float)) else "?"
            print(f"  {p.get('ticker','?'):32s} {p.get('side','?').upper():3s} qty={p.get('qty',0):>3d} limit={p.get('limit_price_cents','?')}¢ "
                  f"live yb/nb={yb}/{nb} fair={fair_str}")
    if book.closed[-10:]:
        print("\n✅ LAST 10 CLOSED:")
        for t in book.closed[-10:]:
            pnl = t.get('pnl_cents', 0) or 0
            print(f"  {t.get('ticker','?'):32s} {t.get('side','?').upper():3s} qty={t.get('qty',0):>3d} "
                  f"{t.get('entry_cents','?')}¢→{t.get('exit_cents','?')}¢ pnl=${pnl/100:+.2f} ({t.get('settlement_result','?')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade Kalshi weather markets vs LIVE prod prices.")
    parser.add_argument("--fair-values", default=None, help="Path to fair-values.json")
    parser.add_argument("--mode", choices=["taker", "mm"], default="taker")
    parser.add_argument("--max-orders", type=int, default=3)
    parser.add_argument("--report", action="store_true", help="Print book state and exit")
    parser.add_argument("--no-sweep", action="store_true", help="Skip pending-maker sweep")
    parser.add_argument("--live", action="store_true", help="Place REAL orders (not paper). Requires Kalshi prod API access.")
    parser.add_argument("--state-dir", default=None, help="Override state directory (default: kalshi-weather/state)")
    args = parser.parse_args()

    # P0a SAFETY (QA 2026-07-01): the CRN book cache must NEVER serve a live order path. Enforce it
    # HERE at the process level — not only via run_variants' per-arm env — so ANY --live entrypoint
    # (run_variants live arm, the legacy run-cycle.sh block, a manual `paper_trade.py --live`) fetches
    # FRESH regardless of an inherited KALSHI_WEATHER_SHARED_BOOK/CYCLE_ID in the shell env. Set before
    # the first fetch (the live sweep at ~:594) so _shared_book_path() returns None for the whole process.
    if args.live:
        os.environ["KALSHI_WEATHER_SHARED_BOOK"] = "0"

    if args.report:
        return cmd_report()

    if not args.fair_values:
        _stderr("--fair-values required (or pass --report)")
        return 2

    state_dir = Path(args.state_dir) if args.state_dir else None
    book = PaperBook(state_path=(state_dir / "paper-book.json") if state_dir else None)
    risk = RiskGate(per_run_max_orders=args.max_orders, _state_dir=state_dir)
    brier = BrierLogger()

    # Live kill-switch (defense-in-depth; run_variants is the primary gate). When the
    # operator has set LIVE_HALT, the live arm does nothing — no sweep, no new orders.
    if args.live:
        try:
            from trader.halt import is_halted_safe
            halted, hreason = is_halted_safe()
        except Exception as e:
            # Fail SAFE: a broken halt check must NOT enable live trading.
            halted, hreason = True, f"halt check unavailable: {e}"
        if halted:
            _stderr(f"🛑 LIVE HALTED ({hreason}) — placing no live orders. Resume: bin/halt_live.py --off")
            print(json.dumps({"status": "live_halted", "reason": hreason}, indent=2))
            return 0

    # 0. Cancel stale maker quotes in cities whose circuit breaker is now open.
    # Otherwise old quotes can fill after the scanner has stopped producing new signals.
    _cancel_halted_city_makers(book, risk)

    # 0a0. Live arm: also cancel HALTED-city resting orders on Kalshi — _cancel_halted_city_makers
    # only drops paper makers, so a tripped city's LIVE orders were auto-cancelled by nothing
    # (kill-chain gap, QA 2026-06-25). Default-on safety control; fail-closed on a list error.
    if args.live:
        try:
            _n_halt = _cancel_halted_city_live_orders(book, risk, prod=True)
            if _n_halt:
                _stderr(f"LIVE halt-cancel: {_n_halt} resting order(s) in halted cities cancelled")
        except Exception as _e:
            _stderr(f"live halt-cancel skipped (non-fatal): {_e}")

    # 0a. Quote-staleness refresh (paper A/B sibling): reprice/drop pending makers on model fair-value
    # drift BEFORE the sweep, so a re-priced quote is swept at its new price. Flag-gated (default off).
    if not args.live and args.fair_values:
        try:
            _n_ref = _refresh_stale_paper_makers(book)
            if _n_ref:
                _stderr(f"Paper quote refresh: {_n_ref} book-stale maker(s) dropped")
        except Exception as _e:
            _stderr(f"paper quote refresh skipped (non-fatal): {_e}")

    # 0b. Research-mode Brier gate check (read-only, never mutates Brier files).
    brier_summary = brier.summary()
    ok, reason = risk.check_brier_gate(brier_summary)
    if not ok:
        _stderr(f"🚫 Brier gate BLOCKED new entries: {reason}")
        _stderr(f"   Research mode config: {risk.get_summary().get('research_mode')}")
        _stderr("   Set KALSHI_RESEARCH_MODE_ALLOW_RED=1 to override and continue data collection.")
        print(json.dumps({
            "status": "brier_gate_blocked",
            "reason": reason,
            "research_mode": risk.get_summary().get("research_mode"),
            "brier_summary": {k: v for k, v in brier_summary.items() if k not in ("calibration_buckets", "worst_bucket")},
            "book_summary": book.summary(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        return 0

    _stderr(f"Research mode active: min_price={risk.min_price_cents}¢  brier_gate=OK  city_cb={'DISABLED' if risk.research_mode.disable_city_circuit_breaker else 'ENFORCED'}")

    # 1. Sweep pending maker quotes against live prices first
    if not args.no_sweep and book.pending_makers:
        tickers = list({p["ticker"] for p in book.pending_makers})
        live_snap = fetch_live_markets_for_tickers(tickers)
        sweep_results = book.sweep_pending_makers(live_snap)
        if sweep_results:
            _stderr(f"Maker sweep: {len(sweep_results)} events")
            for r in sweep_results:
                _stderr(f"  → {r}")
                if r.get("status") == "filled_maker":
                    pos = next((p for p in book.open if p.get("paper_order_id") == r["paper_order_id"]), None)
                    if pos and pos.get("brier_logged", True):
                        _stderr(f"  → Brier already logged for {r['paper_order_id']}, skipping")
                        continue
                    # Get orderbook snapshot for fill-rate model
                    ob_snap = live_snap.get(r["ticker"], {})
                    _bb = ob_snap.get("yes_bid") if r["side"] == "yes" else ob_snap.get("no_bid")
                    _ba = ob_snap.get("yes_ask") if r["side"] == "yes" else ob_snap.get("no_ask")
                    rid = brier.log_prediction(
                        ticker=r["ticker"],
                        side=r["side"],
                        our_prob=r["fair_prob"],
                        market_prob=_market_mid_prob(_bb, _ba, r["price_cents"]),
                        qty=r["qty"],
                        price_cents=r["price_cents"],
                        mode="maker",
                        timestamp_utc=datetime.now(timezone.utc).isoformat(),
                        best_bid=_bb,
                        best_ask=_ba,
                    )
                    # Store brier_record_id back into the open position
                    if pos:
                        pos["brier_record_id"] = rid
                        pos["brier_logged"] = True
                        book._save()

    # 1b. Daily/weekly $ kill-switch — HARD STOP on new entries when the loss limit
    # is hit. Previously check_budget() was only called by run_trader.py (manual),
    # so the live cron path had no hard daily stop (only the soft live_size_scale).
    # The maker sweep above (managing existing exposure) still runs; this only blocks
    # OPENING new exposure. Paper arms set a huge limit so this is a live-only stop.
    ok_budget, budget_reason = risk.check_budget()
    if not ok_budget:
        _stderr(f"🛑 BUDGET STOP: {budget_reason} → no new entries this run")
        if args.live:
            try:
                from trader.notify import alert
                alert(f"LIVE budget stop — halting new entries: {budget_reason}", key="live_budget_stop")
            except Exception:
                pass
        print(json.dumps({
            "status": "budget_stop",
            "reason": budget_reason,
            "book_summary": book.summary(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        return 0

    # 2. Scan fair values
    # Kelly sizing keyed off live cash (not starting bank) so we de-risk after drawdowns
    bankroll_cents = book.cash_cents
    if args.live:
        # Pin weather risk limits to allocated capital, excluding unrelated cash.
        bankroll_cents = _pinned_live_bankroll(bankroll_cents)
    risk.set_bankroll(bankroll_cents)  # real bank → caps / city-throttle %
    scan_bankroll = bankroll_cents
    # 2026-06-19 Claude audit: live mode lead-time window filter
    if args.live:
        os.environ["KALSHI_WEATHER_LIVE_MODE"] = "1"
        # 2026-06-19 P&L work: scale-on-proof — live Kelly sizing shrinks until
        # the (realistic-fill) paper/live week P&L proves out. Sizes only down.
        _scale = risk.live_size_scale()
        scan_bankroll = int(bankroll_cents * _scale)
        _stderr(f"LIVE size scale: {_scale:.2f} (scale-on-proof; week_pnl=${risk.get_summary()['week_pnl_dollars']:.2f})")
        # Gradual size ramp (default OFF). When enabled, the per-market cap steps up a
        # gated ladder on proof instead of the fixed $5. Ground-truth fills + balance.
        if os.environ.get("KALSHI_WEATHER_SIZE_RAMP") == "1" and state_dir:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from reconcile_fills import live_fill_stats
                live_fills = int((live_fill_stats(state_dir) or {}).get("filled") or 0)
                ss = state_dir / "sync-state.json"
                if ss.exists():
                    with open(ss) as _sf:
                        _sd = json.load(_sf)
                    # Prefer the weather-only balance-equivalent (2026-07-05 re-key: immune to
                    # non-weather sports/Iran settlements); fall back to the legacy account total,
                    # then to book cash, so an old-schema sync-state degrades safely.
                    total_cents = int(_sd.get("last_weather_total_cents",
                                              _sd.get("last_total_cents", book.cash_cents)))
                else:
                    total_cents = book.cash_cents
                rung_cap = risk.live_size_rung(live_fills, total_cents)
                risk.per_event_max_position_dollars = rung_cap
                _stderr(f"LIVE size ramp ON: rung cap ${rung_cap}/market (live_fills={live_fills})")
            except Exception as e:
                _stderr(f"size ramp skipped (fell back to fixed cap): {e}")
        # Live quote-staleness refresh: cancel stale Kalshi resting quotes the ORDER BOOK has moved
        # against (band-keyed via _band_stale/premium_quote_price, NOT the model) BEFORE placing new
        # ones; cancel-only, the placement loop re-posts. Flag-gated (default off → no live change).
        try:
            _n_ref = _refresh_stale_live_quotes(book, risk, prod=True)
            if _n_ref:
                _stderr(f"LIVE quote refresh: {_n_ref} book-stale quote(s) cancelled")
        except Exception as _e:
            _stderr(f"live quote refresh skipped (non-fatal): {_e}")
    signals = scan(args.fair_values, risk, mode=args.mode, top_n=10, bankroll_cents=scan_bankroll)
    if not signals:
        _stderr("No signals from scanner.")
        print(json.dumps({"status": "no_signals", "book_summary": book.summary()}, indent=2))
        return 0

    _stderr(f"Scanner produced {len(signals)} signals; verifying live liquidity...")

    placed = 0
    placed_notional_cents = 0   # QA-02: cumulative worst-case notional placed THIS run; fed to the budget stop
    results = []
    live_snap_all: dict = {}

    # Idempotency: skip tickers already in open positions or pending makers.
    # Prevents duplicate exposure if the trader runs multiple times per day.
    existing_tickers = {p["ticker"] for p in book.open + book.pending_makers}

    # 2026-06-19 P&L work: max_open_positions cap now enforced (was the long-
    # standing KNOWN GAP from 2026-06-01). Counts open positions + resting maker
    # quotes; checked per-signal below via risk.check_open_positions().
    open_exposure_count = len(book.open) + len(book.pending_makers)

    # LIVE MODE: enforce a max-orders ceiling (env-tunable). Raised 2→4 (2026-06-23)
    # to accrue live fills faster for reconcile_fills.py, now that the daily $ stop
    # is actually enforced (see 1b above). Lower KALSHI_WEATHER_LIVE_MAX_ORDERS to
    # tighten. Per-position is still capped ($5/market) and the daily stop halts losses.
    if args.live:
        live_ceiling = int(os.environ.get("KALSHI_WEATHER_LIVE_MAX_ORDERS", "4"))
        if args.max_orders > live_ceiling:
            _stderr(f"⚠️ LIVE MODE: capping max-orders to {live_ceiling}")
            args.max_orders = live_ceiling

    # ── n_eff gate for live mode: skip cities with < 3 effective observations ──
    if args.live:
        from model.error_tracker import ErrorTracker
        et = ErrorTracker()
        et.load()

    # ── QA-01 (2026-07-01): fold the arm's OWN resting live orders into the dedup set ──
    # The live arm posts real resting maker quotes, but book.open holds only FILLED positions and
    # sync hardcodes pending_makers=[], so neither existing_tickers nor check_event_limit ever sees
    # an unfilled resting order. Without this, a re-emitted same-ticker signal on a later cycle stacks
    # a SECOND real order on a ticker we already have resting exposure on, breaching the per-event cap.
    # Fail CLOSED: if we can't list resting orders we can't prove we're not stacking, so place nothing.
    live_place_blocked = False
    _resting: list = []
    if args.live:
        from trader.orders import list_resting_orders
        _resting, _rest_err = list_resting_orders(prod=True)
        if _rest_err is not None:
            _stderr(f"🛑 QA-01: could not list resting live orders ({_rest_err}) — "
                    f"skipping live placement this cycle (fail-closed)")
            live_place_blocked = True
        else:
            for _o in _resting:
                _tk = _o.get("ticker")
                if _tk:
                    existing_tickers.add(_tk)
            _stderr(f"  ⓘ QA-01: {len(_resting)} resting live order(s) folded into dedup set")

    # ── Per-event $ cap: seed the exposure view from REAL held positions (audit 2026-07-08) ──
    # check_event_limit sums risk._open_positions, but the live path loads an EMPTY open_positions
    # from risk-state and never calls record_order → the $5/event cap silently collapsed to a
    # per-ORDER check, letting multiple STRIKES of one event stack across cycles (dedup is
    # same-ticker only). Seed from the synced held book so the cap counts prior-cycle strikes;
    # each in-run placement is appended below so strikes placed WITHIN this run also aggregate.
    # In-memory only (paper_trade.py never persists risk state → no cross-cycle double-count).
    # 2026-07-09 (QA-01 residual): also seed the notional of RESTING unfilled makers — held
    # positions alone let a strike-B candidate slip past the cap while a strike-A maker of the
    # same event sat resting. remaining_count excludes the filled part (no double-count with
    # book.open). If the resting list failed, live_place_blocked already stops all placements,
    # so the missing seed is moot that cycle.
    if args.live:
        _basis = {r.get("paper_order_id"): r for r in _read_lifecycle(book)
                  if r.get("event") == "posted_live" and r.get("paper_order_id")}
        _rest_items = _resting_exposure(_resting, _basis)
        risk.seed_open_positions([
            {"ticker": p.get("ticker"), "count": p.get("qty", 0), "avg_cost_cents": p.get("avg_entry_cents", 0)}
            for p in book.open
        ] + _rest_items)
        if _rest_items:
            _stderr(f"  ⓘ event-cap seed: {len(book.open)} held + {len(_rest_items)} resting item(s)")
        # Correlated same-side cap (drawdown fix #1): seed SAME-SIDE exposure across cities from
        # held positions + resting makers (side-preserving). No-op unless
        # KALSHI_WEATHER_MAX_CORRELATED_DIRECTION_* is armed, so the default live path is unchanged.
        if risk.correlated_cap_enabled:
            _dir_seed = [
                {"ticker": p.get("ticker"), "side": p.get("side"),
                 "count": p.get("qty", 0), "avg_cost_cents": p.get("avg_entry_cents", 0)}
                for p in book.open
            ] + [
                {"ticker": (o.get("ticker") or o.get("market_ticker") or ""),
                 "side": (o.get("side") or "").lower(),
                 "count": int(o.get("remaining_count") or o.get("count") or 0),
                 "avg_cost_cents": _resting_price(o, (o.get("side") or "").lower(), _basis)}
                for o in (_resting or [])
                if (o.get("side") or "").lower() in ("yes", "no")
                and (o.get("action") or "").lower() != "sell"
            ]
            risk.seed_direction_exposure(_dir_seed)
            _dbc = risk.direction_bucket_counts()
            _stderr(f"  ⓘ correlated-side cap armed: max {risk.max_correlated_direction_events} "
                    f"events/side (seeded YES={_dbc.get('yes', 0)} NO={_dbc.get('no', 0)})")

    for sig in signals:
        if placed >= args.max_orders or live_place_blocked:
            break

        # n_eff check (live only). 2026-06-21: BYPASS for premium-capture — n_eff
        # gates on forecast-error history, which is irrelevant to the forecast-
        # decoupled premium edge; gating it would block premium-live entirely.
        if args.live and getattr(sig, "source", "") != "premium":
            city = sig.ticker.split('-')[0].replace('KXHIGHT','').replace('KXLOWT','').replace('KXHIGH','').replace('KXLOW','').replace('KXTEMP','')
            from data.weather_data import CITY_ALIASES as _CA   # KXHIGHNY -> "NY" -> "NYC" (QA 2026-06-25)
            city = _CA.get(city, city)
            n = et.get_effective_n(city)
            if n < 3.0:
                _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: n_eff={n:.1f} < 3.0 → SKIP (insufficient error data)")
                continue

        ok, reason = risk.check_duplicate_exposure(sig.ticker, existing_tickers)
        if not ok:
            _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: {reason}")
            continue

        # 2026-06-19 P&L work: total concurrent exposure cap.
        ok_cap, cap_reason = risk.check_open_positions(open_exposure_count + placed)
        if not ok_cap:
            _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: {cap_reason} → STOP opening new exposure")
            break

        # Correlated same-side cap (drawdown fix #1): skip a signal that would exceed the
        # allowed count of DISTINCT same-SIDE events across cities (or the side $ notional).
        # No-op unless armed → default live path is unchanged. `continue` (not break): an
        # opposite-side signal later in the list may still be placeable.
        ok_corr, corr_reason = risk.check_correlated_direction(
            sig.ticker, sig.side, sig.qty, sig.limit_price_cents)
        if not ok_corr:
            _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: {corr_reason} → skip (correlated-direction cap)")
            continue

        # Re-fetch live market right now (prices move)
        live = fetch_live_market(sig.ticker)
        if not live:
            _stderr(f"  ✗ {sig.ticker}: live fetch failed")
            continue
        
        # Spread filter (2026-06-17): wide spread = low conviction in market price.
        # Skip when spread > 8¢ unless KALSHI_WEATHER_SPREAD_FILTER_OFF=1 (paper mode).
        if not os.environ.get("KALSHI_WEATHER_SPREAD_FILTER_OFF"):
            spread = int(live.get("spread_cents", 0) or 0)
            if spread > 8:
                _stderr(f"  ✗ {sig.ticker}: spread too wide ({spread}¢ > 8¢)")
                continue
        
        live_snap_all[sig.ticker] = live

        # ── LIVE MODE: place real orders via Kalshi API ────────────────
        if args.live:
            # Kill-switch re-check: a LIVE_HALT tripped mid-run (after the top-of-run check,
            # during blocking fetches) must still stop new orders (#1, 2026-06-24 review).
            from trader.halt import is_halted_safe as _is_halted_safe
            _halted, _hr = _is_halted_safe()
            if _halted:
                _stderr(f"🛑 LIVE_HALT active mid-run ({_hr}) — stopping live placement")
                break
            price = sig.limit_price_cents if args.mode == "mm" else _live_ask(live, sig.side)
            qty = sig.qty
            if args.mode == "taker" and price <= 0:
                _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: zero live liquidity")
                continue
            # Maker-cross guard (audit 2026-07-06): sig.limit_price_cents is the cycle-start
            # (fair-values snapshot) join-bid, minutes-to-hours stale. If the book dropped since,
            # a GTC bid at that price can be AT/ABOVE the fresh ask → it crosses and fills as a
            # TAKER, paying the taker fee the premium model books as 0 and donating the very spread
            # the strategy exists to harvest (then logged as a maker post, corrupting fill stats).
            # We hold the fresh `live` book here — skip rather than post a crossing "maker".
            if args.mode == "mm" and _maker_would_cross(sig.side, price, live):
                _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: stale maker {price}¢ ≥ live "
                        f"{sig.side}_ask {_live_ask(live, sig.side)}¢ — would cross as TAKER, skip")
                continue
            # Re-enforce the per-market $ cap at the ACTUAL placement price. The scanner
            # sized qty at the scan price; a fresh live price can drift up and breach the
            # cap. check_event_limit also counts existing exposure in this ticker across
            # runs. (run_trader.py does this; the live cron path previously did not.)
            cap_dollars = int(getattr(risk, "per_event_max_position_dollars", 0) or 0)
            if cap_dollars > 0 and price > 0:
                max_qty = (cap_dollars * 100) // price
                if max_qty < qty:
                    _stderr(f"  ⤵ {sig.ticker}: cap ${cap_dollars} @ {price}¢ → qty {qty}→{max_qty}")
                    qty = max_qty
                if qty <= 0 or not risk.check_event_limit(sig.ticker, qty, price):
                    _stderr(f"  ✗ {sig.ticker}: per-market cap ${cap_dollars} reached — skip")
                    continue
            # Per-order daily/weekly budget re-check (the top-of-run check ran once, pre-loop).
            # QA-02 (2026-07-01): record_order is never called on the live path, so today/week PnL
            # only moves on realized settlements (fed by the pre-run sync), NEVER on orders placed
            # earlier in THIS run — a static counter meant every per-order check saw the same number
            # and the stop never tightened. Pass the CUMULATIVE worst-case notional placed so far this
            # run (in-memory only; real losses still arrive via settlements next sync, so no phantom
            # double-count) so the run actually stops once its placements would breach the cap.
            _ok_b, _b_reason = risk.check_budget(prospective_cost_cents=-(placed_notional_cents + qty * price))
            if not _ok_b:
                _stderr(f"  🛑 {sig.ticker}: budget stop ({_b_reason}) — stopping live placement")
                break
            result = place_limit_order(
                market_ticker=sig.ticker, side=sig.side,
                price_cents=price, qty=qty,
                prod=True, dry_run=False,
            )
            results.append({"signal": sig.to_dict(), "live_order": {"success": result.success, "order_id": result.order_id, "error": result.error}})
            if result.success:
                placed += 1
                placed_notional_cents += qty * price   # QA-02: tighten the in-run budget stop
                risk.add_open_position(sig.ticker, qty, price)   # per-event cap: aggregate this strike for later signals THIS run
                risk.add_direction_exposure(sig.ticker, sig.side, qty, price)  # correlated-direction cap (fix #1); no-op unless armed
                # Record what we posted live so bin/reconcile_fills.py can compute the
                # live fill rate (denominator) and compare it to the paper fill model.
                book._log_lifecycle("posted_live", {
                    "ticker": sig.ticker, "side": sig.side,
                    "limit_price_cents": price, "qty": qty,
                    "paper_order_id": result.order_id,
                    "fair_prob_at_post": sig.fair_prob,   # drift basis for the quote-staleness refresh
                    # Book context AT POST (P0b, 2026-07-01): the ONLY data that can ever identify the
                    # aggr/spread/depth slopes distinguishing passive vs premium fills. Append-only,
                    # behavior-neutral. `live` is this signal's snapshot (set at live_snap_all[sig.ticker]).
                    # Downstream aggr = limit_price_cents - {side}_bid; needs ~2-3wk to accumulate.
                    "yes_bid_at_post": int(live.get("yes_bid", 0) or 0),
                    "yes_ask_at_post": int(live.get("yes_ask", 0) or 0),
                    "no_bid_at_post": int(live.get("no_bid", 0) or 0),
                    "no_ask_at_post": int(live.get("no_ask", 0) or 0),
                    "spread_cents_at_post": int(live.get("spread_cents", 0) or 0),
                    "side_bid_qty_at_post": float(live.get(f"{sig.side}_bid_qty", 0) or 0),
                })
                _stderr(f"  ✓ LIVE {args.mode.upper()} {sig.ticker} {sig.side.upper()} qty={qty} @ {price}¢  order_id={result.order_id}")
            else:
                _stderr(f"  ✗ LIVE ORDER REJECTED {sig.ticker}: {result.error}")
            continue

        if args.mode == "taker":
            ask = _live_ask(live, sig.side)
            if ask <= 0:
                _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: zero live ask")
                continue
            # Volume gate: require ask depth (prevents phantom fills on empty books)
            ask_qty = int(live.get(f"{sig.side}_ask_qty", 0) or 0)
            if ask_qty <= 0:
                _stderr(f"  ✗ {sig.ticker} {sig.side.upper()}: zero ask depth (ask={ask}¢ qty={ask_qty})")
                continue
            # Re-validate edge against the CURRENT live ask
            from trader.orders import compute_post_fee_edge
            yes_bid = int(live.get("yes_bid", 0) or 0)
            yes_ask = int(live.get("yes_ask", 0) or 0)
            no_ask  = int(live.get("no_ask", 0) or 0)
            edge = compute_post_fee_edge(sig.fair_prob, yes_ask, no_ask, sig.qty)
            if edge["side"] != sig.side or edge["edge_cents_per_contract"] < 4:
                _stderr(f"  ✗ {sig.ticker}: edge degraded (now {edge['side']}@{edge['edge_cents_per_contract']:.1f}¢)")
                continue

            # Fill against live ask
            fill = book.fill_taker(
                ticker=sig.ticker, side=sig.side,
                ask_price_cents=ask, qty=sig.qty,
                fair_prob=sig.fair_prob,
                edge_cents_post_fee=sig.edge_cents_post_fee,
                confidence=sig.confidence,
                market_close_time=sig.market_close_time,
                rationale=sig.rationale,
                forecast_f=sig.forecast_f,
            )
            results.append({"signal": sig.to_dict(), "fill": fill, "live_market": {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_ask": no_ask}})
            if fill["status"] == "filled":
                placed += 1
                _stderr(f"  ✓ FILLED {sig.ticker} {sig.side.upper()} qty={sig.qty} @ {ask}¢  edge={edge['edge_cents_per_contract']:.1f}¢")
                # Log Brier prediction at trade entry
                pos = next((p for p in book.open if p.get("paper_order_id") == fill["paper_order_id"]), None)
                if pos and pos.get("brier_logged", True):
                    _stderr(f"  → Brier already logged for {fill['paper_order_id']}, skipping")
                else:
                    _bb = yes_bid if sig.side == "yes" else (100 - yes_ask)
                    _ba = ask if sig.side == "yes" else (100 - yes_bid)
                    rid = brier.log_prediction(
                        ticker=sig.ticker,
                        side=sig.side,
                        our_prob=sig.fair_prob,
                        market_prob=_market_mid_prob(_bb, _ba, ask),
                        qty=sig.qty,
                        price_cents=ask,
                        mode="taker",
                        timestamp_utc=datetime.now(timezone.utc).isoformat(),
                        best_bid=_bb,
                        best_ask=_ba,
                    )
                    # Attach brier_record_id to the open position
                    if pos:
                        pos["brier_record_id"] = rid
                        pos["brier_logged"] = True
                        book._save()
            else:
                _stderr(f"  ✗ REJECTED {sig.ticker}: {fill}")

        else:  # mm
            posted = book.post_maker(
                ticker=sig.ticker, side=sig.side,
                limit_price_cents=sig.limit_price_cents, qty=sig.qty,
                fair_prob=sig.fair_prob, edge_cents_post_fee=sig.edge_cents_post_fee,
                confidence=sig.confidence,
                market_close_time=sig.market_close_time,
                rationale=sig.rationale,
                forecast_f=sig.forecast_f,
            )
            results.append({"signal": sig.to_dict(), "posted": posted})
            if posted["status"] == "posted":
                placed += 1
                _stderr(f"  ⏳ POSTED MAKER {sig.ticker} {sig.side.upper()} qty={sig.qty} @ {sig.limit_price_cents}¢")
                # For makers, we log Brier at POST time (prediction made now);
                # outcome recorded only if it later fills.
                pm = next((p for p in book.pending_makers if p.get("paper_order_id") == posted["paper_order_id"]), None)
                if pm and pm.get("brier_logged", True):
                    _stderr(f"  → Brier already logged for {posted['paper_order_id']}, skipping")
                else:
                    # Get orderbook snapshot for maker post
                    ob_snap = live_snap_all.get(sig.ticker, {})
                    best_side_bid = ob_snap.get("yes_bid") if sig.side == "yes" else ob_snap.get("no_bid")
                    best_side_ask = ob_snap.get("yes_ask") if sig.side == "yes" else ob_snap.get("no_ask")
                    rid = brier.log_prediction(
                        ticker=sig.ticker,
                        side=sig.side,
                        our_prob=sig.fair_prob,
                        market_prob=_market_mid_prob(best_side_bid, best_side_ask, sig.limit_price_cents),
                        qty=sig.qty,
                        price_cents=sig.limit_price_cents,
                        mode="maker",
                        timestamp_utc=datetime.now(timezone.utc).isoformat(),
                        best_bid=best_side_bid,
                        best_ask=best_side_ask,
                    )
                    # Store on the pending maker so if it fills we can link
                    if pm:
                        pm["brier_record_id"] = rid
                        pm["brier_logged"] = True
                        book._save()
            else:
                _stderr(f"  ✗ MAKER POST REJECTED {sig.ticker}: {posted}")

    _stderr(_format_summary(book, live_snap_all))

    print(json.dumps({
        "status": "ok",
        "mode": args.mode,
        "placed": placed,
        "results": results,
        "book_summary": book.summary(live_snap_all),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
trader/scanner.py — Scan fair-values.json, apply risk gate, return scored signals.
"""

import json
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trader.orders import compute_post_fee_edge, compute_maker_quote, fee_per_contract_cents, kelly_qty
from trader.risk import RiskGate
from model.calibrate import IsotonicCalibrator

# ── Calibrated-probability sizing (2026-06-19 P&L work) ───────────────────
# The model is ~2x overconfident (mean predicted 62% vs actual 42% WR). Sizing
# Kelly off the raw fair_prob oversizes every bet. Map the raw prob through the
# fitted isotonic curve (state/calibration.json) before sizing. Fail-safe: if no
# fitted curve is found, calibrate() returns the input unchanged.
_MAIN_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_CALIBRATOR: Optional[IsotonicCalibrator] = None


def _calibrated_prob(p: float) -> float:
    """Raw model prob -> isotonic-calibrated prob, for position sizing only.

    Never raises and never increases risk: on any failure or missing curve it
    returns the raw prob. Disable with KALSHI_WEATHER_CALIBRATED_SIZING=0.
    """
    if os.environ.get("KALSHI_WEATHER_CALIBRATED_SIZING", "1") == "0":
        return p
    global _CALIBRATOR
    try:
        if _CALIBRATOR is None:
            _CALIBRATOR = IsotonicCalibrator(state_dir=_MAIN_STATE_DIR)
            _CALIBRATOR.load()
        return _CALIBRATOR.calibrate(p)
    except Exception:
        return p


def _sizing_prob(fair_prob_yes: float, side: str) -> float:
    """Probability used for Kelly sizing on `side`.

    Uses the isotonic-calibrated prob, but CLAMPED so calibration can only ever
    *reduce* the bet size, never increase it. On our bet subset the model is
    ~2x overconfident (62% predicted vs 42% realised), but the global isotonic
    curve maps some mid-range probs upward; sizing up on those would raise risk
    on a strategy that is currently losing live. min(raw, calibrated) keeps the
    shrink where the curve corrects overconfidence and is a no-op otherwise.
    """
    raw = fair_prob_yes if side == "yes" else (1.0 - fair_prob_yes)
    cal_yes = _calibrated_prob(fair_prob_yes)
    cal = cal_yes if side == "yes" else (1.0 - cal_yes)
    return min(raw, cal)


def _edge_fair_cents(fair_prob_yes: float, side: str) -> int:
    """Per-side fair value (cents) used for EDGE / gating decisions.

    OFF (default): the raw model prob — byte-identical to today's ``fair_yes_cents``
    (YES) and ``100 - fair_yes_cents`` (NO).
    ON (KALSHI_WEATHER_CALIBRATED_EDGE=1): the DOWN-ONLY calibrated prob via
    ``_sizing_prob`` = min(raw, calibrated) for that side, so calibrating the GATE
    can only ever TIGHTEN entries (never open a trade the raw prob wouldn't). Sizing
    already uses _sizing_prob; this brings the entry decision into line with it.
    """
    if os.environ.get("KALSHI_WEATHER_CALIBRATED_EDGE", "0") != "1":
        yes_c = max(1, min(99, round(fair_prob_yes * 100)))
        return yes_c if side == "yes" else (100 - yes_c)
    return max(1, min(99, round(_sizing_prob(fair_prob_yes, side) * 100)))


def _downonly_post_fee_edge(fair_prob_yes: float, yes_ask_cents: int, no_ask_cents: int, qty: int) -> dict:
    """Down-only-calibrated twin of orders.compute_post_fee_edge. Same input
    validation, return-dict shape, and ``yes_edge >= no_edge`` tiebreak, but each
    side's fair value comes from _edge_fair_cents (YES and NO independently shrunk).
    Only called when KALSHI_WEATHER_CALIBRATED_EDGE=1."""
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
    yes_fair = _edge_fair_cents(fair_prob_yes, "yes")
    no_fair = _edge_fair_cents(fair_prob_yes, "no")
    yes_fee = fee_per_contract_cents(qty, yes_ask_cents, maker=False)
    yes_edge = yes_fair - yes_ask_cents - yes_fee
    no_fee = fee_per_contract_cents(qty, no_ask_cents, maker=False)
    no_edge = no_fair - no_ask_cents - no_fee
    if yes_edge >= no_edge:
        return {
            "side": "yes",
            "limit_price_cents": yes_ask_cents,
            "edge_cents_per_contract": round(yes_edge, 2),
            "est_profit_per_contract_cents": round(yes_edge, 2),
            "fee_per_contract_cents": round(yes_fee, 2),
            "raw_edge_cents": round(yes_fair - yes_ask_cents, 2),
        }
    return {
        "side": "no",
        "limit_price_cents": no_ask_cents,
        "edge_cents_per_contract": round(no_edge, 2),
        "est_profit_per_contract_cents": round(no_edge, 2),
        "fee_per_contract_cents": round(no_fee, 2),
        "raw_edge_cents": round(no_fair - no_ask_cents, 2),
    }


def _book_is_tradeable(yes_bid: int, yes_ask: int) -> bool:
    """Liquidity gate (2026-06-19 P&L work): only quote where a genuine two-sided
    book exists with a sane spread. 65% of these books are one-sided (a side at
    0/99, median gap 67c); posting into them produces paper "fills" that don't
    fill — or fill adversely — live. Enforced in live mode only by default so the
    paper data-collection stream is unaffected. Fail-safe: only ever *blocks*
    trades. Tunable via KALSHI_WEATHER_MAX_SPREAD_CENTS.
    """
    if os.environ.get("KALSHI_WEATHER_REQUIRE_TWO_SIDED",
                      "1" if os.environ.get("KALSHI_WEATHER_LIVE_MODE", "") == "1" else "0") != "1":
        return True
    max_spread = int(os.environ.get("KALSHI_WEATHER_MAX_SPREAD_CENTS", "15"))
    # Genuine two-sided book: both sides present, not pinned to 0/99, sane spread.
    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= 99 or yes_ask >= 100:
        return False
    if yes_ask <= yes_bid:  # crossed/locked or manufactured (bid==ask fallback)
        return False
    if (yes_ask - yes_bid) > max_spread:
        return False
    return True

# Module-level logger (FIX 2026-06-12: was undefined; NameError fired 3x per cycle
# whenever KALSHI_WEATHER_SIDE_BIAS=yes|no|reset was exported — i.e. every cycle,
# since run-cycle.sh exports it by default).
logger = logging.getLogger("kalshi.scanner")

# FIX 2026-06-18: NO bets have 46% accuracy vs YES at 86% (n=81 settled).
# NO-side signals must clear 2x the edge threshold of YES. In maker mode,
# this means NO needs 30¢ edge when YES needs 15¢.
NO_EDGE_MULTIPLIER = float(os.environ.get("KALSHI_WEATHER_NO_EDGE_MULTIPLIER", "1.4"))  # 2026-06-19 P&L work: full n=442 shows NO edge +12.8pt ≈ YES +12.5pt; the 2.0 penalty (from n=81) over-blocked NO (~46% of gross P&L). Eased to 1.4 (modest penalty retained for the 06-18 directional-accuracy concern). Override via env.

# FIX 2026-06-18 (#3 priority): Cross-city correlation regions.
# When multiple signals come from the same climate region, they're not
# independent — they all bet on the same weather pattern. Discount
# position size for correlated signals to avoid over-concentration.
CLIMATE_REGIONS = {
    "SW": ["PHX", "LV", "LAX", "SATX", "DAL", "HOU"],    # Southwest/Texas (LV = Las Vegas station code, not "LAS")
    "SE": ["ATL", "MIA", "NOLA", "DC", "BOS"],            # Southeast/Atlantic
    "NE": ["NYC", "PHIL", "BOS", "CHI", "MIN"],            # Northeast/Midwest
    "NW": ["SEA", "SFO", "DEN"],                             # Pacific Northwest/Mountain
    "PLAINS": ["OKC", "DAL", "CHI", "MIN", "DEN"],          # Great Plains (overlap OK)
}
SAME_REGION_DISCOUNT = 0.70  # Scale each signal to 70% of normal when region has ≥2 signals

# 2026-06-19 Claude audit: 12-36h lead time is the sweet spot.
# Live mode should ONLY trade this window to avoid the 0-6h dead zone.
LIVE_LEAD_TIME_MIN_HOURS = float(os.environ.get("KALSHI_WEATHER_LIVE_MIN_LEAD_HOURS", "12"))
# 2026-06-20 model work: tightened 36→24h. Both Brier (0.16-0.22 @ 12-24h vs 0.38 @ 24h+)
# and CRPS (≈0.8-1.0 @ 12-24h vs 2.25 @ 24h+, via bin/crps_report.py) show the model's
# distribution is only skillful in the 12-24h band; 24h+ is near-random. Env-overridable.
LIVE_LEAD_TIME_MAX_HOURS = float(os.environ.get("KALSHI_WEATHER_LIVE_MAX_LEAD_HOURS", "24"))


@dataclass
class Signal:
    ticker: str
    side: str                    # "yes" | "no"
    limit_price_cents: int
    qty: int
    fair_prob: float
    edge_cents_post_fee: float
    est_profit_dollars: float
    confidence: str
    rationale: str
    market_close_time: str
    source: str
    mode: str                    # "taker" | "maker"
    raw_edge_cents: float
    fee_per_contract_cents: float
    forecast_f: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_yes_asks(market: dict) -> tuple[int, int]:
    """Extract best YES bid and YES ask from _market snapshot in cents."""
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    # Try alternative fields
    if not yes_bid and not yes_ask:
        ybd = market.get("yes_bid_dollars")
        yad = market.get("yes_ask_dollars")
        if ybd:
            yes_bid = int(float(ybd) * 100)
        if yad:
            yes_ask = int(float(yad) * 100)
    if not yes_ask or yes_ask == 0:
        yes_ask = yes_bid  # fallback to mid/last if no ask
    if not yes_bid or yes_bid == 0:
        yes_bid = yes_ask
    return yes_bid, yes_ask


def premium_quote_price(market: dict, offset: Optional[int] = None):
    """The (side, price_cents) the premium-capture strategy would rest for this market RIGHT NOW,
    derived from its CURRENT book — or None if the market isn't premium-quotable (band/spread gates
    fail or neither side's bid is in-band). Single source of truth for premium pricing: the scanner
    posts it, and the quote-staleness refresh keys on it so staleness is measured against the BOOK
    the quote was priced from (the premium edge is structural/book-derived), NOT the model fair value
    (which bin/tail_edge.py documents as anti-predictive for this edge). (Stream 3 band-keyed, 2026-06-25)"""
    pmin = int(os.environ.get("KALSHI_WEATHER_PREMIUM_MIN_CENTS", "20"))
    pmax = int(os.environ.get("KALSHI_WEATHER_PREMIUM_MAX_CENTS", "60"))
    max_spread = int(os.environ.get("KALSHI_WEATHER_PREMIUM_MAX_SPREAD", "12"))
    # Adverse-markout CUSHION gate (2026-07-02, proposal #4 "fewer, less-adverse fills"): resting at
    # the join-bid, the book cushion below the mid is ~half the spread. If the spread is too tight,
    # that cushion can't cover the CONFIRMED ~-5c post-fill adverse markout (markout_kill_test), so
    # the fill is a near-certain net loss. Requiring a MIN spread skips those cushionless fills →
    # fewer, less-adverse fills, book-derived (model-agnostic, matches the premium thesis). DEFAULT 0
    # (OFF, no behavior change): activate once the resumed spread_cents_at_post telemetry shows the
    # markout-by-spread distribution — picking a non-zero value blind could zero out the arm.
    min_spread = int(os.environ.get("KALSHI_WEATHER_PREMIUM_MIN_SPREAD", "0"))
    if offset is None:
        offset = int(os.environ.get("KALSHI_WEATHER_PREMIUM_QUOTE_OFFSET", "0"))
    yes_bid, yes_ask = _extract_yes_asks(market)
    spread = yes_ask - yes_bid
    if yes_bid <= 0 or yes_ask <= 0 or spread <= 0 or spread > max_spread or spread < min_spread:
        return None
    no_bid = 100 - yes_ask
    pick = ("yes", yes_bid) if pmin <= yes_bid <= pmax else \
           ("no", no_bid) if pmin <= no_bid <= pmax else None
    if not pick:
        return None
    pside, pbid = pick
    ceil_px = (yes_ask - 1) if pside == "yes" else ((100 - yes_bid) - 1)
    return pside, max(1, min(pbid + offset, ceil_px))


def scan(fair_values_path: str, risk_gate: RiskGate, mode: str = "taker", top_n: int = 5, bankroll_cents: int = 30000) -> list[Signal]:
    """
    Load fair-values.json, filter by risk gate, compute edges, return top N signals.
    mode: "taker" | "mm" (market-making)
    """
    path = Path(fair_values_path)
    if not path.exists():
        print(f"[scanner] fair-values file not found: {path}", file=sys.stderr)
        return []

    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[scanner] error reading fair-values: {e}", file=sys.stderr)
        return []

    # Accept either flat list or wrapped {generated_at_utc, records: [...]} form
    lessons_applied = {}
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        entries = payload["records"]
        lessons_applied = payload.get("lessons_applied", {}) or {}
    elif isinstance(payload, list):
        entries = payload
    else:
        print("[scanner] fair-values must be a list or {records: [...]} object", file=sys.stderr)
        return []

    # FIX 2026-06-13: lead-time-keyed min edge. Per
    # research/kalshi-leadtime-analysis-2026-06-13.json (n=221 settled):
    #   0-6h  : brier 0.330 (worst) — model overconfident on stale diurnal
    #   6-12h : brier 0.287 — default
    #   12-18h: brier 0.219
    #   18-24h: brier 0.160 (best) — model well-calibrated, accept lower edge
    #   24h+  : brier 0.377 (worst) — wide error bars, tighten
    # Override-able via env for A/B testing: KALSHI_WEATHER_LEADTIME_FILTER=off
    LEAD_TIME_MIN_EDGE = {
        "0-6h":   25,   # stale diurnal, worst calibration — tightest
        "6-12h":  15,   # raised 10→15 2026-06-15 (consistency with risk.py)
        "12-18h": 15,   # raised 10→15 2026-06-15
        "18-24h": 10,   # best calibration period — can accept lower
        "24h+":   20,   # wide error bars
    }
    if (os.environ.get("KALSHI_WEATHER_LEADTIME_FILTER") or "").lower() == "off":
        LEAD_TIME_MIN_EDGE = {k: risk_gate.min_edge_cents_after_fees for k in LEAD_TIME_MIN_EDGE}

    def _min_edge_for_lead(close_time_str: str) -> tuple[int, str]:
        """Return (min_edge_cents, bucket_name) for a market close time."""
        if not close_time_str:
            return risk_gate.min_edge_cents_after_fees, "unknown"
        try:
            ct = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            hours = (ct - datetime.now(timezone.utc)).total_seconds() / 3600.0
            if hours < 0:
                bucket = "0-6h"  # past close still falls into the tightest bucket
            elif hours < 6:   bucket = "0-6h"
            elif hours < 12:  bucket = "6-12h"
            elif hours < 18:  bucket = "12-18h"
            elif hours < 24:  bucket = "18-24h"
            else:             bucket = "24h+"
            return LEAD_TIME_MIN_EDGE[bucket], bucket
        except Exception:
            return risk_gate.min_edge_cents_after_fees, "unknown"

    city_position_scale = lessons_applied.get("city_position_scale", {}) or {}
    # FIX 2026-06-12: allow env-var override of the LESSONS-derived side_bias
    # so we can A/B test YES vs NO without editing LESSONS.md (which is the
    # historical record). KALSHI_WEATHER_SIDE_BIAS=yes|no|reset
    #   - unset / empty : use lessons side_bias
    #   - "yes" / "no"  : force that side
    #   - "reset"       : clear side_bias (allow both sides freely)
    import os as _os
    _override = (_os.environ.get("KALSHI_WEATHER_SIDE_BIAS") or "").strip().lower()
    if _override in ("yes", "no"):
        side_bias = "prefer_" + _override
        logger.info("side_bias overridden to %s via KALSHI_WEATHER_SIDE_BIAS env", side_bias)
    elif _override == "reset":
        side_bias = None
        logger.info("side_bias reset (both sides allowed) via KALSHI_WEATHER_SIDE_BIAS env")
    else:
        side_bias = lessons_applied.get("side_bias")  # "prefer_yes" | "prefer_no" | None

    def _ticker_city(t: str) -> str:
        # KXHIGHTHOU -> HOU, KXLOWTLAX -> LAX, KXTEMPNYCH -> NYC (best effort)
        _MONTH_CODES = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
        for tag in ("HIGHT", "LOWT", "TEMP", "HIGH", "LOW"):
            if tag in t:
                city = t.split("-", 1)[0].split(tag, 1)[-1]
                # Guard against date-code false positives (e.g. MAY/JUN in malformed tickers)
                if city and city not in _MONTH_CODES and city.isalpha() and 2 <= len(city) <= 4:
                    return city
        return ""

    signals: list[Signal] = []

    for entry in entries:
        ticker = entry.get("ticker", "")
        if not ticker:
            continue

        # ── Risk gate: confidence ──
        confidence = entry.get("confidence", "low")
        if not risk_gate.check_confidence(confidence):
            continue

        # ── Risk gate: close time ──
        raw_market = entry.get("_market", {})
        close_time = raw_market.get("close_time") or raw_market.get("expiration_time") or ""
        if close_time and not risk_gate.check_close_time(close_time):
            continue

        # 2026-06-19 Claude audit: Live mode lead-time window filter.
        # Only trade 12-36h before close (sweet spot). Skip dead zones.
        # 2026-06-21: EXEMPT premium-capture — it's forecast-decoupled, so the
        # forecast "sweet spot" window doesn't apply; gating it here would block
        # premium-live entirely. Premium has its own fill/spread gates below.
        if (_os.environ.get("KALSHI_WEATHER_LIVE_MODE", "") == "1"
                and _os.environ.get("KALSHI_WEATHER_PREMIUM_MODE", "") != "1"):
            try:
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours = (ct - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours < LIVE_LEAD_TIME_MIN_HOURS or hours > LIVE_LEAD_TIME_MAX_HOURS:
                    continue
            except (ValueError, TypeError):
                pass

        fair_prob = float(entry.get("fair_prob", 0.5))  # 0.0-1.0 from data layer
        fair_yes_cents = max(1, min(99, round(fair_prob * 100)))
        yes_bid, yes_ask = _extract_yes_asks(raw_market)

        # ── Premium-capture mode (2026-06-20): structural favorite-longshot harvest ──
        # bin/tail_edge.py: the bot's real edge is NOT forecasting (it loses to market
        # Brier) — it's that the crowd underprices mid-priced (≈20-60¢) weather bins
        # (+14-18pt, significant). The forecast is ANTI-predictive for it. So: buy the
        # band broadly, size FLAT (decoupled from fair_prob), and only where a real
        # two-sided book lets us fill near our price (the fill bottleneck is the game).
        # Gated by KALSHI_WEATHER_PREMIUM_MODE; default off → other streams + live untouched.
        if _os.environ.get("KALSHI_WEATHER_PREMIUM_MODE", "") == "1":
            # Per-city skip (edge decomposition 2026-06-28): drop cities whose realized premium edge
            # bleeds (e.g. SFO -8.6c/ct, n=12). Comma-separated city codes, matched to _ticker_city
            # keys (un-aliased, e.g. SFO, MIA). Default off → no change. (KALSHI_WEATHER_PREMIUM_SKIP_CITIES)
            _skip = {c.strip() for c in _os.environ.get("KALSHI_WEATHER_PREMIUM_SKIP_CITIES", "").upper().split(",") if c.strip()}
            if _skip and _ticker_city(ticker) in _skip:
                continue
            # Pricing (band/spread gates, side pick, offset, ceiling) now lives in
            # premium_quote_price — the SAME function the quote-staleness refresh keys on, so a
            # posted quote and the refresh's staleness check can never drift apart. The offset A/B
            # (KALSHI_WEATHER_PREMIUM_QUOTE_OFFSET: >0 step inside spread, <0 rest deeper, 0 join
            # the bid) is unchanged. (Stream 3 band-keyed, 2026-06-25)
            pq = premium_quote_price(raw_market)
            if pq:
                pside, pprice = pq
                pmin = int(_os.environ.get("KALSHI_WEATHER_PREMIUM_MIN_CENTS", "20"))
                pmax = int(_os.environ.get("KALSHI_WEATHER_PREMIUM_MAX_CENTS", "60"))
                offset = int(_os.environ.get("KALSHI_WEATHER_PREMIUM_QUOTE_OFFSET", "0"))
                cap_c = int(risk_gate.per_event_max_position_dollars * 100 *
                            (city_position_scale.get(_ticker_city(ticker), 1.0)))
                qty = max(1, cap_c // max(pprice, 1))
                # YES-bin haircut (fill-edge breakdown 2026-06-30): the live arm's realized split is
                # YES-bin adversely selected (yes -4.2c/ct, n=47 vs no +9.6c/ct, n=30; and YES fills
                # 86% vs NO 77% — climatology can't pin a narrow high-temp bin, so YES entries scatter
                # off and miss). Optionally shrink (mult<1) or skip (mult<=0) YES-side premium entries
                # to test whether de-weighting the adversely-selected side lifts realized P&L. Default
                # 1.0 → NO change (live arm never sets it). Paper A/B only until a clustered,
                # multiplicity-corrected win. (KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT)
                if pside == "yes":
                    try:
                        _ym = float(_os.environ.get("KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT", "1"))
                    except (ValueError, TypeError):
                        _ym = 1.0
                    if _ym != _ym or _ym in (float("inf"), float("-inf")):   # NaN/inf → fail safe (no-op)
                        _ym = 1.0
                    if _ym <= 0.0:
                        continue                    # mult<=0 → skip YES entirely
                    _ym = min(_ym, 1.0)             # knob only ever SHRINKS YES size; never inflate
                    if _ym != 1.0:
                        qty = max(1, int(qty * _ym))
                signals.append(Signal(
                    ticker=ticker, side=pside, limit_price_cents=pprice, qty=qty,
                    fair_prob=fair_prob, edge_cents_post_fee=0.0,
                    est_profit_dollars=round(qty * 0.15, 2),   # structural band EV ≈15c/ct
                    confidence=confidence,
                    rationale=f"premium-capture {pmin}-{pmax}c band (flat size, forecast-decoupled, offset={offset:+d})",
                    market_close_time=close_time, source="premium", mode="maker",
                    raw_edge_cents=0.0, fee_per_contract_cents=0.0,
                    forecast_f=entry.get("model_temp_forecast")))
            continue

        # Liquidity gate (2026-06-19 P&L work): skip illiquid/one-sided books.
        # This is the single biggest paper→live leak — fills assumed on paper
        # don't materialise (or fill adversely) in thin books. Live-mode only by
        # default; never increases risk. See _book_is_tradeable.
        if not _book_is_tradeable(yes_bid, yes_ask):
            continue

        # Taker mode requires a live ask to lift
        if mode == "taker" and yes_ask == 0 and yes_bid == 0:
            no_ask_chk = int(raw_market.get("no_ask", 0) or 0)
            if no_ask_chk == 0:
                continue

        no_ask = (100 - yes_bid) if yes_bid else int(raw_market.get("no_ask", 0) or 0)
        no_bid = (100 - yes_ask) if yes_ask else int(raw_market.get("no_bid", 0) or 0)

        # City risk multiplier (dynamic Kelly) + lesson-driven position scaling
        scan_city = _ticker_city(ticker)
        mult, reason = risk_gate.get_city_risk_multiplier(scan_city)
        if mult == 0.0:
            print(f"[scanner] skip {ticker}: {reason}", file=sys.stderr)
            continue
        # Combine risk multiplier with lesson-based position scale
        position_scale = city_position_scale.get(scan_city, 1.0) * mult
        if mult < 1.0 and reason:
            print(f"[scanner] reduce {ticker}: {reason} x{mult:.2f}", file=sys.stderr)

        if mode == "taker":
            # Side-bias aware edge computation: when systemic bias is detected,
            # evaluate the PREFERRED side independently rather than always taking
            # the "better" side from the model's perspective.
            # FIX 2026-06-13: override min edge with lead-time bucket value.
            min_edge, lead_bucket = _min_edge_for_lead(close_time)
            if side_bias == "prefer_yes":
                # Evaluate YES independently
                yes_fair = _edge_fair_cents(fair_prob, "yes")  # calibrated-edge gate (no-op when off)
                yes_fee = fee_per_contract_cents(qty=10, price_cents=yes_ask, maker=False)
                yes_edge = yes_fair - yes_ask - yes_fee
                if yes_edge >= min_edge and yes_ask > 0:
                    edge_info = {
                        "side": "yes",
                        "limit_price_cents": yes_ask,
                        "edge_cents_per_contract": round(yes_edge, 2),
                        "est_profit_per_contract_cents": round(yes_edge, 2),
                        "fee_per_contract_cents": round(yes_fee, 2),
                        "raw_edge_cents": round(yes_fair - yes_ask, 2),
                    }
                else:
                    continue
            elif side_bias == "prefer_no":
                # Evaluate NO independently
                no_fair = _edge_fair_cents(fair_prob, "no")  # calibrated-edge gate (no-op when off)
                no_fee = fee_per_contract_cents(qty=10, price_cents=no_ask, maker=False)
                no_edge = no_fair - no_ask - no_fee
                if no_edge >= min_edge and no_ask > 0:
                    edge_info = {
                        "side": "no",
                        "limit_price_cents": no_ask,
                        "edge_cents_per_contract": round(no_edge, 2),
                        "est_profit_per_contract_cents": round(no_edge, 2),
                        "fee_per_contract_cents": round(no_fee, 2),
                        "raw_edge_cents": round(no_fair - no_ask, 2),
                    }
                else:
                    continue
            else:
                # No side bias — use the standard better-side logic. When the
                # calibrated-edge gate is on, evaluate both sides on the down-only
                # calibrated prob; otherwise the original raw-prob path (unchanged).
                if os.environ.get("KALSHI_WEATHER_CALIBRATED_EDGE", "0") == "1":
                    edge_info = _downonly_post_fee_edge(fair_prob, yes_ask, no_ask, qty=10)
                else:
                    edge_info = compute_post_fee_edge(fair_prob, yes_ask, no_ask, qty=10)
                edge = edge_info["edge_cents_per_contract"]
                if edge < min_edge:
                    continue

            # Price floor: PDF warns 1¢ contracts pay 100% fee due to ceiling rounding
            price_check = edge_info["limit_price_cents"]
            if price_check < risk_gate.min_price_cents or price_check > risk_gate.max_price_cents:
                continue

            # Quarter-Kelly sizing per PDF (¤Kelly, ≤5% bankroll, ≤$50/trade, scaled by lessons)
            # 2026-06-19 P&L work: size off the CALIBRATED prob (model is ~2x
            # overconfident) so bets aren't oversized. Edge gating above still
            # uses the raw model prob; only the size is calibrated.
            price = edge_info["limit_price_cents"]
            side_fair = _sizing_prob(fair_prob, edge_info["side"])
            per_event_cap = int(risk_gate.per_event_max_position_dollars * 100 * position_scale)
            qty = kelly_qty(
                fair_prob_for_side=side_fair,
                price_cents=price,
                bankroll_cents=bankroll_cents,
                fraction=0.25,
                per_event_cap_cents=per_event_cap,
                per_trade_cap_cents=5000,
                max_bankroll_pct=0.05,
            )
            if qty < 1:
                continue

            est_profit = qty * edge_info["edge_cents_per_contract"] / 100.0

            sig = Signal(
                ticker=ticker,
                side=edge_info["side"],
                limit_price_cents=price,
                qty=qty,
                fair_prob=fair_prob,
                edge_cents_post_fee=edge_info["edge_cents_per_contract"],
                est_profit_dollars=round(est_profit, 2),
                confidence=confidence,
                rationale=entry.get("rationale", ""),
                market_close_time=close_time,
                source=entry.get("source", "unknown"),
                mode="taker",
                raw_edge_cents=edge_info["raw_edge_cents"],
                fee_per_contract_cents=edge_info["fee_per_contract_cents"],
                forecast_f=entry.get("model_temp_forecast"),
            )
            signals.append(sig)

        else:  # market-making mode
            # FIX 2026-06-13: override min edge with lead-time bucket value.
            min_edge, lead_bucket = _min_edge_for_lead(close_time)
            # Per-side fair value (cents) for the edge/guard decisions. No-op when
            # the calibrated-edge gate is off (== fair_yes_cents / 100-fair_yes_cents).
            yes_fair_cents = _edge_fair_cents(fair_prob, "yes")
            no_fair_cents = _edge_fair_cents(fair_prob, "no")
            # If the book is totally empty, seed quotes from the model itself
            # (post 3¢ inside fair on both sides).
            if yes_bid == 0 and yes_ask == 0:
                # Post wide enough to leave ≥ min_edge (now lead-time-keyed) after maker rebate.
                conservative_offset = max(12, min_edge + 2)
                yes_quote = max(1, min(99, yes_fair_cents - conservative_offset))
                no_quote = max(1, min(99, no_fair_cents - conservative_offset))
            else:
                yes_quote, no_quote = compute_maker_quote(fair_prob, yes_bid, yes_ask)
            # Choose whichever side has a quotable price
            if risk_gate.min_price_cents <= yes_quote <= risk_gate.max_price_cents:
                rebate = fee_per_contract_cents(qty=1, price_cents=yes_quote, maker=True)
                edge = yes_fair_cents - yes_quote + rebate  # rebate is negative
                # FIX 2026-06-13: don't post YES if our price > fair value.
                # Pre-fix: KXHIGHTSEA fair_prob=0.165 was getting NO@49¢ filled
                # when model said NO was only worth 16.5¢. Filter: never post
                # if price_cents > fair_prob_side * 100.
                if yes_quote > yes_fair_cents:
                    logger.info(f"skip {ticker} YES @ {yes_quote}¢ > fair {yes_fair_cents}¢ (negative EV)")
                    pass  # fall through to NO side
                yes_min_edge = min_edge  # YES uses standard threshold
                if edge >= yes_min_edge and side_bias != "prefer_no":
                    per_event_cap = int(risk_gate.per_event_max_position_dollars * 100 * position_scale)
                    qty = kelly_qty(
                        fair_prob_for_side=_sizing_prob(fair_prob, "yes"),  # 2026-06-19: calibrated sizing (clamped down-only)
                        price_cents=yes_quote,
                        bankroll_cents=bankroll_cents,
                        fraction=0.25,
                        per_event_cap_cents=per_event_cap,
                        per_trade_cap_cents=5000,
                        max_bankroll_pct=0.05,
                    )
                    if qty < 1:
                        continue
                    sig = Signal(
                        ticker=ticker,
                        side="yes",
                        limit_price_cents=yes_quote,
                        qty=qty,
                        fair_prob=fair_prob,
                        edge_cents_post_fee=round(edge, 2),
                        est_profit_dollars=round(qty * edge / 100.0, 2),
                        confidence=confidence,
                        rationale=entry.get("rationale", ""),
                        market_close_time=close_time,
                        source=entry.get("source", "unknown"),
                        mode="maker",
                        raw_edge_cents=round(fair_prob - yes_quote, 2),
                        fee_per_contract_cents=round(rebate, 2),
                        forecast_f=entry.get("model_temp_forecast"),
                    )
                    signals.append(sig)
            if risk_gate.min_price_cents <= no_quote <= risk_gate.max_price_cents:
                rebate = fee_per_contract_cents(qty=1, price_cents=no_quote, maker=True)
                edge = no_fair_cents - no_quote + rebate  # rebate is negative
                # FIX 2026-06-13: don't post NO if our price > fair NO value.
                # fair NO value = 100 - fair_yes_cents (i.e., probability NO wins).
                fair_no_cents = no_fair_cents
                if no_quote > fair_no_cents:
                    logger.info(f"skip {ticker} NO @ {no_quote}¢ > fair {fair_no_cents}¢ (negative EV)")
                    continue
                # FIX 2026-06-18: NO bets require 2x edge (30¢ vs 15¢) due to 46% accuracy
                no_min_edge = min_edge * NO_EDGE_MULTIPLIER
                if edge >= no_min_edge and side_bias != "prefer_yes":
                    per_event_cap = int(risk_gate.per_event_max_position_dollars * 100 * position_scale)
                    qty = kelly_qty(
                        fair_prob_for_side=_sizing_prob(fair_prob, "no"),  # 2026-06-19: calibrated sizing (clamped down-only)
                        price_cents=no_quote,
                        bankroll_cents=bankroll_cents,
                        fraction=0.25,
                        per_event_cap_cents=per_event_cap,
                        per_trade_cap_cents=5000,
                        max_bankroll_pct=0.05,
                    )
                    if qty < 1:
                        continue
                    sig = Signal(
                        ticker=ticker,
                        side="no",
                        limit_price_cents=no_quote,
                        qty=qty,
                        fair_prob=fair_prob,
                        edge_cents_post_fee=round(edge, 2),
                        est_profit_dollars=round(qty * edge / 100.0, 2),
                        confidence=confidence,
                        rationale=entry.get("rationale", ""),
                        market_close_time=close_time,
                        source=entry.get("source", "unknown"),
                        mode="maker",
                        raw_edge_cents=round((100 - fair_prob) - no_quote, 2),
                        fee_per_contract_cents=round(rebate, 2),
                        forecast_f=entry.get("model_temp_forecast"),
                    )
                    signals.append(sig)

    # ── Correlation-aware signal scaling ──────────────────────────────
    # If two signals track the same (city, date) event, reduce qty proportionally
    # so total exposure to that weather event doesn't exceed a single Kelly bet.
    def _event_group_key(s: Signal) -> str:
        """Extract (city, date) key from ticker + close_time for dedup."""
        city = _ticker_city(s.ticker)
        date_str = ""
        if s.market_close_time:
            try:
                dt = datetime.fromisoformat(s.market_close_time.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        return f"{city}|{date_str}" if city and date_str else ""

    event_groups: dict[str, list[Signal]] = {}
    for s in signals:
        key = _event_group_key(s)
        if key:
            event_groups.setdefault(key, []).append(s)
    for key, group in event_groups.items():
        if len(group) > 1:
            scale = 1.0 / len(group)
            for s in group:
                s.qty = max(1, int(round(s.qty * scale)))

    # ── Climate-region correlation discount ────────────────────────
    # FIX 2026-06-18 (#3 priority): When >1 signal from same climate
    # region, scale each to SAME_REGION_DISCOUNT (0.70) to avoid
    # over-concentrating on a single weather pattern.
    region_signals: dict[str, list[Signal]] = {}
    for s in signals:
        city = _ticker_city(s.ticker)
        if city:
            for region, cities in CLIMATE_REGIONS.items():
                if city in cities:
                    region_signals.setdefault(region, []).append(s)
                    break  # first matching region wins
    for region, group in region_signals.items():
        if len(group) > 1:
            for s in group:
                s.qty = max(1, int(round(s.qty * SAME_REGION_DISCOUNT)))

    # Score by edge * sqrt(qty_affordable) * horizon_discount, descending
    def _score(s: Signal) -> float:
        horizon_discount = 1.0
        if s.market_close_time:
            try:
                # Market close times are ISO timestamps (e.g. 2026-06-12T18:00:00Z)
                close_dt = datetime.fromisoformat(s.market_close_time.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                hours_to_expiry = (close_dt - now_dt).total_seconds() / 3600.0
                if hours_to_expiry > 0:
                    # Discount signals with far-out expiry dates (less certainty).
                    # 6h-out signals get 1.0x, 48h-out get ~0.35x, 96h-out get ~0.25x.
                    horizon_discount = 1.0 / math.sqrt(max(6, hours_to_expiry) / 6)
            except (ValueError, TypeError):
                pass
        raw_score = s.edge_cents_post_fee * math.sqrt(s.qty)
        return raw_score * horizon_discount

    signals.sort(key=_score, reverse=True)
    return signals[:top_n]

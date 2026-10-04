#!/usr/bin/env python3
"""
trader/paper_book.py — Paper trading book against LIVE prod Kalshi prices.

Demo Kalshi has zero volume on weather markets, so paper trading there is
meaningless. This module simulates fills against the real prod order book.

State:
  state/paper-book.json
    {
      "version": 1,
      "cash_cents": 30000,         # paper starting bank, $300
      "open": [ Position ],
      "closed": [ ClosedTrade ],
      "filled_orders": [ FilledOrder ],  # audit trail
      "pending_makers": [ PendingMaker ], # not-yet-filled maker quotes
      "updated_utc": "..."
    }
"""
from __future__ import annotations

import json
import os
import math
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


STARTING_BANK_CENTS = int(os.environ.get("KALSHI_WEATHER_PAPER_BANK_CENTS", "30000"))  # $300 default, env-overridable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fee_cents_per_contract(price_cents: int) -> int:
    """Kalshi taker fee: ceil(0.07 * 1 * P * (1-P)) per contract, P in dollars.
    Delegates to the canonical formula in orders.py."""
    from trader.orders import _kalshi_taker_fee_cents
    return _kalshi_taker_fee_cents(1, price_cents)  # per-contract fee for qty=1


@dataclass
class FilledOrder:
    ticker: str
    side: str               # yes | no
    price_cents: int
    qty: int
    fee_cents_total: int
    cost_cents_total: int   # qty*price + fee (debit)
    fair_prob: float
    edge_cents_post_fee: float
    confidence: str
    mode: str               # taker | maker
    market_close_time: str
    filled_at_utc: str
    paper_order_id: str
    rationale: str = ""
    brier_logged: bool = False


@dataclass
class Position:
    ticker: str
    side: str
    qty: int
    avg_entry_cents: int
    fee_cents_total: int
    cost_cents_total: int   # outflow when opened
    fair_prob_at_open: float
    opened_utc: str
    market_close_time: str
    paper_order_id: str
    rationale: str = ""
    brier_record_id: Optional[str] = None
    brier_logged: bool = False
    forecast_f: Optional[float] = None


@dataclass
class ClosedTrade:
    ticker: str
    side: str
    qty: int
    entry_cents: int
    exit_cents: int                  # 100 if won, 0 if lost (settlement)
    fee_cents_total: int
    pnl_cents: int                   # net of fees
    settlement_result: str           # "yes" | "no" | "manual_close"
    opened_utc: str
    closed_utc: str
    paper_order_id: str


@dataclass
class PendingMaker:
    ticker: str
    side: str
    limit_price_cents: int
    qty: int
    fair_prob: float
    edge_cents_post_fee: float
    confidence: str
    market_close_time: str
    posted_utc: str
    paper_order_id: str
    rationale: str = ""
    brier_logged: bool = False
    forecast_f: Optional[float] = None


class PaperBook:
    """Simulates fills against live prod prices. Keeps full audit trail."""

    def __init__(self, state_path: Optional[Path] = None):
        if state_path:
            self.state_path = state_path
        else:
            base = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"))
            self.state_path = base / "paper-book.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.state_path.exists():
            self.cash_cents = STARTING_BANK_CENTS
            self.open: list[dict] = []
            self.closed: list[dict] = []
            self.filled_orders: list[dict] = []
            self.pending_makers: list[dict] = []
            self.starting_bank_cents = STARTING_BANK_CENTS
            self._save()
            return
        # Fail-CLOSED on corruption (audit 2026-07-06): a corrupt paper-book must NOT
        # silently reset to a fresh full-cash book and persist it — that wipes the open
        # positions and the calibration-source book (BSS rests on state/paper). Back up
        # the corrupt bytes + raise instead of clobbering.
        from trader.risk import _load_json_state_or_raise
        raw = _load_json_state_or_raise(self.state_path, "paper_book")
        self.cash_cents = int(raw.get("cash_cents", STARTING_BANK_CENTS))
        self.starting_bank_cents = int(raw.get("starting_bank_cents", STARTING_BANK_CENTS))
        self.open = list(raw.get("open", []))
        self.closed = list(raw.get("closed", []))
        self.filled_orders = list(raw.get("filled_orders", []))
        self.pending_makers = list(raw.get("pending_makers", []))

    def _save(self) -> None:
        payload = {
            "version": 1,
            "starting_bank_cents": self.starting_bank_cents,
            "cash_cents": self.cash_cents,
            "open": self.open,
            "closed": self.closed,
            "filled_orders": self.filled_orders,
            "pending_makers": self.pending_makers,
            "updated_utc": _now_iso(),
        }
        tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")  # QA-19: per-process tmp (no cross-writer collision)
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.state_path)

    # ── maker lifecycle log ─────────────────────────────────────────────
    # Append-only ledger of every maker quote's fate (posted/replaced/filled/
    # expired), sibling of paper-book.json. Lets bin/premium_fill_report.py
    # compute the FILL RATE (filled ÷ posted) — the execution metric the
    # settlement log alone can't give (it only sees fills, never the misses).

    def _lifecycle_path(self) -> Path:
        return self.state_path.parent / "maker-lifecycle.jsonl"

    def _log_lifecycle(self, event: str, rec: dict) -> None:
        try:
            row = {
                "event": event,
                "ts": _now_iso(),
                "ticker": rec.get("ticker"),
                "side": rec.get("side"),
                "limit_price_cents": rec.get("limit_price_cents"),
                "qty": rec.get("qty"),
                "paper_order_id": rec.get("paper_order_id"),
                # the model YES fair_prob this quote was posted at — the drift basis the
                # quote-staleness refresh keys on (None for old rows → "can't refresh, leave resting").
                "fair_prob_at_post": rec.get("fair_prob_at_post"),
            }
            # P0b (2026-07-04): the canonical column set above is a stable schema (fixed columns +
            # None defaults so downstream .get() parsers always find them), but it USED to be the
            # whole row — every other key the caller passed was silently dropped at write time. That
            # discarded the posted_live book-context fields (spread_cents_at_post, {yes,no}_{bid,ask}_
            # at_post, side_bid_qty_at_post) the aggr/spread/depth fill model and markout_kill_test's
            # captured-half-spread check depend on (also city/order_id/remaining_count on halt/refresh
            # rows). Merge through any additional rec keys — append-only and behavior-neutral (all
            # readers use .get()). setdefault keeps the canonical keys above (incl. event/ts)
            # authoritative: a colliding rec key never overwrites them.
            for k, v in rec.items():
                row.setdefault(k, v)
            with open(self._lifecycle_path(), "a") as f:
                # TypeError/ValueError guard the merged rec values: telemetry logging must never
                # crash the trade loop on a stray non-serializable value (current callers pass only
                # JSON scalars, so this is a safety net, not an expected path).
                f.write(json.dumps(row) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    # ── core ops ───────────────────────────────────────────────────────

    def can_afford(self, qty: int, price_cents: int) -> tuple[bool, int]:
        from trader.orders import _kalshi_taker_fee_cents
        fee = _kalshi_taker_fee_cents(qty, price_cents)
        cost = qty * price_cents + fee
        return self.cash_cents >= cost, cost

    def fill_taker(
        self,
        ticker: str,
        side: str,
        ask_price_cents: int,
        qty: int,
        fair_prob: float,
        edge_cents_post_fee: float,
        confidence: str,
        market_close_time: str,
        rationale: str = "",
        brier_record_id: Optional[str] = None,
        forecast_f: Optional[float] = None,
    ) -> dict:
        """
        Simulate a taker fill at the LIVE prod ask. Caller must have already
        verified ask_price_cents > 0 against a live market snapshot.

        Position/size caps are NOT enforced here BY DESIGN — they live in
        RiskGate (trader/risk.py): max_open_positions (added 2026-06-19,
        counts open + resting makers) and per_event_max_position_dollars
        (bankroll-scaled; live sets $5). The "KNOWN GAP (2026-06-01)" note
        that stood here was resolved by that 06-19 work — see
        RISK-GAP-NOTES.md (2026-07-12 status addendum). This method stays
        limit-agnostic so the book can't double-enforce what the gate decides.
        """
        if ask_price_cents <= 0 or ask_price_cents >= 100:
            return {"status": "rejected", "reason": "invalid_ask", "ask": ask_price_cents}
        if qty <= 0:
            return {"status": "rejected", "reason": "invalid_qty"}

        ok, cost_total = self.can_afford(qty, ask_price_cents)
        if not ok:
            return {
                "status": "rejected",
                "reason": "insufficient_paper_cash",
                "cash_cents": self.cash_cents,
                "needed_cents": cost_total,
            }

        from trader.orders import _kalshi_taker_fee_cents
        fee_total = _kalshi_taker_fee_cents(qty, ask_price_cents)
        paper_id = f"paper-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        now = _now_iso()

        fo = FilledOrder(
            ticker=ticker, side=side, price_cents=ask_price_cents, qty=qty,
            fee_cents_total=fee_total, cost_cents_total=cost_total,
            fair_prob=fair_prob, edge_cents_post_fee=edge_cents_post_fee,
            confidence=confidence, mode="taker",
            market_close_time=market_close_time, filled_at_utc=now,
            paper_order_id=paper_id, rationale=rationale,
            brier_logged=False,
        )
        pos = Position(
            ticker=ticker, side=side, qty=qty, avg_entry_cents=ask_price_cents,
            fee_cents_total=fee_total, cost_cents_total=cost_total,
            fair_prob_at_open=fair_prob, opened_utc=now,
            market_close_time=market_close_time, paper_order_id=paper_id,
            rationale=rationale, brier_record_id=brier_record_id,
            brier_logged=False, forecast_f=forecast_f,
        )

        self.cash_cents -= cost_total
        self.filled_orders.append(asdict(fo))
        self.open.append(asdict(pos))
        self._save()
        return {
            "status": "filled",
            "paper_order_id": paper_id,
            "cost_cents_total": cost_total,
            "fee_cents_total": fee_total,
            "cash_remaining_cents": self.cash_cents,
        }

    def post_maker(
        self,
        ticker: str,
        side: str,
        limit_price_cents: int,
        qty: int,
        fair_prob: float,
        edge_cents_post_fee: float,
        confidence: str,
        market_close_time: str,
        rationale: str = "",
        brier_record_id: Optional[str] = None,
        forecast_f: Optional[float] = None,
    ) -> dict:
        """Queue a maker limit order. Fill resolution happens on next sweep
        when we see live bid/ask cross the limit.

        Dedupe: if an existing pending maker on the same (ticker, side) is at
        the same price and qty, skip. If the price moved, replace it.

        Position/size caps are NOT enforced here BY DESIGN — they live in
        RiskGate (trader/risk.py): max_open_positions (added 2026-06-19,
        counts open + resting makers) and per_event_max_position_dollars
        (bankroll-scaled; live sets $5). The "KNOWN GAP (2026-06-01)" note
        that stood here was resolved by that 06-19 work — see
        RISK-GAP-NOTES.md (2026-07-12 status addendum). This method stays
        limit-agnostic so the book can't double-enforce what the gate decides.
        """
        replaced = False
        kept = []
        for pm in self.pending_makers:
            if pm["ticker"] == ticker and pm["side"] == side:
                if pm["limit_price_cents"] == limit_price_cents and pm["qty"] == qty:
                    # Exact duplicate — don't post a new one.
                    return {"status": "skipped_duplicate", "paper_order_id": pm["paper_order_id"]}
                # Same ticker+side, different price/qty → replace with newer quote.
                replaced = True
                self._log_lifecycle("replaced", pm)
                continue
            kept.append(pm)
        if replaced:
            self.pending_makers = kept
        paper_id = f"paper-maker-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        pm = PendingMaker(
            ticker=ticker, side=side, limit_price_cents=limit_price_cents, qty=qty,
            fair_prob=fair_prob, edge_cents_post_fee=edge_cents_post_fee,
            confidence=confidence, market_close_time=market_close_time,
            posted_utc=_now_iso(), paper_order_id=paper_id, rationale=rationale,
            brier_logged=False, forecast_f=forecast_f,
        )
        d = asdict(pm)
        if brier_record_id is not None:
            d["brier_record_id"] = brier_record_id
        self.pending_makers.append(d)
        self._save()
        self._log_lifecycle("posted", d)
        return {"status": "posted", "paper_order_id": paper_id}

    def sweep_pending_makers(self, market_snapshot: dict) -> list[dict]:
        """For each pending maker, check the live market snapshot.
        Fill rule: a YES limit at price P fills only if the live YES bid
        crosses up to P AND there is actual bid depth (volume gate).
        For a NO limit at price P, the live NO bid must cross up with depth.
        
        Volume gate: requires `yes_bid_qty` (or `no_bid_qty`) >= min_qty
        (configurable via KALSHI_WEATHER_MIN_BID_QTY, default 1).
        This prevents phantom fills from empty-book flickers on low-volume markets.

        2026-06-19 P&L work — fill realism (KALSHI_WEATHER_FILL_REALISM=1, default
        ON): the old rule let 1 lot of bid depth fill an entire 30-lot maker, with
        no adverse selection — the main reason paper (+26%) overstated live (−8%).
        Realism adds: (a) the bid must cross THROUGH the limit by
        KALSHI_WEATHER_ADVERSE_MARGIN_CENTS (default 1¢), not merely touch it, and
        (b) bid depth must be ≥ KALSHI_WEATHER_FILL_DEPTH_FRAC (default 0.5) of the
        order size before it fills. Set FILL_REALISM=0 to restore the optimistic
        data-collection behaviour. Returns list of fill events."""
        min_bid_qty = int(os.environ.get("KALSHI_WEATHER_MIN_BID_QTY", "1"))
        _realism = os.environ.get("KALSHI_WEATHER_FILL_REALISM", "1") == "1"
        _adverse_margin = int(os.environ.get("KALSHI_WEATHER_ADVERSE_MARGIN_CENTS", "1")) if _realism else 0
        _depth_frac = float(os.environ.get("KALSHI_WEATHER_FILL_DEPTH_FRAC", "0.5")) if _realism else 0.0

        def _fills_now(live_bid: int, bid_qty: int, pm: dict) -> bool:
            need_price = pm["limit_price_cents"] + _adverse_margin
            need_depth = max(min_bid_qty, int(pm.get("qty", 1) * _depth_frac))
            return live_bid >= need_price and bid_qty >= need_depth

        fills = []
        remaining = []
        for pm in self.pending_makers:
            tk = pm["ticker"]
            snap = market_snapshot.get(tk)
            if not snap:
                remaining.append(pm)
                continue
            close_time = snap.get("close_time", "") or pm.get("market_close_time", "")
            # Auto-cancel pending makers in markets that have closed
            try:
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                if ct <= datetime.now(timezone.utc):
                    fills.append({"status": "cancelled_market_closed", "paper_order_id": pm["paper_order_id"]})
                    self._log_lifecycle("expired", pm)
                    continue
            except Exception:
                pass

            if pm["side"] == "yes":
                live_bid = int(snap.get("yes_bid", 0) or 0)
                bid_qty = int(snap.get("yes_bid_qty", 0) or 0)
                if _fills_now(live_bid, bid_qty, pm):
                    result = self._convert_maker_to_fill(pm)
                    fills.append(result)
                    if result.get("status") == "rejected_no_cash_requeued":
                        remaining.append(pm)
                    elif result.get("status") == "filled_maker":
                        self._log_lifecycle("filled", pm)
                    elif result.get("status") == "rejected_double_exposure":
                        # Dropped because a position in this ticker already exists.
                        # Not a fill and not a clean expiry — log so the funnel balances.
                        self._log_lifecycle("cancelled_dup", pm)
                    continue
            else:  # no
                live_bid = int(snap.get("no_bid", 0) or 0)
                bid_qty = int(snap.get("no_bid_qty", 0) or 0)
                if _fills_now(live_bid, bid_qty, pm):
                    result = self._convert_maker_to_fill(pm)
                    fills.append(result)
                    if result.get("status") == "rejected_no_cash_requeued":
                        remaining.append(pm)
                    elif result.get("status") == "filled_maker":
                        self._log_lifecycle("filled", pm)
                    elif result.get("status") == "rejected_double_exposure":
                        # Dropped because a position in this ticker already exists.
                        # Not a fill and not a clean expiry — log so the funnel balances.
                        self._log_lifecycle("cancelled_dup", pm)
                    continue
            remaining.append(pm)
        self.pending_makers = remaining
        self._save()
        return fills

    def _convert_maker_to_fill(self, pm: dict) -> dict:
        """Convert a pending maker into a filled position. Maker fee is
        ~1/4 of taker. We use the same formula but scaled by 0.25.
        
        Guard: reject fill if an open position already exists for the same
        ticker, preventing double exposure from stale maker quotes."""
        existing_tickers = {p["ticker"] for p in self.open}
        if pm["ticker"] in existing_tickers:
            return {"status": "rejected_double_exposure", "paper_order_id": pm["paper_order_id"],
                    "ticker": pm["ticker"], "reason": "position_already_open"}
        
        from trader.orders import _kalshi_maker_rebate_cents
        # A maker rebate is a CREDIT, not a fee: it reduces cost. Store it as a
        # negative fee to match orders.fee_per_contract_cents(maker=True). The old
        # code ADDED the rebate to cost, overcharging maker fills by ~2x the rebate
        # and biasing the maker-vs-taker A/B. (Slightly raises paper maker P&L;
        # revert by flipping the sign back if Kalshi does not rebate this series.)
        rebate_total = _kalshi_maker_rebate_cents(pm["qty"], pm["limit_price_cents"])

        # ── Fill realism (2026-06-29 audit) ──────────────────────────────────────
        # The sim used to fill the WHOLE lot at the EXACT limit with only the rebate
        # credit — zero slippage, zero adverse selection — so paper overstated realized
        # edge ~5x (paper-premium +16.3¢/ct vs live-premium +3.0¢/ct; clean matched-market
        # paired gap +6.85¢/ct [+3.16,+11.67]). The maker who actually gets filled (a)
        # rarely gets the exact limit (live entry-vs-quote slippage ≈ +0.84¢) and (b) is
        # adversely selected — the book moves against the fill (live post-fill markout
        # ≈ −5.6¢). Charge both as an explicit per-fill debit so paper MEASURES like live.
        # Combined ≈ 6.4¢/ct closes the matched gap. Folded into fee_total so the
        # cost = qty*price + fee invariant holds; recorded entry price stays the true limit.
        # Gated by FILL_REALISM (default on); zero the knobs or set FILL_REALISM=0 to revert
        # to the optimistic data-collection behaviour. Live arm is unaffected (its fills are
        # synced from Kalshi, never routed through this method — filled_orders stays 0).
        realism_charge = 0
        if os.environ.get("KALSHI_WEATHER_FILL_REALISM", "1") == "1":
            _slip = float(os.environ.get("KALSHI_WEATHER_FILL_SLIPPAGE_CENTS", "0.84"))
            _adverse = float(os.environ.get("KALSHI_WEATHER_FILL_ADVERSE_MARKOUT_CENTS", "5.6"))
            realism_charge = max(0, round((_slip + _adverse) * pm["qty"]))
        fee_total = -rebate_total + realism_charge  # net per-fill: rebate credit + realism drag
        cost_total = pm["qty"] * pm["limit_price_cents"] + fee_total

        if cost_total > self.cash_cents:
            # Re-queue the maker instead of silently dropping it on cash shortage
            import sys
            print(f"[paper_book] ⚠️ maker {pm['paper_order_id']} ({pm['ticker']} {pm['side']}) cash shortage: need {cost_total}¢ have {self.cash_cents}¢ — requeueing",
                  file=sys.stderr)
            return {"status": "rejected_no_cash_requeued", "paper_order_id": pm["paper_order_id"]}

        now = _now_iso()
        brier_record_id = pm.get("brier_record_id")
        brier_logged = pm.get("brier_logged", True)
        fo = FilledOrder(
            ticker=pm["ticker"], side=pm["side"], price_cents=pm["limit_price_cents"],
            qty=pm["qty"], fee_cents_total=fee_total, cost_cents_total=cost_total,
            fair_prob=pm["fair_prob"], edge_cents_post_fee=pm["edge_cents_post_fee"],
            confidence=pm["confidence"], mode="maker",
            market_close_time=pm["market_close_time"], filled_at_utc=now,
            paper_order_id=pm["paper_order_id"], rationale=pm.get("rationale", ""),
            brier_logged=brier_logged,
        )
        pos = Position(
            ticker=pm["ticker"], side=pm["side"], qty=pm["qty"],
            avg_entry_cents=pm["limit_price_cents"], fee_cents_total=fee_total,
            cost_cents_total=cost_total, fair_prob_at_open=pm["fair_prob"],
            opened_utc=now, market_close_time=pm["market_close_time"],
            paper_order_id=pm["paper_order_id"], rationale=pm.get("rationale", ""),
            brier_record_id=brier_record_id,
            brier_logged=brier_logged,
            forecast_f=pm.get("forecast_f"),
        )
        self.cash_cents -= cost_total
        self.filled_orders.append(asdict(fo))
        self.open.append(asdict(pos))
        return {
            "status": "filled_maker",
            "paper_order_id": pm["paper_order_id"],
            "cost_cents_total": cost_total,
            "realism_charge_cents": realism_charge,
            "ticker": pm["ticker"],
            "side": pm["side"],
            "qty": pm["qty"],
            "price_cents": pm["limit_price_cents"],
            "fair_prob": pm["fair_prob"],
            "mode": "maker",
        }

    def settle_position(self, ticker: str, settlement_result: str) -> list[dict]:
        """When a market settles, mark all open positions in it. settlement_result is 'yes', 'no',
        or 'void'. A void (Kalshi cancels the market, e.g. NWS settlement data unavailable) refunds
        the stake: payout == cost so pnl is exactly 0."""
        out = []
        still_open = []
        for pos in self.open:
            if pos["ticker"] != ticker:
                still_open.append(pos)
                continue
            if settlement_result == "void":
                # Cancelled market → refund the full cost (fees included in cost_cents_total), pnl 0.
                payout = pos["cost_cents_total"]
                exit_cents = pos["avg_entry_cents"]      # nominal for the record; pnl is 0 by construction
                pnl = 0
            else:
                exit_cents = 100 if pos["side"] == settlement_result else 0
                payout = exit_cents * pos["qty"]
                # pnl = payout - entry_cost (fees already inside cost_cents_total)
                pnl = payout - pos["cost_cents_total"]
            self.cash_cents += payout
            ct = ClosedTrade(
                ticker=ticker, side=pos["side"], qty=pos["qty"],
                entry_cents=pos["avg_entry_cents"], exit_cents=exit_cents,
                fee_cents_total=pos["fee_cents_total"], pnl_cents=pnl,
                settlement_result=settlement_result, opened_utc=pos["opened_utc"],
                closed_utc=_now_iso(), paper_order_id=pos["paper_order_id"],
            )
            self.closed.append(asdict(ct))
            out.append({"ticker": ticker, "pnl_cents": pnl, "side": pos["side"], "qty": pos["qty"], "paper_order_id": pos["paper_order_id"]})
        self.open = still_open
        self._save()
        return out

    # ── reporting ──────────────────────────────────────────────────────

    def equity_cents(self, market_snapshot: Optional[dict] = None) -> int:
        """Cash + mark-to-market of open positions using last live mid price.
        Falls back to entry price if no snapshot provided."""
        mtm = 0
        for pos in self.open:
            snap = (market_snapshot or {}).get(pos["ticker"]) or {}
            yb = int(snap.get("yes_bid", 0) or 0)
            ya = int(snap.get("yes_ask", 0) or 0)
            if yb or ya:
                # Live yes-book present: yes-mid (or the single quoted side), flipped into the
                # position's own side-space for a NO position (NO price = 100 - yes price).
                mid = (yb + ya) // 2 if (yb and ya) else (yb or ya)
                if pos["side"] == "no":
                    mid = 100 - mid
            else:
                # QA-04 (2026-07-01): empty/absent book — fall back to the position's own-side
                # entry, which is ALREADY stored in side-space (a NO entry is a NO price). Do NOT
                # apply the 100-mid flip here, or a NO position gets a phantom ~+(100-2*entry)¢/ct
                # unrealized mark (e.g. bought NO @30 was marked 70).
                mid = pos["avg_entry_cents"]
            mtm += mid * pos["qty"]
        return self.cash_cents + mtm

    def summary(self, market_snapshot: Optional[dict] = None) -> dict:
        realized = sum(t["pnl_cents"] for t in self.closed)
        eq = self.equity_cents(market_snapshot)
        return {
            "starting_bank_dollars": self.starting_bank_cents / 100,
            "cash_dollars": self.cash_cents / 100,
            "equity_dollars": eq / 100,
            "net_pnl_dollars": (eq - self.starting_bank_cents) / 100,
            "realized_pnl_dollars": realized / 100,
            "open_positions": len(self.open),
            "pending_makers": len(self.pending_makers),
            "trades_closed": len(self.closed),
            "fills_total": len(self.filled_orders),
        }

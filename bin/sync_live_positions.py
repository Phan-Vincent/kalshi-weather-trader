#!/usr/bin/env python3
"""
bin/sync_live_positions.py — Pull Kalshi's actual position data and sync the live book.

Kalshi represents positions via:
  position_fp: float      — order/position size (+YES, -NO)
  total_traded_dollars    — capital reserved/committed  
  market_exposure_dollars — capital at risk
  realized_pnl_dollars    — settled P&L (non-zero after contract closes)
  resting_orders_count    — sometimes cleared by Kalshi for maker orders

The fills endpoint tells the real story: count > 0 = actual contract traded.
This script reads Kalshi's actual state and updates the live paper book to match.

Usage:
  python3 bin/sync_live_positions.py
  python3 bin/sync_live_positions.py --report    # print comparison, don't write
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _build_live_positions(positions: list, events: list) -> tuple[list, int]:
    """Build per-MARKET live position records from Kalshi's raw market/event positions.

    Pure (no I/O) so the two field-accuracy fixes below are unit-testable (2026-06-25 QA audit):

    • entry cost is the PER-MARKET total_traded_dollars, NOT the event-level total_cost_dollars.
      Kalshi's event total_cost is SHARED across every strike of an event, so feeding it to the
      per-contract entry (cost/qty in sync_live_book) yields impossible >100¢ entries on
      multi-strike events (e.g. KXHIGHTNOLA-…-B91.5 showed 194¢). The event total still rolls up
      into the informational deployed-capital number only.

    • qty uses round(), not int() truncation. A genuine fractional holding (0<|fp|<1) truncated to
      qty=0 would be dropped from the open book entirely; round + a floor of 1 keeps it open. fp=0
      (a settled position Kalshi still reports for ~one cycle) stays qty=0 so sync_live_book's
      settlement detection (qty<=0) is preserved — only |fp|>=0.01 holdings are floored up.
    """
    live_positions: list = []
    total_deployed_cents = 0
    for p in positions:
        fp = float(p.get("position_fp", 0))
        if abs(fp) < 0.01 and float(p.get("total_traded_dollars", 0)) < 0.01:
            continue  # empty position

        ticker = p["ticker"]
        side = "yes" if fp > 0 else "no"
        qty = abs(round(fp))
        if qty == 0 and abs(fp) >= 0.01:
            qty = 1  # genuine sub-1 holding → keep it open, don't truncate it away (bug 2)

        # Per-MARKET traded cost (this strike only) drives the per-contract entry (bug 1).
        mkt_cost_cents = round(float(p.get("total_traded_dollars", 0)) * 100)

        # Event-level cost feeds ONLY the informational deployed-capital rollup printed by main().
        evt = next((e for e in events if e["event_ticker"] in ticker or ticker.startswith(e["event_ticker"])), {})
        evt_cost = round(float(evt.get("total_cost_dollars", 0) or p.get("total_traded_dollars", 0)) * 100)

        total_deployed_cents += evt_cost
        live_positions.append({
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "position_fp": fp,
            "total_cost_cents": mkt_cost_cents,
            "realized_pnl_cents": round(float(p.get("realized_pnl_dollars", 0)) * 100),
            "market_exposure_cents": round(float(p.get("market_exposure_dollars", 0)) * 100),
            "fees_paid_cents": round(float(p.get("fees_paid_dollars", 0)) * 100),
            "resting_orders": int(p.get("resting_orders_count", 0)),
            "last_updated": p.get("last_updated_ts", ""),
        })
    return live_positions, total_deployed_cents


def get_kalshi_positions() -> dict:
    """Fetch Kalshi prod account state via CLI."""
    result = subprocess.run(
        ["kalshi-cli", "--prod", "portfolio", "positions", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"kalshi-cli failed: {result.stderr}", file=sys.stderr)
        return {}

    balance_result = subprocess.run(
        ["kalshi-cli", "--prod", "portfolio", "balance", "--json"],
        capture_output=True, text=True, timeout=30,
    )

    data = json.loads(result.stdout)
    # QA-03 (2026-07-01): the balance call is independent of the positions call. If it fails or
    # returns null fields, do NOT silently coerce cash/portfolio to 0 — that zeroes the live book's
    # cash (→ bankroll floors → city risk-multiplier collapses sizing) and the sync-state total, and
    # int(None) on a null field crashes the whole sync. Parse defensively and flag balance_ok so the
    # caller carries the previous cash forward instead of writing zeros.
    balance_ok = balance_result.returncode == 0
    bal = {}
    if balance_ok:
        try:
            bal = json.loads(balance_result.stdout)
        except (json.JSONDecodeError, ValueError):
            balance_ok = False
    if bal.get("balance") is None or bal.get("portfolio_value") is None:
        balance_ok = False
    if not balance_ok:
        print("kalshi-cli balance unavailable — caller will carry forward previous cash",
              file=sys.stderr)

    positions = data.get("market_positions", [])
    events = data.get("event_positions", [])

    # Build position map by event ticker
    event_map = {}
    for e in events:
        event_map[e["event_ticker"]] = {
            "event_cost_cents": round(float(e.get("total_cost_dollars", 0)) * 100),
            "event_exposure_cents": round(float(e.get("event_exposure_dollars", 0)) * 100),
            "realized_pnl_cents": round(float(e.get("realized_pnl_dollars", 0)) * 100),
        }

    live_positions, total_deployed_cents = _build_live_positions(positions, events)

    available_cents = int(bal.get("balance") or 0)
    portfolio_cents = int(bal.get("portfolio_value") or 0)

    return {
        "available_cents": available_cents,
        "portfolio_cents": portfolio_cents,
        "total_cents": available_cents + portfolio_cents,
        "balance_ok": balance_ok,
        "positions": live_positions,
        "num_positions": len(live_positions),
        "total_deployed_cents": total_deployed_cents,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def get_kalshi_settlements(limit: int = 200) -> dict:
    """Map ticker -> {market_result, revenue_cents, settled_time} from Kalshi's settlement history
    (`kalshi-cli portfolio settlements`). The authoritative settled-OUTCOME + payout source, used to
    reconcile positions that settled and were PURGED by Kalshi before a sync caught them as a qty=0
    ghost (otherwise their realized P&L is silently lost). `revenue` is our payout in CENTS; realized
    P&L = revenue − our cost basis. Fail-soft: returns {} on any CLI/parse error (caller leaves the
    disappeared position uncommitted to retry next cycle). (fill-quality deep-dive, 2026-06-26)"""
    try:
        result = subprocess.run(
            ["kalshi-cli", "--prod", "portfolio", "settlements", "--json", "--limit", str(limit)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"kalshi-cli settlements failed: {result.stderr}", file=sys.stderr)
            return {}
        rows = json.loads(result.stdout).get("settlements", []) or []
    except Exception as e:
        print(f"kalshi-cli settlements error: {e}", file=sys.stderr)
        return {}
    out = {}
    for r in rows:
        tk = r.get("ticker")
        if tk:
            out[tk] = {"market_result": r.get("market_result"),
                       "revenue_cents": int(r.get("revenue", 0) or 0),
                       "settled_time": r.get("settled_time", "")}
    return out


_CARRY_ALERT_CYCLES = int(os.environ.get("KALSHI_WEATHER_CARRY_ALERT_CYCLES", "12"))


def _carry_forward(prev: dict, dest: list) -> None:
    """Carry a settled-but-unconfirmed position forward in the open book so the next sync retries its
    settlement (never drop it → never lose a settlement). Bounded: after N carries, alert ONCE so a
    human can investigate a stuck position (e.g. one flattened on the web that will never settle); keep
    carrying regardless, since dropping risks losing a real settlement. (QA 2026-06-26)"""
    prev["carry_cycles"] = int(prev.get("carry_cycles", 0) or 0) + 1
    if prev["carry_cycles"] >= _CARRY_ALERT_CYCLES and prev["carry_cycles"] % _CARRY_ALERT_CYCLES == 0:
        try:
            from trader.notify import alert
            alert(f"settlement carry-forward stuck: {prev.get('ticker')} unsettled after "
                  f"{prev['carry_cycles']} syncs — verify it settled or was flattened",
                  key=f"carry_stuck:{prev.get('ticker')}")
        except Exception:
            pass
    dest.append(prev)


def _is_weather_ticker(tk: str) -> bool:
    # QA-10 (2026-07-01): anchored match via the shared helper (was a substring test that
    # false-positived non-weather series merely CONTAINING HIGH/LOW/TEMP into the live book).
    from data.weather_data import is_weather_ticker
    return is_weather_ticker(tk)


def weather_budget_total_cents(kalshi_state: dict) -> int:
    """Account total that feeds the WEATHER daily/weekly stop, EXCLUDING non-weather positions'
    market value (e.g. the legacy KXUSAIRANAGREEMENT contract) so an unrelated position's mark
    can't contaminate — or MASK — the weather stop (audit #7, 2026-06-24). Uses per-position
    market_exposure_cents (the value, which changes), not cost (which would cancel in the delta)."""
    non_weather_cents = sum(
        int(p.get("market_exposure_cents", 0) or 0)
        for p in kalshi_state.get("positions", [])
        if not _is_weather_ticker(p.get("ticker", ""))
    )
    return (int(kalshi_state["available_cents"]) + int(kalshi_state.get("portfolio_cents", 0))
            - non_weather_cents)


def weather_ramp_total_cents(realized_pnl_cents: int, kalshi_state: dict) -> int:
    """Weather-only balance-equivalent that baselines the live size-ramp (paper_trade.live_size_rung).

    = lifetime weather REALIZED P&L + current mark of OPEN weather positions. Contains NO account-cash
    term (available/portfolio), so ANY non-weather event — a sports or KXUSAIRANAGREEMENT settlement
    whose payout lands in available_cents, a deposit, a withdrawal, a fee — contributes exactly 0 to the
    ramp's DELTA (the only thing live_size_rung reads). Contrast weather_budget_total_cents, which
    neutralizes non-weather OPEN marks but NOT their realized cash: once such a position SETTLES its
    proceeds sit in available_cents permanently and would otherwise spuriously promote/demote the weather
    ramp (the 2026-07-02 sports +$147 case). Mark-to-market on the open leg preserves the old signal's
    'unrealized swings move the ramp' responsiveness, scoped to weather. (2026-07-05 size-ramp re-key.)"""
    weather_open_value_cents = sum(
        int(p.get("market_exposure_cents", 0) or 0)
        for p in kalshi_state.get("positions", [])
        if _is_weather_ticker(p.get("ticker", ""))
    )
    return int(realized_pnl_cents) + weather_open_value_cents


# At-fill markout snapshot tuning (2026-07-05 QA hardening).
# A newly-observed fill only yields an HONEST "at-fill" mid when we observe it PROMPTLY. A fill first
# seen long after it filled — a sync gap (laptop asleep, missed launchd slots) — would carry a stale
# mid AND an age_hours (now − opened_utc, where opened_utc = Kalshi last_updated_ts = the fill time)
# that lands INSIDE the [2,8]h window every markout consumer keys on, contaminating the strategy's
# kill/continue instruments. So we only snapshot prompt observations (age below the window with
# margin) — which ALSO means a gap's burst of stale-age fills is skipped BEFORE any network fetch,
# bounding the fan-out. Defense in depth: every repo markout reader independently drops
# source="sync_fill_detect" rows, so these diagnostic rows never reach a live instrument regardless.
_AT_FILL_MAX_AGE_H = float(os.environ.get("KALSHI_WEATHER_AT_FILL_MAX_AGE_H", "1.0"))
# Hard cap on orderbook fetches per sync so a Kalshi-endpoint stall coinciding with a multi-fill cycle
# can't stretch the shared .cycle.lock hold into the next trade slot. Prompt fills per cycle are
# normally 0–4; the rest (if any) are picked up by cmd_report next cycle.
_AT_FILL_MAX_FETCHES = int(os.environ.get("KALSHI_WEATHER_AT_FILL_MAX_FETCHES", "12"))
# Overall wall-clock budget across the fetch loop. fetch_orderbook's timeout is a per-socket-op bound,
# NOT a total-request deadline, so a drip-feeding endpoint could exceed it; this caps the TOTAL lock
# extension regardless. Generous default (normal loops finish in well under 1s) so it only trips on a
# pathological stall — long before the ~20min gap to the next trade slot.
_AT_FILL_MAX_SECONDS = float(os.environ.get("KALSHI_WEATHER_AT_FILL_MAX_SECONDS", "30"))


def _age_hours(now, opened_utc):
    """Hours between `now` (aware UTC) and an ISO `opened_utc`, rounded to 2dp, or None if unparseable."""
    try:
        return round((now - datetime.fromisoformat(
            str(opened_utc or "").replace("Z", "+00:00"))).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def _fetch_mid_cents(ticker: str, side: str):
    """Best-effort CURRENT top-of-book mark (cents, side-normalized) for `ticker`, or None on any
    failure. Uses the shared data.kalshi_data.yes_mark_from_book valuation (two-sided → mid; one-sided
    → the honest standing side, NOT the fabricated real_side/2 that sign-flipped near-settled markout;
    NO = 100 − YES mark). Used by the at-fill markout snapshot. It is a read-only network call with a
    TIGHT timeout/no-retry (this is telemetry running inside the sync's .cycle.lock — it must never
    dominate the lock hold), wrapped so ANY error (import, fetch, malformed book) degrades to None —
    a telemetry fetch must never disturb the sync."""
    try:
        from data.kalshi_data import fetch_orderbook, derive_book, yes_mark_from_book
        ob = fetch_orderbook(ticker, timeout=4, retries=1)   # ~4s worst case, not the 20s default
        if not ob or not ob.get("orderbook_fp"):
            return None
        b = derive_book(ob["orderbook_fp"])
        yb, ya = b.get("yes_bid"), b.get("yes_ask")
        yes_mark = yes_mark_from_book(yb, ya)   # one-sided book → honest liquidation side, not real_side/2
        if yes_mark is None:
            return None
        return yes_mark if side == "yes" else 100.0 - yes_mark
    except Exception:
        return None


def _snapshot_new_fills_markout(state_dir, open_positions, previous_open_tickers,
                                had_previous_book, *, mid_fn=_fetch_mid_cents) -> list:
    """At-fill markout telemetry: snapshot the current mid the MOMENT a fill is first observed.

    The markout log (bin/paper_trade.py cmd_report) only snapshots the open book at the 6×/day :20
    trade cycles, so the FIRST post-fill mid lands a median ~1.9h after the fill — which blends the
    spread captured at fill with ~2h of drift and leaves the spread-capture component unmeasurable
    (reviews/yes-bleed-decomposition-2026-07-04.md). This sync runs every ~30min (the :10/:40 launchd
    job + the :20 pre-trade sync), so recording a mid HERE — once, when a ticker first appears in the
    open book AND we observed it promptly — lands the first observation within ~30min of the fill.

    Contract — DIAGNOSTIC, additive, fail-soft telemetry:
      • One extra markout row per newly-observed fill, tagged source="sync_fill_detect", consumed ONLY
        by the offline yes-bleed decomposition. Every repo markout reader (live_fill_quality._mean_
        markout, markout_kill_test, live_paper_gap, pnl_replay, premium_fill_report) hard-excludes this
        source, so it perturbs NO live kill/continue instrument.
      • age_hours is now − opened_utc (Kalshi last_updated_ts = the fill time), i.e. how STALE the
        fill is at first observation. We only write when that is small (`< _AT_FILL_MAX_AGE_H`): a
        gap-delayed observation would carry a stale mid mislabeled "at-fill" AND an age inside the
        [2,8]h consumer window — so it's skipped, which additionally means NO orderbook fetch for it.
      • Fetches are capped per cycle (`_AT_FILL_MAX_FETCHES`) so a stall can't stretch the .cycle.lock.
      • Skipped entirely with no previous book (a cold start can't tell a new fill from a pre-existing
        position). Never raises — a telemetry failure must never block a sync.

    Returns the rows actually persisted (empty if nothing was written or the append failed).
    """
    rows: list = []
    try:
        if not had_previous_book:
            return []                          # cold start: can't distinguish new fills → skip
        now = datetime.now(timezone.utc)
        fetches = 0
        t0 = time.monotonic()
        for p in open_positions:
            tk = p.get("ticker")
            if not tk or tk in previous_open_tickers:
                continue                       # seen last cycle → not a newly-observed fill
            # Age is computed from opened_utc BEFORE any network call: a gap that first-observes a
            # fill hours late (stale mid, age in the consumer window) is skipped here WITHOUT a fetch.
            age_h = _age_hours(now, p.get("opened_utc"))
            if age_h is None or not (-_AT_FILL_MAX_AGE_H <= age_h < _AT_FILL_MAX_AGE_H):
                continue                       # stale/gap-delayed (or bad ts) → not an at-fill snap
            # Hard bounds on the .cycle.lock hold: at most N fetches AND at most _AT_FILL_MAX_SECONDS
            # of wall-clock across them, so a degraded orderbook endpoint can never stretch the lock.
            if fetches >= _AT_FILL_MAX_FETCHES or (time.monotonic() - t0) >= _AT_FILL_MAX_SECONDS:
                print(f"  ⓘ at-fill snapshot bound reached (fetches={fetches}/{_AT_FILL_MAX_FETCHES}, "
                      f"elapsed≈{time.monotonic() - t0:.0f}s) — remaining new fills deferred to cmd_report",
                      file=sys.stderr)
                break
            side = p.get("side", "yes")
            fetches += 1
            cur = mid_fn(tk, side)
            if cur is None:
                continue                       # no usable book this moment → skip (safe)
            rows.append({
                "ts": now.isoformat(), "ticker": tk, "side": side,
                "qty": p.get("qty", 0), "fill_px": p.get("avg_entry_cents", 0),
                "opened_utc": p.get("opened_utc"), "cur_mid": round(cur, 1),
                "age_hours": age_h, "paper_order_id": p.get("paper_order_id"),
                "source": "sync_fill_detect",   # excluded by every repo markout reader (see docstring)
            })
        if rows:
            from data.weather_data import append_jsonl_atomic   # flock-guarded atomic append
            append_jsonl_atomic(Path(state_dir) / "markout-log.jsonl", rows)
    except Exception as e:                      # telemetry must NEVER block or fail a sync
        print(f"  ⚠ at-fill markout snapshot failed (non-fatal): {e}", file=sys.stderr)
        return []                               # nothing reliably persisted → don't report rows
    return rows


def sync_live_book(kalshi_state: dict) -> dict:
    """Update state/live/paper-book.json to match Kalshi's actual state.
    
    2026-06-19 audit fix: Also feeds settled positions into RiskGate so
    the daily/weekly loss circuit-breakers can see real live P&L.
    Previously sync_live_positions.py only synced the book, leaving
    risk-state blind to live losses (today_pnl_cents always read 0).
    """
    state_dir = os.environ.get(
        "KALSHI_WEATHER_STATE_DIR",
        str(ROOT / "state" / "live"),
    )
    book_path = Path(state_dir) / "paper-book.json"

    # ── Scope the live book to WEATHER markets only (default on) ───────────────────────────────
    # The Kalshi position feed is account-wide, so it sweeps in EVERY position the account holds —
    # including non-weather ones (e.g. the legacy KXUSAIRANAGREEMENT contract). Those don't belong
    # in the weather arm's book: they pad open[]/fills_total, surface as the strategy's unrealized
    # P&L on the dashboard (positions-live.json + the markout log both derive from book.open), and a
    # legacy one sitting in the old book gets misread as a "disappeared" settlement. Exclude them
    # here so the book reflects ONLY weather trades. The daily/weekly $ kill-switch already excludes
    # them independently (weather_budget_total_cents subtracts non-weather exposure), so the stop is
    # unaffected. Escape hatch: KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS=1 restores the all-positions book.
    weather_only = os.environ.get("KALSHI_WEATHER_LIVE_BOOK_ALL_TICKERS", "") != "1"
    def _in_book(tk: str) -> bool:
        return (not weather_only) or _is_weather_ticker(tk)

    # ── Load previous book to detect settlements + PRESERVE settled trade history ──
    previous_open_tickers: dict[str, dict] = {}
    previous_closed: list = []
    previous_cash = None   # QA-03: last-known-good cash, carried forward if the balance feed fails
    had_previous_book = book_path.exists()   # cold-start guard for the at-fill markout snapshot below
    if had_previous_book:
        try:
            with open(book_path) as f:
                old_book = json.load(f)
            previous_cash = old_book.get("cash_cents")
            for pos in old_book.get("open", []):
                if _in_book(pos["ticker"]):
                    previous_open_tickers[pos["ticker"]] = pos
            # Settled-but-pending positions (carried forward awaiting settlement confirmation) live in
            # their OWN field, NOT open[], so they don't inflate equity / the open-position cap. They
            # must stay re-detectable, so merge them into previous_open_tickers for this cycle. (QA 6-26)
            for pos in old_book.get("pending_settlement", []):
                if _in_book(pos["ticker"]):
                    previous_open_tickers.setdefault(pos["ticker"], pos)
            # Carry closed trades forward. sync_live_book itself records settled live positions
            # into closed[] + settlement-log.jsonl below (settle_paper SKIPS synced positions);
            # sync used to hard-reset closed=[] every cycle, so the live arm's realized P&L was
            # never retained (closed always 0 → watchdog/Stage-2 gate had no realized signal).
            previous_closed = [c for c in (old_book.get("closed", []) or []) if _in_book(c.get("ticker", ""))]
        except (OSError, json.JSONDecodeError):
            pass

    # ── Build new book from Kalshi state ──
    cash = kalshi_state["available_cents"]
    balance_ok = kalshi_state.get("balance_ok", True)
    if not balance_ok and previous_cash is not None:
        # QA-03: balance feed failed — keep last-known-good cash rather than writing $0 (which would
        # floor bankroll and misreport a phantom loss). Positions are still synced fresh below.
        cash = previous_cash
        try:
            from trader.notify import alert
            alert(f"live sync: balance feed unavailable — carried forward previous cash "
                  f"${previous_cash/100:.2f} (book cash/sync-total not refreshed this cycle)",
                  key="live_balance_feed")
        except Exception as _e:
            print(f"[sync] balance-feed alert dispatch failed: {_e}", file=sys.stderr)
    # Read the private starting-bank state; fallback is a synthetic public example.
    starting_bank = 100000
    try:
        _li = json.load(open(ROOT / "state" / "live-initial.json"))
        starting_bank = int(_li.get("initial_balance_cents", starting_bank))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass

    # Capture per-trade realized P&L for the live arm. Kalshi keeps a SETTLED position for ~one
    # cycle reporting qty=0 + realized_pnl_cents (its net settled P&L) before clearing it. Record
    # those to settlement-log.jsonl + closed[] (deduped by ticker — date-specific, so unique),
    # using the previous cycle's open snapshot for the original side/qty/entry. This is the live
    # analogue of settle_paper's settlement-log, driven by Kalshi's own realized number, so it
    # neither crashes on the synced schema nor double-counts the risk budget (2026-06-25 fix).
    already_settled = {c.get("ticker") for c in previous_closed}
    newly_settled: list = []
    gap_settled: list = []   # qty<=0 + realized but NO prior snapshot: feed the $-stop, but no log record
    open_positions = []
    pending_settlement: list = []   # settled-but-pending (carried forward for retry; kept OUT of open[])
    ghost_prevs: list = []          # prevs of qty=0 live_sync settlements — carried if the feed fails
    # WEATHER-ONLY: drop non-weather account positions from the book lifecycle (open/settle/disappear).
    book_positions = [p for p in kalshi_state["positions"] if _in_book(p.get("ticker", ""))]
    for pos in book_positions:
        rpnl = int(pos.get("realized_pnl_cents", 0) or 0)
        if pos["qty"] <= 0:                       # no open contracts left → settled (or empty ghost)
            prev = previous_open_tickers.get(pos["ticker"], {})
            prev_qty = int(prev.get("qty", 0) or 0)
            # A settled position WITH a prior open snapshot (qty>0) → full per-trade record (log +
            # closed[] + $-stop). A gap-settled one (NO snapshot: filled+settled within a sync gap, or
            # Kalshi's qty→0 one cycle before realized) has unknown side/qty, so a per-trade record
            # would be malformed (crashes the watchdog / mis-scores the gate) — but its realized $ MUST
            # still reach the kill-switch. So record a closed[]-only marker, fed to the $-stop but NOT
            # written to the settlement-log. (audit 2026-06-25)
            if rpnl != 0 and pos["ticker"] not in already_settled:
                if prev_qty > 0:
                    side = prev.get("side")
                    newly_settled.append({
                        "ticker": pos["ticker"],
                        "side": side,
                        "qty": prev_qty,
                        "pnl_cents": rpnl,            # Kalshi's net realized P&L (incl. fees)
                        "settlement_result": (side if rpnl > 0 else ("no" if side == "yes" else "yes")) if side else None,
                        "entry_cents": prev.get("entry_price_cents") or prev.get("avg_entry_cents"),
                        "opened_utc": prev.get("opened_utc", ""),
                        "market_close_time": "",     # live arm has no market close_time; keep field, empty
                        "settled_at_utc": kalshi_state["timestamp_utc"],
                        "settled_at": kalshi_state["timestamp_utc"],
                        "synced_from_kalshi": True,
                        "source": "live_sync",
                    })
                    ghost_prevs.append(prev)         # so a feed failure can carry it forward for retry
                else:
                    # gap-settled: a fill that OPENED and SETTLED inside one sync gap (no prior open
                    # snapshot), so side/qty/entry are unknown — but Kalshi's realized P&L is authoritative.
                    # Record it (clearly flagged, qty=0/entry=None so the band + per-contract analyses
                    # defensively skip it) so the settlement-log is COMPLETE for total-P&L/win-rate and the
                    # live sample isn't silently missing intraday-resolved fills (validation 2026-06-28).
                    gap_settled.append({
                        "ticker": pos["ticker"], "side": None, "qty": 0,
                        "pnl_cents": rpnl, "settlement_result": None, "entry_cents": None,
                        "opened_utc": "", "market_close_time": "",
                        "settled_at_utc": kalshi_state["timestamp_utc"], "settled_at": kalshi_state["timestamp_utc"],
                        "gap_settled": True, "synced_from_kalshi": True, "source": "live_gap",
                    })
            elif rpnl == 0 and prev_qty > 0 and pos["ticker"] not in already_settled:
                # qty=0 ghost whose realized P&L Kalshi hasn't reported yet (it shows qty=0 + rpnl=0 for
                # >=1 cycle before the real number, then may purge). Carry the prior snapshot forward so
                # it stays detectable — the qty=0 loop (real rpnl) or the disappeared-reconcile (purged)
                # settles it later; dropping it here would lose the settlement. (QA 2026-06-26)
                _carry_forward(prev, pending_settlement)
            continue                              # settled/empty → not an open position
        # total_cost_cents is now the PER-MARKET cost (bug 1), so cost/qty is a real per-contract
        # entry; clamp [0,100] defensively so a malformed cost can never re-introduce a >100¢ entry.
        entry = max(0, min(100, round(pos["total_cost_cents"] / pos["qty"]))) if pos["qty"] > 0 else 0
        open_positions.append({
            "ticker": pos["ticker"],
            "side": pos["side"],
            "qty": pos["qty"],
            "entry_price_cents": entry,
            "avg_entry_cents": entry,
            "cost_cents": pos["total_cost_cents"],
            "pnl_cents": pos["realized_pnl_cents"],
            "fair_prob": 0.5,
            "mode": "live",
            "order_id": pos["last_updated"],
            "opened_utc": pos["last_updated"],
            "fees_paid_cents": int(pos.get("fees_paid_cents", 0) or 0),   # for net-of-fees reconcile P&L
            "synced_from_kalshi": True,
        })

    # ── Reconcile DISAPPEARED settled positions (Kalshi purged them before a sync saw qty=0) ──
    # The loop above only catches a settlement Kalshi still reports as a qty=0 ghost. A position
    # Kalshi PURGED before this sync ran is absent from kalshi_state["positions"] entirely → its
    # realized P&L would be silently lost (closed[] never grows, the $ stop never sees the loss — the
    # fill-quality deep-dive found 0 settlements captured this way). Detect each ticker open last
    # cycle but gone now, confirm it settled via Kalshi's settlement feed, and record realized
    # P&L = settlement revenue − our cost basis. Flows through the same feed/closed[]/log path below;
    # a settled ghost the qty=0 loop already caught is excluded by `seen`. (deep-dive 2026-06-26)
    current_tickers = {p["ticker"] for p in book_positions}
    seen = already_settled | {r["ticker"] for r in newly_settled} | {r["ticker"] for r in gap_settled}
    disappeared = [prev for tk, prev in previous_open_tickers.items()
                   if tk not in current_tickers and tk not in seen]
    reconciled_prevs: list = []   # disappeared+settled prevs — carried forward if the feed fails (retry)
    if disappeared:
        settlements = get_kalshi_settlements()
        for prev in disappeared:
            tk = prev["ticker"]
            s = settlements.get(tk)
            if not s or s.get("market_result") in (None, ""):
                # Gone but not yet confirmed settled (settlement-feed lag, or a CLI failure returning
                # {}). Do NOT drop it — carry it forward so the next sync retries; else a transient
                # feed failure would PERMANENTLY lose the settlement (fail-OPEN). premium-live never
                # sells, so a disappeared position is a settlement-in-progress, not an exit.
                _carry_forward(prev, pending_settlement)
                continue
            qty = int(prev.get("qty", 0) or 0)
            side = prev.get("side")
            entry = int(prev.get("entry_price_cents") or prev.get("avg_entry_cents") or 0)
            cost = int(prev.get("cost_cents") or (entry * qty))
            fees = int(prev.get("fees_paid_cents", 0) or 0)
            settled_at = s.get("settled_time") or kalshi_state["timestamp_utc"]
            newly_settled.append({
                "ticker": tk, "side": side, "qty": qty,
                "pnl_cents": int(s["revenue_cents"]) - cost - fees,   # payout − cost − entry fee (net, like the ghost path)
                "settlement_result": s.get("market_result"),
                "entry_cents": entry, "opened_utc": prev.get("opened_utc", ""),
                "market_close_time": "",
                "settled_at_utc": settled_at, "settled_at": settled_at,
                "synced_from_kalshi": True, "source": "live_settlement_reconcile",
            })
            reconciled_prevs.append(prev)

    # ── Feed REALIZED settlement P&L into RiskGate (live daily/weekly $ kill-switch) — FEED FIRST ──
    # closed[] is the dedup ledger, so it must NOT be committed before the feed: if the feed failed
    # after closed[] was persisted, the settled ticker would be skipped next cycle and its loss would
    # be permanently masked from the stop (audit 2026-06-25 HIGH). Feed first; commit closed[] +
    # settlement-log only on success, else leave them uncommitted to re-capture + re-feed next cycle.
    # OPTION B: realized settlements only (newly_settled + gap_settled), NOT the mark-inclusive Kalshi
    # balance delta, so an intraday mark swing can't prematurely trip the stop. (The pre-existing dead
    # feed — RiskGate(_state_dir=<str>) crashing in .mkdir, swallowed — is fixed by passing a Path.)
    fed = newly_settled + gap_settled
    feed_ok = True
    if fed:
        try:
            from trader.risk import RiskGate
            import re as _re
            def _city(tk):
                m = _re.match(r'KX(?:HIGH|LOW)T?([A-Z]+)', tk or "")
                return m.group(1) if m else None
            risk = RiskGate(_state_dir=Path(state_dir))          # Path: __post_init__ calls .mkdir
            # ATOMIC feed (single persist): budget + per-city counters in one transaction, so a mid-feed
            # failure persists NOTHING and the retry (carried-forward settlements) can't double-count the
            # $ kill-switch. (QA 2026-06-26)
            risk.record_settlements_batch(
                [{"pnl_cents": int(r.get("pnl_cents", 0)), "city_code": _city(r["ticker"])} for r in fed])
            try:                                                 # notify only (sync places no orders)
                ok_budget, budget_reason = risk.check_budget()
                if not ok_budget:
                    from trader.notify import alert
                    alert(f"LIVE loss limit hit: {budget_reason}", key="live_budget_stop")
            except Exception:
                pass
        except Exception as e:
            feed_ok = False
            print(f"  ⚠ RiskGate feed FAILED — closed[]/log NOT committed this cycle, will retry: {e}",
                  file=sys.stderr)

    # Commit settled trades into closed[] (+ settlement-log) ONLY after a successful feed, so the dedup
    # ledger never outruns the kill-switch. On feed failure the open book is still refreshed, but the
    # settlements stay uncommitted → re-captured + re-fed next cycle.
    # On feed failure: the qty=0-ghost + gap settlements Kalshi RE-REPORTS next cycle auto-retry, but a
    # disappeared (purged) reconciled one OR a qty=0 live_sync ghost Kalshi purges THIS cycle would be
    # gone from current_tickers → carry their snapshots into pending_settlement so previous_open_tickers
    # re-contains them and the reconcile re-fires next cycle. (gap_settled has no snapshot to carry —
    # accepted residual: lost only if the feed fails AND Kalshi purges it the same cycle.) (QA 2026-06-26)
    if not feed_ok:
        for _p in reconciled_prevs + ghost_prevs:
            _carry_forward(_p, pending_settlement)
    committed = (newly_settled + gap_settled) if feed_ok else []
    all_closed = previous_closed + committed
    balance_delta = sum(int(r.get("pnl_cents", 0)) for r in committed)   # realized this cycle (return dict)

    book = {
        "version": 2,
        "cash_cents": cash,
        "starting_bank_cents": starting_bank,
        "open": open_positions,
        "pending_settlement": pending_settlement,
        "pending_makers": [],
        "closed": all_closed,
        "filled_orders": [],
        "fills_total": len(open_positions),
        "realized_pnl_cents": sum(int(c.get("pnl_cents", 0)) for c in all_closed),
        "trades_closed": len(all_closed),
        "updated_utc": kalshi_state["timestamp_utc"],
        "sync_source": "kalshi_api",
    }
    _tmp = book_path.with_name(book_path.name + f".{os.getpid()}.tmp")   # atomic + per-process tmp (QA-19)
    with open(_tmp, "w") as f:
        json.dump(book, f, indent=2)
    os.replace(_tmp, book_path)

    # Per-trade settlement-log (read by the fill-quality watchdog + the Stage-2 gate): attributed
    # settlements (newly_settled) PLUS gap_settled, now flagged (gap_settled:true, qty=0/entry=None) so
    # the log is COMPLETE for total-P&L/win-rate while the band/per-contract analyses still skip the
    # attribution-less rows. Both dedupe via closed[]/already_settled, so no double-logging across cycles.
    logged = (newly_settled + gap_settled) if feed_ok else []
    if logged:
        settlement_log = Path(state_dir) / "settlement-log.jsonl"
        from data.weather_data import append_jsonl_atomic   # QA-17: flock-guarded atomic append
        append_jsonl_atomic(settlement_log, logged)
        if gap_settled:
            print(f"  ⓘ logged {len(gap_settled)} gap-settled fill(s) (intraday open+settle; flagged, "
                  f"attribution-less) to settlement-log", file=sys.stderr)

    # Track the Kalshi account total: observability + the size-ramp baseline (paper_trade
    # live_size_rung). NOT used by the daily/weekly kill-switch, which resets on UTC date / ISO week.
    new_total = weather_budget_total_cents(kalshi_state)
    # Weather-only baseline for the size-ramp; structurally immune to non-weather settlements (unlike
    # new_total, which still carries realized non-weather cash). Uses realized (feed-independent, from
    # book) + fresh weather open marks, so it's valid even on a balance-feed failure.
    weather_total = weather_ramp_total_cents(book["realized_pnl_cents"], kalshi_state)
    sync_state_path = Path(state_dir) / "sync-state.json"
    if not balance_ok:
        # QA-03: a balance-feed failure collapses new_total toward 0 (or negative). Don't overwrite
        # the size-ramp baseline with a bogus total — carry the previous sync-state values forward.
        prev_ss = {}
        if sync_state_path.exists():
            try:
                with open(sync_state_path) as f:
                    prev_ss = json.load(f)
            except (OSError, json.JSONDecodeError):
                prev_ss = {}
        sync_payload = {
            "last_total_cents": prev_ss.get("last_total_cents", new_total),
            "last_weather_total_cents": prev_ss.get("last_weather_total_cents", weather_total),
            "last_available_cents": prev_ss.get("last_available_cents", kalshi_state["available_cents"]),
            "last_portfolio_cents": prev_ss.get("last_portfolio_cents", kalshi_state.get("portfolio_cents", 0)),
            "updated_utc": kalshi_state["timestamp_utc"],
            "balance_stale": True,
        }
    else:
        sync_payload = {
            "last_total_cents": new_total,
            "last_weather_total_cents": weather_total,
            "last_available_cents": kalshi_state["available_cents"],
            "last_portfolio_cents": kalshi_state.get("portfolio_cents", 0),
            "updated_utc": kalshi_state["timestamp_utc"],
        }
    _sst = sync_state_path.with_name(sync_state_path.name + f".{os.getpid()}.tmp")   # atomic + per-process tmp (QA-19)
    with open(_sst, "w") as f:
        json.dump(sync_payload, f, indent=2)
    os.replace(_sst, sync_state_path)

    # ── At-fill markout telemetry (2026-07-05) ──────────────────────────────────────────────────
    # Snapshot the current mid the moment a fill is first observed, so the markout log has a ≤~30min
    # post-fill point instead of only the 6×/day cmd_report cycles (~1.9h median first snapshot).
    # Purely additive + fail-soft; unblocks the spread-capture-vs-adverse-selection split of the
    # YES bleed (reviews/yes-bleed-decomposition-2026-07-04.md). Deliberately runs AFTER every state
    # write above so a slow/failing orderbook fetch can never delay or block the book / settlement /
    # kill-switch path.
    _snapshot_new_fills_markout(state_dir, open_positions, previous_open_tickers, had_previous_book)

    settled_count = len(set(previous_open_tickers.keys()) - {p["ticker"] for p in open_positions})

    return {
        "book_path": str(book_path),
        "synced_positions": len(open_positions),
        "cash_cents": cash,
        "previous_cash": None,
        "balance_delta_cents": balance_delta,
        "settled_detected": settled_count,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sync live book from Kalshi API")
    parser.add_argument("--report", action="store_true", help="Print comparison only, don't write")
    args = parser.parse_args()

    kalshi = get_kalshi_positions()
    if not kalshi:
        print("❌ Failed to fetch Kalshi state")
        return 1

    print(f"Kalshi: {kalshi['num_positions']} positions, "
          f"${kalshi['available_cents']/100:.2f} available, "
          f"${kalshi['total_cents']/100:.2f} total, "
          f"${kalshi['total_deployed_cents']/100:.2f} deployed")
    print()

    for pos in kalshi["positions"]:
        print(f"  {pos['ticker']:45s} {pos['side']:3s} qty={pos['qty']:3d} "
              f"pos_fp={pos['position_fp']:8.2f} "
              f"cost=${pos['total_cost_cents']/100:.2f} "
              f"exposure=${pos['market_exposure_cents']/100:.2f} "
              f"pnl=${pos['realized_pnl_cents']/100:.2f}")

    if args.report:
        print("\n📋 Report only — not writing to disk.")
        return 0

    result = sync_live_book(kalshi)
    print(f"\n✅ Synced {result['synced_positions']} positions to {result['book_path']}")
    print(f"   Cash: ${result['cash_cents']/100:.2f}")
    delta = result.get('balance_delta_cents', 0)
    if delta != 0:
        print(f"   Balance Δ: ${delta/100:+.2f} → fed to RiskGate (budget breaker + city counters)")
    if result.get('settled_detected', 0) > 0:
        print(f"   Settled: {result['settled_detected']} positions cleared from Kalshi")
    return 0


if __name__ == "__main__":
    sys.exit(main())

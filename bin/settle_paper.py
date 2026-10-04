#!/usr/bin/env python3
"""
bin/settle_paper.py — Settle paper positions against live Kalshi settlement results.

For each open paper position, fetch the prod market. If `status == "finalized"`
and `result` is "yes" or "no", mark the position closed at $1 or $0.

We trust Kalshi's official settlement rather than re-deriving from NWS, since
the markets settle on the same NWS Climatological Report we'd be parsing
anyway, and Kalshi handles the rounding/conversion quirks.

Usage:
  python3 bin/settle_paper.py
  python3 bin/settle_paper.py --dry-run    # show would-settle but don't mutate
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.paper_book import PaperBook
from trader.brier import BrierLogger
from model.error_tracker import ErrorTracker, fetch_actual_temp
from kalshi_weather.data.weather_data import STATIONS, get_station_for_city, parse_market_ticker


KALSHI_CLI = os.environ.get("KALSHI_CLI", "kalshi-cli")
STATE_DIR = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", ROOT / "state"))
LOSS_QUEUE = STATE_DIR / "loss-queue.jsonl"
SETTLEMENT_LOG = STATE_DIR / "settlement-log.jsonl"

# Grace period after a market's CLOSE_TIME before a still-missing result is treated
# as an anomaly. FIX 2026-07-06 (audit): this was keyed to expiration_time, which on
# these weather series is ~6 DAYS after close (empirically ~152h) — so the watchdog
# stayed silent for ~6 days, defeating the exact "settle pass never ran" incident it
# exists for (the 14h-cron-gap dropped ~13 settlements). Kalshi actually publishes
# `result` ~10-12h after close_time, so anchor to close_time with a 24h grace: well
# past the normal ~12h lag, catches a stalled/missing settle pass within a day.
STALE_SETTLE_GRACE_SECONDS = 24 * 3600  # 24h past CLOSE (results land ~10-12h after close)


def _parse_iso(ts: str):
    """Parse an ISO-8601 timestamp (Kalshi uses a trailing 'Z'). Returns an
    aware datetime, or None if missing/unparseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _enqueue_loss(record: dict) -> None:
    """Append a confirmed loss to the post-mortem queue."""
    LOSS_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOSS_QUEUE, "a") as f:
        f.write(json.dumps(record) + "\n")


def _parsed_snapshot(ticker: str, market: dict) -> Optional[dict]:
    """JSON-safe parse_market_ticker snapshot for the settlement / loss-queue record.

    A T-market's DIRECTION (above vs below) can only be recovered from the market
    TITLE (parse_market_ticker → _infer_direction_from_title), which lives on the
    raw `market` dict — never on the bare ticker. bin/postmortem.py runs long after
    settlement with only the ticker in hand, so re-parsing there defaults every
    "<S°" below-market to "above" and mis-signs its diagnosis. Persisting the parse
    HERE, where the title is available, lets the post-mortem read the correct
    direction/boundary instead of re-deriving it.

    ±inf bin edges (T-markets) are flattened to None so the on-disk JSONL stays
    standard JSON; classification only reads bin_low/bin_high for 'between' markets,
    whose edges are finite. Returns None (never raises) if the ticker can't be
    parsed — a diagnostics-only snapshot must not break the settlement money path.
    """
    try:
        parsed = parse_market_ticker(ticker, raw_market=market)
    except Exception as e:  # noqa: BLE001 — must never break settlement
        print(f"[settle] note: could not snapshot-parse {ticker}: {e}", file=sys.stderr)
        return None
    for k in ("bin_low", "bin_high"):
        v = parsed.get(k)
        if isinstance(v, float) and not math.isfinite(v):
            parsed[k] = None
    return parsed


def _log_settlement(record: dict) -> None:
    # QA-17 (2026-07-01): flock-guarded atomic append so a manual settle run can't interleave a
    # torn settlement line with the cron's concurrent append.
    from data.weather_data import append_jsonl_atomic
    append_jsonl_atomic(SETTLEMENT_LOG, record)


def _find_position_context(book: PaperBook, ticker: str) -> list[dict]:
    """Snapshot open positions in `ticker` before settlement, with full context."""
    return [dict(p) for p in book.open if p.get("ticker") == ticker]


def fetch_market(ticker: str) -> dict:
    """Fetch market with exponential-backoff retries.

    FIX 2026-06-11 (audit-trade-lifecycle Finding 4.1): a single transient
    failure (network blip, Kalshi API hiccup) used to delay settlement by
    24h. Now we retry up to 3 times with 1s/2s/4s waits.
    """
    import time
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            out = subprocess.run(
                [KALSHI_CLI, "--prod", "markets", "get", ticker, "--json"],
                capture_output=True, text=True, timeout=15, check=True,
            )
            result = json.loads(out.stdout)
            # Sanity-check expected fields (audit Finding 4.2)
            if not isinstance(result, dict):
                raise ValueError(f"unexpected non-dict response: {type(result).__name__}")
            return result
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
            last_err = e
            wait = 2 ** attempt  # 1, 2, 4 seconds
            print(f"[settle] fetch attempt {attempt+1}/3 failed for {ticker}: {e} — retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"[settle] fetch failed after 3 attempts for {ticker}: {last_err}", file=sys.stderr)
    return {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    book = PaperBook()
    brier = BrierLogger()
    if not book.open:
        print(json.dumps({"status": "nothing_open"}, indent=2))
        return 0

    settlements: list[dict] = []
    skipped: list[dict] = []
    stale: list[dict] = []

    now = datetime.now(timezone.utc)
    # Live arms: positions are synced from Kalshi (synced_from_kalshi=True) with a different
    # schema (cost_cents/order_id, no fee_cents_total), and their per-trade realized P&L is
    # captured directly in sync_live_positions.py from Kalshi's realized_pnl_cents. Skip them
    # here so settle_paper never (a) KeyErrors on the synced schema (settle_position assumes the
    # paper-fill schema) nor (b) double-counts the live $ kill-switch — sync already feeds the
    # budget once from the Kalshi balance delta (the 2026-06-19 update_budget=False invariant).
    seen_tickers = sorted({pos["ticker"] for pos in book.open
                           if not pos.get("synced_from_kalshi")})
    for tk in seen_tickers:
        m = fetch_market(tk)
        if not m:
            skipped.append({"ticker": tk, "reason": "fetch_failed"})
            continue
        status = m.get("status", "")
        result = (m.get("result") or "").strip().lower()
        close_time = m.get("close_time", "")
        expiration_time = m.get("expiration_time", "")

        # Settle ONLY on an official Kalshi result. On weather markets `status`
        # flips to 'closed' (trading halted) DAYS before settlement, while
        # `result` stays empty until expiration_time, so the result field is the
        # single source of truth — we never trust 'closed' (or any status) alone.
        # 'void' is a terminal result too (Kalshi voids when the NWS settlement reading is
        # unavailable/under review) → route it to settle for a refund, not to stale-forever
        # (2026-07-13 audit: there was no code path for a void, so a voided market's positions
        # and the hourly stale_settle alert would persist indefinitely).
        if result not in ("yes", "no", "void"):
            ct = _parse_iso(close_time)
            # Overdue is measured from CLOSE_TIME (when the result becomes available
            # ~10-12h later), NOT expiration_time (~6 days out — see grace const).
            overdue_s = (now - ct).total_seconds() if ct else None
            if ct and ct > now:
                # Still trading — entirely normal.
                skipped.append({"ticker": tk, "reason": f"not_yet_closed (status={status} close_time={close_time})"})
            elif overdue_s is not None and overdue_s > STALE_SETTLE_GRACE_SECONDS:
                # Past official expiration + grace but still no result. This is
                # the anomaly we care about: a settle pass never ran, or Kalshi
                # is late. Surface it loudly instead of silently skipping forever.
                stale.append({
                    "ticker": tk,
                    "status": status,
                    "result": result,
                    "expiration_time": expiration_time,
                    "hours_overdue": round(overdue_s / 3600.0, 1),
                })
                print(
                    f"[settle] ⚠️ STALE: {tk} expired {overdue_s / 3600.0:.1f}h ago, "
                    f"still no result (status={status}) — investigate",
                    file=sys.stderr,
                )
            else:
                # Closed, within the normal finalization-lag window.
                skipped.append({"ticker": tk, "reason": f"no_result_yet (status={status} result={result!r})"})
            continue

        # Result is present → settle. If the status is unexpectedly non-terminal,
        # trust the result but note the discrepancy for debugging.
        if status and status not in ("finalized", "settled", "closed"):
            print(f"[settle] note: {tk} has result={result!r} but unexpected status={status!r}; settling on result", file=sys.stderr)

        if args.dry_run:
            settlements.append({"ticker": tk, "would_settle": result, "dry_run": True})
            continue

        # Capture pre-settlement context for any losses
        pre_ctx = _find_position_context(book, tk)
        # Parse ONCE with the raw market in hand so the settlement / loss-queue records
        # carry the correct T-market direction (below vs above). The bare ticker can't
        # recover it downstream — see _parsed_snapshot.
        market_parsed = _parsed_snapshot(tk, m)

        out = book.settle_position(tk, result)
        for s in out:
            s["ticker"] = tk
            s["settlement"] = result
            settlements.append(s)
            print(f"[settle] {tk} → {result.upper()}: pnl ${s['pnl_cents']/100:+.2f}", file=sys.stderr)

            # Brier outcome logging
            ctx = next((c for c in pre_ctx if c.get("paper_order_id") == s.get("paper_order_id")), {})
            brier_record_id = ctx.get("brier_record_id")
            if not brier_record_id:
                # Fallback: match by fingerprint so we never silently lose a settlement
                brier_record_id = brier.find_record_by_fingerprint(
                    tk, s["side"], s["qty"],
                    ctx.get("avg_entry_cents", 0),
                    ctx.get("fair_prob_at_open", 0.0),
                )
            if brier_record_id and result in ("yes", "no"):
                brier.record_outcome(brier_record_id, outcome=(result == "yes"))
                print(f"[settle]   ↳ Brier outcome logged for {brier_record_id}", file=sys.stderr)
            elif result in ("yes", "no"):
                print(f"[settle]   ↳ No brier_record_id for this position (fallback also failed)", file=sys.stderr)
            # void → no Brier outcome to score (handled by the void-skip below)

            # Always log the settlement
            settlement_record = {
                "ticker": tk,
                "side": s["side"],
                "qty": s["qty"],
                "pnl_cents": s["pnl_cents"],
                "settlement_result": result,
                "settled_at_utc": datetime.now(timezone.utc).isoformat(),
                "market_settlement_price": m.get("settlement_value") or m.get("final_price") or m.get("result"),
                "market_close_time": m.get("close_time"),
                "entry_cents": ctx.get("avg_entry_cents"),
                "fair_prob_at_open": ctx.get("fair_prob_at_open"),
                "opened_utc": ctx.get("opened_utc"),
                "rationale": ctx.get("rationale", ""),
                "paper_order_id": ctx.get("paper_order_id"),
                # Direction-correct parse (title in hand); consumed by bin/postmortem.py
                # so below-markets aren't mis-parsed as "above". None if parse failed.
                "market_parsed": market_parsed,
            }
            _log_settlement(settlement_record)

            # Queue losses for post-mortem analysis
            if s["pnl_cents"] < 0:
                _enqueue_loss({**settlement_record, "status": "pending_postmortem"})
                print(f"[settle]   ↳ enqueued for post-mortem", file=sys.stderr)

            if result == "void":
                # A void has NO outcome: the record above logs it (refunded, pnl 0), but skip the
                # consecutive-loss counter and the error-model bias/std update — there is nothing to
                # score, and a void must not reset a loss streak or feed the forecast-bias EWMA.
                print(f"[settle]   ↳ VOID: {tk} refunded (pnl 0), no outcome scored", file=sys.stderr)
                continue

            # Update risk gate consecutive-loss counters
            try:
                city_code = _extract_city_from_ticker(tk)
                from trader.risk import RiskGate
                rg = RiskGate()
                rg.record_settlement(s["pnl_cents"], city_code=city_code)
                print(f"[settle]   ↳ risk gate updated: consecutive_losses={rg._consecutive_losses}, city={city_code}", file=sys.stderr)
            except Exception as e:
                print(f"[settle]   ↳ risk gate update failed: {e}", file=sys.stderr)

            # Update error model with actual temp
            try:
                city_code = _extract_city_from_ticker(tk)
                forecast_f = ctx.get("forecast_f") if ctx else None
                if city_code and forecast_f is not None:
                    _update_error_model(city_code, forecast_f, tk, m)
                elif city_code:
                    # forecast_f absent → error model can't learn from this settlement.
                    # This silently froze the per-city bias/std EWMA Jun 18–Jul 6 2026
                    # (maker positions never carried forecast_f). Make it visible so a
                    # future regression surfaces in the cycle log instead of freezing.
                    print(
                        f"[settle]   ↳ error model SKIPPED for {tk}: position has no "
                        f"forecast_f (bias/std cannot update)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[settle]   ↳ error model update failed: {e}", file=sys.stderr)

    if stale:
        print(f"[settle] ⚠️ {len(stale)} position(s) overdue for settlement — see 'stale' in output", file=sys.stderr)
        try:
            from trader.notify import alert
            # Per-SET dedup key + 12h window (2026-07-13 audit). The old global "stale_settle" key with
            # the 1h default fired ~10 Telegram sends/day on the stuck MIA JUL07 market AND masked any
            # NEWLY-stale ticker for up to an hour. Keying on the sorted stale-ticker set means a new
            # stale ticker changes the key and re-fires immediately, while a persistent known-stale set
            # is deduped for 12h — surfaced ~twice/day, not a flood.
            all_tk = sorted(s["ticker"] for s in stale)
            alert(f"⚠️ {len(stale)} position(s) overdue for settlement ({', '.join(all_tk[:5])}). "
                  f"Settle pass may be stuck or Kalshi is late.",
                  key="stale_settle:" + ",".join(all_tk), dedup_seconds=12 * 3600)
        except Exception:
            pass

    print(json.dumps({
        "status": "ok",
        "dry_run": args.dry_run,
        "settled": settlements,
        "skipped": skipped,
        "stale": stale,
        "book_summary": book.summary(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return 0


def _extract_city_from_ticker(ticker: str) -> Optional[str]:
    """Extract city code from a Kalshi weather ticker.

    Tickers look like: KXHIGHTHOU-26MAY29-B71.5 or KXLOWTSEA-26MAY28-T55
    or KXTEMPNYCH-26MAY2816-T82 (hourly temperature markets).
    We want the city code (HOU, SEA, NYCH, etc.) sitting between the
    HIGHT/LOWT/TEMP/HIGH/LOW prefix and the first '-'.

    FIX 2026-06-11 (audit-trade-lifecycle Finding 4.3): the prior regex only
    matched HIGHT|LOWT, which silently skipped TEMP markets from city-level
    loss tracking and error model updates.
    """
    import re as _re
    m = _re.match(r"^KX(?:HIGHT|LOWT|HIGH|LOW|TEMP)([A-Z]+)-", ticker)
    if not m:
        return None
    city = m.group(1)
    # Guard against date-code false positives (e.g. MAY/JUN in malformed tickers)
    _MONTH_CODES = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
    if city in _MONTH_CODES:
        return None
    if not (2 <= len(city) <= 5) or not city.isalpha():
        return None
    return city


def _update_error_model(
    city_code: str,
    forecast_f: float,
    ticker: str,
    market: dict,
) -> None:
    """Fetch actual temp and record error for this settlement."""
    # Resolve market type from ticker
    # FIX 2026-06-11: include HIGH/LOW/TEMP (was: HIGHT/LOWT only)
    mtype = (
        "daily_high" if ("HIGHT" in ticker or "HIGH" in ticker)
        else ("daily_low" if ("LOWT" in ticker or "LOW" in ticker) else None)
    )
    if mtype is None:
        return

    # Resolve station from city code. "NY" (KXHIGHNY daily-high) + "NYCH" both map to NYC — without
    # the "NY" alias the shared error model never learned NYC's daily-high bias (QA 2026-06-25; the
    # read/pricing path was aliased in c1a510a but this independent extractor was not).
    CITY_ALIAS = {"NYCH": "NYC", "NY": "NYC"}
    city = CITY_ALIAS.get(city_code, city_code)
    if city not in STATIONS:
        print(f"[settle]   ↳ error model: unknown city {city} for {ticker}", file=sys.stderr)
        return

    meta = STATIONS[city]

    # Parse date from ticker: KXHIGHTDAL-26JUN03-B89.5 -> 2026-06-03
    parts = ticker.split("-")
    if len(parts) < 2:
        return
    date_str = parts[1]
    if len(date_str) >= 7:
        yr = "20" + date_str[:2]
        month_map = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
        }
        mon = month_map.get(date_str[2:5], "01")
        day = date_str[5:7]
        date_iso = f"{yr}-{mon}-{day}"
    else:
        return

    # correction_city: train the bias EWMA on the STATION-consistent actual (grid + learned
    # per-city correction), so city_bias_f moves the KDE price toward Kalshi's settlement
    # station, not the colder grid cell (audit 2026-07-06 #7).
    actual_f = fetch_actual_temp(meta["lat"], meta["lon"], date_iso, mtype, correction_city=city)
    if actual_f is None:
        print(f"[settle]   ↳ error model: no actual temp for {city} on {date_iso}", file=sys.stderr)
        return

    tracker = ErrorTracker()
    tracker.load()
    tracker.record_error(city, forecast_f, actual_f)
    tracker.save()
    print(f"[settle]   ↳ error model: {city} forecast={forecast_f}°F actual={actual_f}°F "
          f"(bias={tracker.get_bias(city):+.2f}°F)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

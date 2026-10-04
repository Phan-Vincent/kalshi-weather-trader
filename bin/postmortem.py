#!/usr/bin/env python3
"""
bin/postmortem.py — Drain the loss queue, gather context, write post-mortems.

For each queued loss:
  1. Fetch the observed weather (NWS Climate Daily Report via api.weather.gov).
  2. Reconstruct what the forecast said when we opened the position (from cache
     if available, otherwise note "context unavailable").
  3. Classify the failure mode (forecast miss, model miscalibration, bad market
     selection, late update, etc).
  4. Write a structured post-mortem to postmortems/YYYY-MM-DD/ticker.md.
  5. Append one distilled lesson to LESSONS.md, which the fair-value builder
     reads on subsequent runs so the model gets a chance to course-correct.

Usage:
  python3 bin/postmortem.py                # drain all queued losses
  python3 bin/postmortem.py --limit 5      # process at most 5
  python3 bin/postmortem.py --dry-run      # show what it would do
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from data.weather_data import STATIONS, parse_market_ticker


STATE_DIR = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", ROOT / "state"))
LOSS_QUEUE = STATE_DIR / "loss-queue.jsonl"
PROCESSED = STATE_DIR / "loss-queue-processed.jsonl"   # QA-sweep 2026-07-01: honor KALSHI_WEATHER_STATE_DIR like LOSS_QUEUE (was hardcoded ROOT/state)
POSTMORTEM_DIR = ROOT / "postmortems"
LESSONS_FILE = ROOT / "LESSONS.md"
CACHE_DIR = ROOT / "cache"

# Station-basis correction (drawdown fix #3, default OFF). Kalshi settles daily high/low
# markets on the OFFICIAL NWS station, but this postmortem scores the observed value against
# the raw Open-Meteo archive GRID — a >=1.7°F gap on some events (e.g. ATL) that misclassifies
# failures and poisons the distilled lessons. When armed, apply the SAME learned per-city
# grid→station offset that settle_paper / CRPS already use (model.error_tracker). Kept OFF by
# default because the observed value flows through postmortem_deep→LESSONS.md→the fair-value
# side-bias, i.e. it can reach LIVE order selection and splice the futility-checkpoint stream;
# the operator flips this on (KALSHI_WEATHER_POSTMORTEM_STATION_BASIS=1) after the checkpoint.
_STATION_BASIS = os.environ.get("KALSHI_WEATHER_POSTMORTEM_STATION_BASIS", "0") == "1"

# Cap LESSONS.md so the builder doesn't drown in old advice
MAX_LESSONS = 30


def _stderr(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] postmortem: {msg}", file=sys.stderr)


def _load_queue() -> list[dict]:
    if not LOSS_QUEUE.exists():
        return []
    out = []
    with open(LOSS_QUEUE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _write_queue(records: list[dict]) -> None:
    LOSS_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOSS_QUEUE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, LOSS_QUEUE)


def _append_processed(record: dict) -> None:
    PROCESSED.parent.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED, "a") as f:
        f.write(json.dumps(record) + "\n")


def _fetch_observed(city_code: str, date_iso: str) -> dict:
    """Try to fetch observed daily summary for the station from NWS / Open-Meteo
    archive. Returns {high_f, low_f, source} or {} if unavailable."""
    station = STATIONS.get(city_code, {})
    lat = station.get("lat")
    lon = station.get("lon")
    out = {}

    # Open-Meteo archive is the simplest reliable source for past daily extremes.
    if lat and lon:
        url = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={date_iso}&end_date={date_iso}"
            "&daily=temperature_2m_max,temperature_2m_min"
            "&temperature_unit=fahrenheit&timezone=auto"
        )
        try:
            import urllib.request, ssl
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            req = urllib.request.Request(
                url, headers={"User-Agent": "kalshi-weather-postmortem/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read())
            daily = data.get("daily", {})
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])
            if highs and lows:
                hi, lo = highs[0], lows[0]
                src = "open-meteo-archive"
                # Station-basis correction (fix #3): re-base the grid onto the settlement station
                # using the learned per-city offset (same one settle_paper's EWMA applies). OFF
                # by default; when a (city,metric) has too few learned events the offset is 0.0,
                # so this leaves the raw grid unchanged for those cities.
                if _STATION_BASIS:
                    try:
                        from model.error_tracker import _actual_correction_f
                        hi = hi + _actual_correction_f(city_code, "daily_high")
                        lo = lo + _actual_correction_f(city_code, "daily_low")
                        src = "open-meteo-archive+station-corr"
                    except Exception as e:
                        _stderr(f"station-basis correction failed for {city_code} {date_iso}: {e}")
                out = {
                    "high_f": hi,
                    "low_f": lo,
                    "source": src,
                    "station_lat": lat,
                    "station_lon": lon,
                }
        except Exception as e:
            _stderr(f"observed fetch failed for {city_code} {date_iso}: {e}")

    return out


def _load_forecast_at_open(city_code: str, opened_utc: str) -> dict:
    """Look for a cached forecast file matching the opened-at hour."""
    if not opened_utc:
        return {}
    station = STATIONS.get(city_code, {})
    icao = station.get("station") or station.get("icao") or city_code
    try:
        dt = datetime.fromisoformat(opened_utc.replace("Z", "+00:00"))
    except Exception:
        return {}
    # Look for cache files around the open time (within 2 hours)
    if not CACHE_DIR.exists():
        return {}
    candidates = sorted(CACHE_DIR.glob(f"forecast-{icao}*.json"))
    for c in candidates[::-1]:  # newest first
        try:
            with open(c) as f:
                d = json.load(f)
            # Best effort: return whatever the file has
            return {"_cache_file": str(c.name), **{k: v for k, v in d.items() if k != "raw"}}
        except Exception:
            pass
    return {}


def _parsed_for_record(record: dict) -> dict:
    """Parsed market fields for a loss/settlement record.

    Prefer the `market_parsed` snapshot that settle_paper.py stores at settlement
    time — it was parsed WITH the raw market title, so the T-market direction
    (below vs above) and its ±0.5 boundary are correct. Fall back to re-parsing the
    bare ticker for legacy records written before the snapshot existed; that
    fallback CANNOT recover below-markets — parse_market_ticker defaults a
    title-less T-market to "above" (data/weather_data._infer_direction_from_title)
    — so legacy "<S°" post-mortems stay mis-signed until backfilled.
    """
    stored = record.get("market_parsed")
    if isinstance(stored, dict) and stored.get("bin_kind"):
        return stored
    return parse_market_ticker(record.get("ticker", ""))


def _classify_failure(loss: dict, observed: dict) -> dict:
    """Determine the failure mode. Returns dict with class + signed errors."""
    ticker = loss["ticker"]
    side = loss["side"]
    fair_at_open = loss.get("fair_prob_at_open")
    entry = loss.get("entry_cents", 0)
    settlement = loss.get("settlement_result")

    parsed = _parsed_for_record(loss)
    market_type = parsed.get("market_type", "unknown")
    threshold = parsed.get("threshold_f")
    bin_kind = parsed.get("bin_kind")
    bin_low = parsed.get("bin_low")
    bin_high = parsed.get("bin_high")

    # What the observation actually was
    if market_type in ("daily_high", "hourly_temp"):
        observed_val = observed.get("high_f")
    elif market_type == "daily_low":
        observed_val = observed.get("low_f")
    else:
        observed_val = None

    classification = {
        "ticker": ticker,
        "side": side,
        "settlement_result": settlement,
        "fair_prob_at_open": fair_at_open,
        "entry_cents": entry,
        "market_type": market_type,
        "threshold_f": threshold,
        "bin_kind": bin_kind,
        "bin_range_f": [bin_low, bin_high] if bin_kind == "between" else None,
        "observed_temp_f": observed_val,
        "observed_source": observed.get("source"),
    }

    # Buckets
    if observed_val is None:
        classification["failure_mode"] = "no_observation_data"
        classification["lesson"] = (
            f"[{ticker}] No observation data available for {parsed.get('city_code')} "
            f"on {parsed.get('date_iso')}. Add a fallback obs source."
        )
        return classification

    # Was the forecast directionally right but the model was overconfident?
    if fair_at_open is not None:
        # We bet our `side` would win at probability `fair_at_open`. We lost.
        # If we bet YES with fair_prob > 0.7, we were very confident and still
        # wrong = overconfidence or forecast miss.
        confidence_class = (
            "overconfident" if fair_at_open >= 0.70 or fair_at_open <= 0.30
            else "marginal"
        )
    else:
        confidence_class = "unknown"

    if bin_kind in ("above", "below") and threshold is not None:
        miss_f = observed_val - threshold
        direction = "warmer_than_threshold" if miss_f > 0 else "cooler_than_threshold"
        # ABOVE: YES wins when observed > threshold. BELOW: YES wins when observed <
        # threshold (matches shadow_score.outcome). So a lost YES on an ABOVE market
        # means the day came in cooler than we bet (forecast too warm); a lost YES on
        # a BELOW market means it came in warmer (forecast too cool). NO mirrors YES
        # within each direction.
        yes_loss_means_too_warm = (bin_kind == "above")
        if (side == "yes") == yes_loss_means_too_warm:
            classification["failure_mode"] = f"forecast_too_warm_{confidence_class}"
        else:
            classification["failure_mode"] = f"forecast_too_cool_{confidence_class}"
        classification["observed_minus_threshold_f"] = round(miss_f, 2)
        classification["direction"] = direction
    elif bin_kind == "between" and bin_low is not None and bin_high is not None:
        if observed_val < bin_low:
            miss = "below_bin"
        elif observed_val > bin_high:
            miss = "above_bin"
        else:
            miss = "in_bin_but_we_bet_against"
        classification["failure_mode"] = f"bin_miss_{miss}_{confidence_class}"
        classification["bin_distance_f"] = round(
            min(abs(observed_val - bin_low), abs(observed_val - bin_high)), 2
        )
    else:
        classification["failure_mode"] = f"unclassified_{confidence_class}"

    # Distilled lesson
    fm = classification["failure_mode"]
    # Metric label derived from market_type (drawdown fix #3): the forecast_too_warm lesson used
    # to hardcode "daily-high", mislabeling daily_LOW markets (e.g. SEA/PHIL lows) as highs in
    # the postmortem report. market_type is authoritative here (from the parsed snapshot).
    metric_label = {"daily_high": "daily-high", "daily_low": "daily-low",
                    "hourly_temp": "hourly-temp"}.get(market_type, (market_type or "temp").replace("_", "-"))
    if "forecast_too_warm" in fm:
        bias = "cold-bias the model" if "overconfident" in fm else "tighten the upper-tail floor"
        classification["lesson"] = (
            f"{parsed.get('city_code')} {metric_label} forecast was {observed_val:.1f}\u00b0F vs threshold {threshold}\u00b0F "
            f"(we bet {side.upper()} at fair_prob={fair_at_open:.2f}, lost {abs(loss['pnl_cents'])/100:.2f}). "
            f"Action: {bias} for {parsed.get('city_code')} in this season."
        )
    elif "forecast_too_cool" in fm:
        bias = "warm-bias the model" if "overconfident" in fm else "widen the lower-tail floor"
        classification["lesson"] = (
            f"{parsed.get('city_code')} forecast undershot by {abs(round(observed_val - threshold, 1))}\u00b0F. "
            f"Action: {bias} for {parsed.get('city_code')}."
        )
    elif "bin_miss" in fm:
        city = parsed.get("city_code", "?")
        if fm.startswith("bin_miss_above_bin"):
            adjust = f"warm-bias the model for {city}"
        elif fm.startswith("bin_miss_below_bin"):
            adjust = f"cold-bias the model for {city}"
        else:
            adjust = f"widen the Gaussian std floor for {city}"
        classification["lesson"] = (
            f"{city} bin market: observed {observed_val:.1f}\u00b0F, bin was "
            f"[{bin_low}, {bin_high}]. Action: {adjust}."
        )
    else:
        classification["lesson"] = (
            f"{ticker}: {fm}. Manual review needed."
        )

    return classification


def _write_postmortem(loss: dict, observed: dict, forecast_snapshot: dict, classification: dict) -> Path:
    """Write the structured post-mortem markdown."""
    ticker = loss["ticker"]
    date_str = loss.get("settled_at_utc", datetime.now(timezone.utc).isoformat())[:10]
    folder = POSTMORTEM_DIR / date_str
    folder.mkdir(parents=True, exist_ok=True)
    # File name safe
    safe_ticker = re.sub(r"[^A-Za-z0-9._-]", "_", ticker)
    out = folder / f"{safe_ticker}.md"

    body = f"""# Post-mortem: {ticker}

**Settled:** {loss.get('settled_at_utc', 'n/a')}
**Outcome:** lost ${abs(loss['pnl_cents']) / 100:.2f} on a {loss['side'].upper()} position
**Settlement result:** {loss.get('settlement_result', '?').upper()}

## What we believed

- Fair probability at open: **{loss.get('fair_prob_at_open', 'n/a')}**
- Entry price: **{loss.get('entry_cents', 'n/a')}\u00a2**
- Rationale: {loss.get('rationale', '(none recorded)')}
- Opened at: {loss.get('opened_utc', 'n/a')}

## What happened

- Observed value: **{classification.get('observed_temp_f', 'n/a')}\u00b0F** ({classification.get('observed_source', '?')})
- Market type: {classification.get('market_type', '?')} | bin: {classification.get('bin_kind', '?')} threshold/range: {classification.get('threshold_f') or classification.get('bin_range_f')}
- Failure mode: **{classification['failure_mode']}**

## Forecast snapshot at open

```json
{json.dumps(forecast_snapshot, indent=2, default=str)}
```

## Lesson

{classification['lesson']}

## Raw loss record

```json
{json.dumps(loss, indent=2, default=str)}
```
"""
    out.write_text(body)
    return out


from kalshi_weather.model.lessons import append_lesson, append_blacklist_systemic


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="Max records to process; 0 = all")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    queue = _load_queue()
    if not queue:
        print(json.dumps({"status": "empty_queue"}, indent=2))
        return 0

    processed = []
    remaining = list(queue)
    n = 0
    for record in queue:
        if args.limit and n >= args.limit:
            break
        n += 1

        ticker = record.get("ticker", "")
        parsed = _parsed_for_record(record)
        city = parsed.get("city_code", "")
        date_iso = parsed.get("date_iso", "")

        observed = _fetch_observed(city, date_iso) if city and date_iso else {}
        forecast = _load_forecast_at_open(city, record.get("opened_utc", ""))
        classification = _classify_failure(record, observed)

        if args.dry_run:
            print(json.dumps({"ticker": ticker, "classification": classification}, indent=2))
            continue

        path = _write_postmortem(record, observed, forecast, classification)
        # FIX 2026-06-15: LESSONS.md bias path deprecated.
        # Postmortem still writes detailed markdown reports to postmortems/,
        # but no longer distills one-line lessons into LESSONS.md. The EWMA
        # ErrorTracker is the authoritative city-bias source.
        # append_lesson(LESSONS_FILE, classification["lesson"], max_lessons=MAX_LESSONS)
        processed.append({
            "ticker": ticker,
            "post_mortem_path": str(path.relative_to(ROOT)),
            "failure_mode": classification["failure_mode"],
        })
        remaining.remove(record)
        _append_processed({**record, "post_mortem_path": str(path), "processed_at_utc": datetime.now(timezone.utc).isoformat(), "classification": classification})
        _stderr(f"wrote {path.relative_to(ROOT)}")

    if not args.dry_run:
        _write_queue(remaining)

    print(json.dumps({
        "status": "ok",
        "processed_count": len(processed),
        "remaining_in_queue": len(remaining),
        "processed": processed,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

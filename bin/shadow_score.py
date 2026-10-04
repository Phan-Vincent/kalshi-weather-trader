#!/usr/bin/env python3
"""
bin/shadow_score.py — Does the model beat the MARKET? (the profitability question)

Trade-only logging gives ~5 settled data points per cycle. The forecast-log
(written by build_fair_values.py) is a SHADOW prediction for EVERY priced market
(~384/cycle) — fair_prob + market_mid + which model priced it (legacy vs forecast).
This script settles those shadow predictions against realised temps and compares:

    model Brier   = mean (fair_prob   - outcome)^2
    market Brier  = mean (market_mid  - outcome)^2

Edge exists only if model Brier < market Brier. Brier skill vs market =
(market_brier - model_brier) / market_brier  (>0 ⇒ model beats the crowd).

Broken out by model mode (legacy climatology vs the forecast fix) and lead-time,
so you can see whether the forecast fix actually beats the market, not just
climatology. Read-only; reuses the CRPS actuals cache.

Usage:
    python3 bin/shadow_score.py              # fetch+cache actuals as needed
    python3 bin/shadow_score.py --no-fetch   # cached actuals only
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # crps_report helpers

from crps_report import parse_ticker, get_actual, lead_bucket, load_cache, STATE  # noqa
import crps_report

FLOG = STATE / "forecast-log.jsonl"
SHADOW_LOG = STATE / "shadow-score.jsonl"   # appended time series of model-vs-market skill (--log)


def outcome(bin_kind, thr, lo, hi, actual):
    """Resolve YES(1)/NO(0) for a shadow market given the realised temp."""
    if bin_kind == "above" and thr is not None:
        return 1 if actual > thr else 0
    if bin_kind == "below" and thr is not None:
        return 1 if actual < thr else 0
    if bin_kind == "between" and lo is not None and hi is not None:
        return 1 if (lo < actual <= hi) else 0
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--log", action="store_true",
                    help="append a summary row to state/shadow-score.jsonl (the forward-edge time series)")
    args = ap.parse_args()
    if not FLOG.exists():
        print("No forecast-log.jsonl yet — runs accumulate from build_fair_values.")
        return 1

    cache = load_cache()
    allow_fetch = not args.no_fetch
    today = datetime.now(timezone.utc).date().isoformat()

    # (mode, lead_bucket) -> [ (model_brier, market_brier) ]
    rows = defaultdict(list)
    rows_city = defaultdict(list)   # (mode, city) -> [(model_b, market_b)] — per-city breakdown
    n_scored = 0
    for line in open(FLOG):
        try:
            e = json.loads(line)
        except Exception:
            continue
        fp, mid = e.get("fair_prob"), e.get("market_mid")
        tp = parse_ticker(e.get("ticker", ""))
        if fp is None or not tp:
            continue
        city, mtype, date_iso = tp
        if date_iso >= today:                 # only settled days
            continue
        a = get_actual(city, mtype, date_iso, cache, allow_fetch)
        if a is None:
            continue
        out = outcome(e.get("bin_kind"), e.get("thr"), e.get("lo"), e.get("hi"), a)
        if out is None:
            continue
        # lead-time at this snapshot
        hours = None
        try:
            asof = datetime.fromisoformat(e["asof_utc"].replace("Z", "+00:00"))
            close = datetime.fromisoformat(date_iso + "T06:00:00+00:00") + timedelta(days=1)
            hours = (close - asof).total_seconds() / 3600.0
        except Exception:
            pass
        model_b = (fp - out) ** 2
        market_b = ((mid - out) ** 2) if (mid is not None and 0 < mid < 1) else None
        mode = e.get("mode", "?")
        rows[(mode, lead_bucket(hours))].append((model_b, market_b))
        rows_city[(mode, city)].append((model_b, market_b))
        n_scored += 1

    # Persist newly-fetched actuals atomically; skip entirely under --no-fetch (get_actual never
    # mutates the cache without fetching, so it's provably unchanged) — the cron's 6x/day --no-fetch
    # run must NOT truncate-rewrite the SHARED cache (a mid-write kill → silent empty → 429 storm).
    if not args.no_fetch:
        try:
            import os as _os
            _tmp = f"{crps_report.ACTUALS_CACHE}.tmp"
            with open(_tmp, "w") as _f:
                json.dump(cache, _f)
            _os.replace(_tmp, crps_report.ACTUALS_CACHE)
        except Exception:
            pass

    if not n_scored:
        print("No scorable shadow predictions yet (need settled days with actuals).")
        return 1

    def summarize(pairs):
        mb = [m for m, k in pairs]
        kb = [k for m, k in pairs if k is not None]
        model = sum(mb) / len(mb)
        market = (sum(kb) / len(kb)) if kb else None
        skill = ((market - model) / market) if (market and market > 0) else None
        return len(mb), model, market, skill

    print(f"Shadow model-vs-market Brier — {n_scored} settled shadow predictions\n")
    for mode in ("legacy", "forecast"):
        allp = [p for (m, lb), ps in rows.items() if m == mode for p in ps]
        if not allp:
            continue
        n, model, market, skill = summarize(allp)
        verdict = ("model BEATS market — edge" if skill and skill > 0
                   else "market beats model — no edge" if skill is not None else "—")
        print(f"[{mode.upper()}]  n={n}  model Brier={model:.4f}  "
              f"market Brier={market:.4f}  skill_vs_market={skill:+.1%}  ({verdict})"
              if market else f"[{mode.upper()}]  n={n}  model Brier={model:.4f}  (no market mids)")
        for lb in ("0-6h", "6-12h", "12-18h", "18-24h", "24h+"):
            ps = rows.get((mode, lb))
            if ps:
                n2, mo, ma, sk = summarize(ps)
                if ma:
                    print(f"     {lb:>6}: n={n2:>4}  model={mo:.4f}  market={ma:.4f}  skill={sk:+.1%}")
        print()

    # ── Per-city breakdown (forecast mode): WHERE does the model beat the market? ──
    fc_cities = sorted({c for (m, c) in rows_city if m == "forecast"})
    if fc_cities:
        print("Per-city skill (FORECAST mode) — model vs market (✓ = model beats market):")
        for c in fc_cities:
            n2, mo, ma, sk = summarize(rows_city[("forecast", c)])
            if ma:
                tag = "✓" if (sk is not None and sk > 0) else " "
                print(f"   {tag} {c:<6} n={n2:>4}  model={mo:.4f}  market={ma:.4f}  skill={sk:+.1%}")
        print()

    if args.log:
        def _summ(pairs):
            if not pairs:
                return None
            n, model, market, skill = summarize(pairs)
            return {"n": n, "model_brier": round(model, 5),
                    "market_brier": round(market, 5) if market is not None else None,
                    "skill_vs_market": round(skill, 4) if skill is not None else None}
        rec = {
            "asof_utc": datetime.now(timezone.utc).isoformat(),
            "n_scored": n_scored,
            "legacy": _summ([p for (m, lb), ps in rows.items() if m == "legacy" for p in ps]),
            "forecast": _summ([p for (m, lb), ps in rows.items() if m == "forecast" for p in ps]),
            "by_city_forecast": {c: _summ(rows_city[("forecast", c)]) for c in fc_cities},
        }
        try:
            with open(SHADOW_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"[shadow] appended summary to {SHADOW_LOG}")
        except Exception as e:
            print(f"[shadow] failed to append to {SHADOW_LOG}: {e}", file=sys.stderr)

    print("Reminder: edge requires model Brier < market Brier (positive skill). "
          "Climatology base-rate Brier ≈ p(1-p); the market is the bar to beat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

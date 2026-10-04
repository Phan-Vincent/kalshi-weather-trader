#!/usr/bin/env python3
"""bin/spread_cushion_calibration.py — pre-registered, evidence-based trigger to calibrate
KALSHI_WEATHER_PREMIUM_MIN_SPREAD (the adverse-markout cushion gate) from LIVE data.

The gate is shipped but OFF (scanner.premium_quote_price): we don't yet know the markout-by-spread
relationship, and picking a threshold blind could either do nothing or zero out the arm. This tool
watches the accruing spread_cents_at_post telemetry + realized net EV/ct and — ONLY when the data
CONFIDENTLY shows a spread band below which premium fills lose money — fires ONE review alert
recommending the threshold. No rush by design: it reports accrual every cycle and never rules on a
bucket until it is decisive.

PRE-REGISTERED ESTIMAND: realized net EV/ct (fee-adjusted, maker) of attributed weather premium-live
fills, joined to their spread_cents_at_post, bucketed by spread, EVENT-CLUSTERED (series+city+date).
PRE-REGISTERED CONFIDENCE: the ANYTIME-VALID confidence sequence (compare_variants.asymp_confseq) —
valid under the continuous per-cycle monitoring this tool does, so a bucket is 'confidently losing'
the instant its CS upper bound < 0 (with >= MIN_EVENTS events), no fixed-n wait, no peeking penalty.
PRE-REGISTERED RULE: recommend PREMIUM_MIN_SPREAD = hi+1 of the widest CONTIGUOUS run, starting from
the tightest bucket, of confidently-losing buckets. A bucket that is not-yet-decisive stops the run
(we never recommend cutting a band we haven't confidently measured). No losing tight bucket → no cut.

Report + alert only (a human sets the env). The recommendation is frozen immutably once first fired.

Usage:  python3 bin/spread_cushion_calibration.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import _read_jsonl, _event, cluster_bootstrap_ci, asymp_confseq  # noqa: E402
from data.weather_data import is_weather_ticker                                        # noqa: E402
from trader.orders import fee_per_contract_cents                                       # noqa: E402

STATE_DIR = ROOT / "state" / "live-premium"
STATE_PATH = STATE_DIR / "spread-cushion-calibration.json"
MIN_EVENTS = 10                                     # no bucket ruled on fewer independent events
# Top bucket is open-ended so a raised KALSHI_WEATHER_PREMIUM_MAX_SPREAD (default 12) never silently
# drops wide-spread fills from the calibration.
_BUCKETS = [(1, 2, "1-2c"), (3, 4, "3-4c"), (5, 6, "5-6c"), (7, 8, "7-8c"), (9, 10 ** 9, "9+c")]
_NOW = lambda: datetime.now(timezone.utc).isoformat()   # noqa: E731


def _bucket_label(spread: int):
    for lo, hi, lab in _BUCKETS:
        if lo <= spread <= hi:
            return lab
    return None                                      # spread < 1 (shouldn't happen)


def posts_by_key(life: list[dict]) -> dict:
    """(ticker, side) -> [(limit_price_cents, spread_cents_at_post), …] in chronological (file) order,
    for OUR posted_live quotes that carry the spread telemetry."""
    out: dict = defaultdict(list)
    for r in life:
        if r.get("event") != "posted_live":
            continue
        sp, px = r.get("spread_cents_at_post"), r.get("limit_price_cents")
        if (isinstance(sp, (int, float)) and sp > 0 and isinstance(px, (int, float))
                and is_weather_ticker(r.get("ticker", ""))):
            out[(r.get("ticker"), r.get("side"))].append((int(px), int(sp)))
    return out


def _spread_for_fill(posts: list, entry_cents: int):
    """Attribute the fill to the quote it ACTUALLY filled at, not the first post. A resting quote is
    cancelled+re-posted at a DIFFERENT spread when the book moves (quote-staleness refresh), and a
    maker fills at its limit — so the settlement's entry_cents identifies the filling quote. Match on
    price (latest such post); fall back to the nearest price within 1c (Kalshi entry rounding); else
    None — never guess a spread we can't attribute."""
    if not posts:
        return None
    exact = [sp for (px, sp) in posts if px == entry_cents]
    if exact:
        return exact[-1]
    px, sp = min(posts, key=lambda ps: abs(ps[0] - entry_cents))
    return sp if abs(px - entry_cents) <= 1 else None


def net_by_spread_bucket(settle: list[dict], pk: dict) -> dict:
    """bucket_label -> [(event, net_ev_per_ct)] for attributed weather fills, each bucketed by the
    spread of the quote it actually filled at."""
    buckets: dict = defaultdict(list)
    for s in settle:
        if (s.get("qty") or 0) <= 0 or s.get("entry_cents") in (None, 0) or s.get("gap_settled"):
            continue
        if not is_weather_ticker(s.get("ticker", "")):
            continue
        sp = _spread_for_fill(pk.get((s.get("ticker"), s.get("side"))) or [], int(s["entry_cents"]))
        if sp is None:
            continue                                # no attributable spread for this fill yet
        lab = _bucket_label(sp)
        if lab is None:
            continue
        net = s["pnl_cents"] / s["qty"] - fee_per_contract_cents(s["qty"], s["entry_cents"], maker=True)
        buckets[lab].append((_event(s.get("ticker", "")), net))
    return buckets


def _cs_neg(cs) -> bool:
    _, lo, hi = cs
    return hi is not None and lo != hi and hi < 0        # anytime-valid, non-degenerate, upper < 0


def evaluate(state_dir: Path = None) -> dict:
    sd = state_dir or STATE_DIR
    life = _read_jsonl(sd / "maker-lifecycle.jsonl")
    settle = _read_jsonl(sd / "settlement-log.jsonl")
    buckets = net_by_spread_bucket(settle, posts_by_key(life))

    rows = []
    for lo, hi, lab in _BUCKETS:
        items = buckets.get(lab, [])
        n_ev = len({e for e, _ in items})
        ci = cluster_bootstrap_ci(items) if items else (None, None, None)
        cs = asymp_confseq(items) if items else (None, None, None)
        rows.append({"bucket": lab, "lo": lo, "hi": hi, "n_fills": len(items), "n_events": n_ev,
                     "ci": ci, "cs": cs,
                     "confidently_losing": n_ev >= MIN_EVENTS and _cs_neg(cs)})

    # Rule: widest contiguous run of confidently-losing buckets from the tightest; a not-yet-decisive
    # bucket STOPS the run (never recommend cutting a band we haven't confidently measured). Capped at
    # max_spread+1 so the open-ended top bucket can't yield a nonsensical min_spread (all-losing →
    # cap = "cut everything", surfaced via all_buckets_losing).
    reco_cap = int(os.environ.get("KALSHI_WEATHER_PREMIUM_MAX_SPREAD", "12")) + 1
    recommend = 0
    for r in rows:
        if r["confidently_losing"]:
            recommend = min(r["hi"] + 1, reco_cap)
        else:
            break
    all_losing = all(r["confidently_losing"] for r in rows) and any(r["n_events"] for r in rows)

    total_ev = sum(r["n_events"] for r in rows)
    total_fills = sum(r["n_fills"] for r in rows)
    return {"buckets": rows, "recommend_min_spread": recommend, "all_buckets_losing": all_losing,
            "total_events_with_spread": total_ev, "total_fills_with_spread": total_fills}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "tool_version": "1.0", "created_utc": _NOW(),
        "estimand": {
            "metric": "realized net EV/ct (fee-adj, maker) by spread_cents_at_post bucket",
            "unit": "event-clustered (series+city+date)",
            "confidence": "anytime-valid confidence sequence (asymp_confseq), CS upper<0 = confidently losing",
            "min_events_per_bucket": MIN_EVENTS,
            "rule": "recommend PREMIUM_MIN_SPREAD = hi+1 of the widest contiguous confidently-losing "
                    "run from the tightest bucket; a not-yet-decisive bucket stops the run",
            "source": "state/live-premium/{maker-lifecycle,settlement}-log.jsonl",
        },
        "review_triggered": None,   # frozen immutably once a confident cut first emerges
    }


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def _fmt(ci):
    if not ci or ci[0] is None:
        return "n/a"
    if ci[1] is None or ci[2] is None:
        return f"{ci[0]:+.2f}¢ [n/a]"      # point defined, interval not yet (single-event bucket)
    return f"{ci[0]:+.2f}¢ [{ci[1]:+.2f}, {ci[2]:+.2f}]"


def main() -> int:
    r = evaluate()
    state = _load_state()
    state["last_run_utc"] = _NOW()
    state["last_recommendation"] = r["recommend_min_spread"]

    print("[spread-cushion-cal] premium-live net EV/ct by spread_cents_at_post (event-clustered; "
          "anytime-valid CS gates 'confidently losing')")
    print(f"  telemetry so far: {r['total_fills_with_spread']} fills / {r['total_events_with_spread']} "
          f"events with a spread tag (needs ≥{MIN_EVENTS}/bucket to rule)")
    print(f"  {'bucket':>7} {'events':>6} {'fills':>6}   net EV/ct (BCa 95%)              anytime CS")
    for b in r["buckets"]:
        flag = "  ← confidently losing" if b["confidently_losing"] else ""
        print(f"  {b['bucket']:>7} {b['n_events']:>6} {b['n_fills']:>6}   {_fmt(b['ci']):<30} "
              f"{_fmt(b['cs'])}{flag}")

    triggered = state.get("review_triggered")
    if r["recommend_min_spread"] > 0 and triggered is None:
        rec = r["recommend_min_spread"]
        note = ("ALL measured buckets are confidently losing — the premium strategy itself may be "
                "unviable; re-evaluate the arm, not just the gate." if r["all_buckets_losing"]
                else f"set KALSHI_WEATHER_PREMIUM_MIN_SPREAD={rec} on premium-live (reject spread<{rec}).")
        state["review_triggered"] = {"utc": _NOW(), "recommend_min_spread": rec,
                                     "all_buckets_losing": r["all_buckets_losing"], "note": note}
        print(f"\n  ⚑ REVIEW TRIGGERED: data now confidently supports a cushion cut → {note}")
        try:
            from trader.notify import alert
            alert(f"Spread-cushion calibration ready: {note} (evidence: net EV/ct anytime-CS upper<0 "
                  f"for spread<{rec}, ≥{MIN_EVENTS} events/bucket). Report only; your call.",
                  key="spread_cushion_review")
        except Exception as e:
            print(f"  [spread-cushion-cal] alert dispatch failed: {e}", file=sys.stderr)
    elif triggered is not None:
        print(f"\n  review already triggered {triggered['utc']}: "
              f"recommend PREMIUM_MIN_SPREAD={triggered['recommend_min_spread']} (locked)")
    else:
        print(f"\n  no confident cut yet — accruing. (recommendation would be "
              f"PREMIUM_MIN_SPREAD={r['recommend_min_spread']}; 0 = keep gate off)")

    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

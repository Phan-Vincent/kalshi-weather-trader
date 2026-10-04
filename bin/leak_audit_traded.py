#!/usr/bin/env python3
"""bin/leak_audit_traded.py — ROADMAP P0-1, closed on the TRADED bins.

`bin/leak_audit.py` runs the same-day-skill-vs-leak audit over the whole SHADOW forecast log (every
market logged every cycle). This tool runs the identical hours-before-extreme stratification over the
bins we ACTUALLY QUOTED — the selected population where the headline "+0.041 same-day Brier edge" was
measured (edge-inventory front, `state/paper/…`). It is the decision-time replay P0-1 calls for:

  For each settled traded bin (per-arm settlement-log):
    • decision time  t0 = opened_utc
    • model prob     p_model = fair_prob_at_open           (what the model actually quoted, YES-side)
    • outcome        y = 1 if settlement_result == "yes"   (Kalshi's authoritative resolution)
    • market prob    p_mkt = forecast-log market_mid at the snapshot nearest t0 (≤45min), else the
                     entry_cents-implied YES price          (--market forces one source, for robustness)
  score model_b=(p_model-y)^2, market_b=(p_mkt-y)^2, Δbrier=market_b-model_b, and stratify by
  hours-before the day's extreme with an event-clustered BCa CI — reusing leak_audit's machinery.

The tell is the same: skill present with ≥6h forecast lead ⇒ GENUINE EDGE; skill only <2h before / after
the extreme ⇒ the same-day edge is an outcome LEAK. A SAME-DAY-ONLY section isolates exactly the slice
that produced the +0.041, because that is the number under audit.

CAVEAT — selection: traded bins are chosen where model diverges from market, so the OVERALL Brier level
is selection-inflated and must NOT be read as a tradeable edge. The leak inference survives selection: it
keys off the LEAD-TIME GRADIENT within the selected set (a leak loads the near-extreme strata; genuine
skill shows up with real lead), not the level. Read-only.

Usage:
    python3 bin/leak_audit_traded.py                       # arm=paper, market=mid(+entry fallback)
    python3 bin/leak_audit_traded.py --arm paper,paper-caledge
    python3 bin/leak_audit_traded.py --market entry        # robustness: market = our fill price only
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import leak_audit as la  # noqa: E402  (share stratification + verdict + formatting)
from leak_audit import _STRATA, _row, _fmt_ci, _asof_local_date, _DEFAULT_CUTOFF  # noqa: E402
from crps_report import parse_ticker  # noqa: E402  (recover city/type/date when market_parsed absent)
from data.weather_data import CITY_ALIASES  # noqa: E402

STATE = ROOT / "state"
FLOG = STATE / "forecast-log.jsonl"
MAX_JOIN_GAP_MIN = 45.0   # a forecast-log market_mid farther than this from t0 is not "decision-time"


def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_arm(arm: str) -> list[dict]:
    p = STATE / arm / "settlement-log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in open(p):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _market_mid_index(tickers: set) -> dict:
    """{ticker: sorted [(asof_dt, market_mid)]} from the forecast log, restricted to traded tickers."""
    idx = {}
    if not FLOG.exists():
        return idx
    for line in open(FLOG):
        if '"market_mid"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        tk = o.get("ticker")
        if tk not in tickers:
            continue
        mm, a = o.get("market_mid"), o.get("asof_utc")
        if mm is None or not (0 < mm < 1):
            continue
        adt = _parse_dt(a)
        if adt:
            idx.setdefault(tk, []).append((adt, mm))
    for lst in idx.values():
        lst.sort(key=lambda x: x[0])
    return idx


def _nearest_mid(idx: dict, ticker: str, t0: datetime):
    """market_mid at the forecast-log snapshot closest to t0; (mid, gap_min) or (None, None)."""
    lst = idx.get(ticker)
    if not lst:
        return None, None
    times = [a for a, _ in lst]
    i = bisect_left(times, t0)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(lst):
            gap = abs((lst[j][0] - t0).total_seconds())
            if best is None or gap < best[1]:
                best = (lst[j][1], gap)
    return (best[0], best[1] / 60.0) if best else (None, None)


def _entry_implied_yes(rec: dict):
    """Market-implied YES prob from our fill price. side=yes → entry/100; side=no → 1-entry/100."""
    ec = rec.get("entry_cents")
    if ec is None:
        return None
    p = max(0.0, min(1.0, ec / 100.0))
    return p if rec.get("side") == "yes" else (1.0 - p)


def _city_type_date(rec: dict):
    """(canonical_city, city_code, mtype, date_iso). Prefer market_parsed; fall back to the ticker for
    older records (pre-market_parsed, i.e. the June pre-guard era)."""
    mp = rec.get("market_parsed") or {}
    code, mtype, date_iso = mp.get("city_code"), mp.get("market_type"), mp.get("date_iso")
    if not mtype or not date_iso or not code:
        tp = parse_ticker(rec.get("ticker", ""))
        if tp:
            code, mtype, date_iso = tp[0], tp[1], tp[2]
    return CITY_ALIASES.get(code or "", code or ""), code, mtype, date_iso


def build_scored(recs: list[dict], idx: dict, market_src: str, since_dt: datetime | None):
    """Return (scored, meta). scored rows match leak_audit's shape so analyze() applies unchanged."""
    scored = []
    meta = {"n": 0, "skipped": 0, "pre_cutoff": 0, "mkt_from_mid": 0, "mkt_from_entry": 0, "no_market": 0}
    for r in recs:
        city, code, mtype, date_iso = _city_type_date(r)
        p_model = r.get("fair_prob_at_open")
        sr = r.get("settlement_result")
        t0 = _parse_dt(r.get("opened_utc"))
        if p_model is None or sr not in ("yes", "no") or not mtype or not date_iso or t0 is None:
            meta["skipped"] += 1
            continue
        if since_dt is not None and t0 <= since_dt:   # trade made by the pre-LEAK-1-guard (leaky) model
            meta["pre_cutoff"] += 1
            continue
        y = 1.0 if sr == "yes" else 0.0
        mid, gap = _nearest_mid(idx, r.get("ticker", ""), t0)
        use_mid = mid is not None and gap is not None and gap <= MAX_JOIN_GAP_MIN
        if market_src == "entry":
            p_mkt = _entry_implied_yes(r)
        elif market_src == "mid":
            p_mkt = mid if use_mid else None
        else:  # auto: tight mid, else entry
            p_mkt = mid if use_mid else _entry_implied_yes(r)
        if p_mkt is None or not (0 <= p_mkt <= 1):
            meta["no_market"] += 1
            continue
        meta["mkt_from_mid" if use_mid and market_src != "entry" else "mkt_from_entry"] += 1
        ext = la._local_instant_utc(date_iso, la.EXTREME_LOCAL_HOUR.get(mtype, 17.0), city)
        hbe = (ext - t0).total_seconds() / 3600.0
        hts = (la._settle_instant_utc(date_iso, city) - t0).total_seconds() / 3600.0
        scored.append({"event": f"{code}-{date_iso}", "model_b": (p_model - y) ** 2,
                       "market_b": (p_mkt - y) ** 2, "hbe": hbe, "hts": hts,
                       "same_day": date_iso <= _asof_local_date(t0, city)})
        meta["n"] += 1
    return scored, meta


def _overall_skill(scored):
    """Reproduce the headline model-vs-market Brier edge on this population (sanity vs the +0.023/+0.041)."""
    return la._summ([(r["event"], r["model_b"], r["market_b"]) for r in scored])


def _render(title, scored, min_events):
    res = la.analyze(scored, min_events)
    print(f"\n  === {title} (n={len(scored)}) ===")
    for label, _, _ in _STRATA:
        print(_row(label, res["strata"][label]))
    print(_row("LEAKFREE", res["clean"]), " (≥6h before extreme)")
    print(_row("SUSPECT", res["suspect"]), " (<2h before extreme)")
    ov = _overall_skill(scored)
    print(f"  overall Δbrier(mkt-model) {_fmt_ci(ov.get('ci_diff'))}  "
          f"(model {ov['model_brier']:.4f} vs market {ov['market_brier']:.4f}, "
          f"skill {ov['skill']:+.1%})" if ov.get("model_brier") is not None else "")
    print(f"  VERDICT (P0-1): {res['verdict']}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="P0-1 leak audit on TRADED bins (decision-time replay)")
    ap.add_argument("--arm", default="paper", help="comma-sep arm dir(s) under state/ (default: paper)")
    ap.add_argument("--market", choices=["auto", "mid", "entry"], default="auto",
                    help="market prob source: auto=tight forecast-log mid else entry (default); "
                         "mid=forecast-log only; entry=fill-price implied only (robustness)")
    ap.add_argument("--min-events", type=int, default=la.MIN_EVENTS_FOR_VERDICT,
                    help="leak-free events required for a verdict (P0-1 gate = 150)")
    ap.add_argument("--all", action="store_true",
                    help="include pre-LEAK-1-guard trades (the leaky-model era); default excludes them")
    args = ap.parse_args()

    since_dt = None if args.all else datetime.fromisoformat(_DEFAULT_CUTOFF)

    arms = [a.strip() for a in args.arm.split(",") if a.strip()]
    recs = []
    for a in arms:
        r = _load_arm(a)
        if not r:
            print(f"[leak-audit-traded] WARNING: no records for arm '{a}'")
        recs += r
    if not recs:
        print("[leak-audit-traded] no traded records found")
        return 1

    idx = _market_mid_index({r.get("ticker") for r in recs if r.get("ticker")})
    scored, meta = build_scored(recs, idx, args.market, since_dt)

    window = "ALL trades (incl. pre-guard leaky era)" if args.all else f"post-LEAK-1-guard (opened>{_DEFAULT_CUTOFF})"
    print(f"[leak-audit-traded] P0-1 on TRADED bins — arm(s)={','.join(arms)}, market={args.market}, "
          f"window={window},\n  extreme@ HIGH {la.EXTREME_LOCAL_HOUR['daily_high']:.0f}:00 / LOW "
          f"{la.EXTREME_LOCAL_HOUR['daily_low']:.0f}:00 station-local")
    print(f"  scored {meta['n']} traded bins "
          f"(market: {meta['mkt_from_mid']} decision-time mid, {meta['mkt_from_entry']} entry-implied; "
          f"{meta['no_market']} no-market dropped, {meta['pre_cutoff']} pre-guard excluded, "
          f"{meta['skipped']} incomplete)")

    _render("ALL TRADED BINS", scored, args.min_events)
    sameday = [r for r in scored if r["same_day"]]
    if sameday:
        _render("SAME-DAY TRADED BINS ONLY  ← the slice that produced the +0.041", sameday, args.min_events)
    priorday = [r for r in scored if not r["same_day"]]
    if priorday:
        _render("PRIOR-DAY TRADED BINS ONLY (≥1 day ahead — cannot leak)", priorday, args.min_events)

    print("\n  (GENUINE EDGE ⇒ build Phase 1; LEAK-DOMINATED ⇒ same-day 'edge' is a leak, tradeable ≈ 0)")
    print("  NOTE: traded bins are SELECTED — read the lead-time GRADIENT, not the overall level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

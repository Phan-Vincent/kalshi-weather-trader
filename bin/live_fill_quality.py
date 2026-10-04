#!/usr/bin/env python3
"""bin/live_fill_quality.py — early-warning watchdog on LIVE fill quality.

The 100-fill bar only matters if those fills CONFIRM the paper edge: fill-rate ≈ the model
AND positive markout-after-rebate. This flags adverse selection / negative realized edge EARLY
(well before 100) so we don't burn weeks collecting bad fills. Read-only; alert-only — it never
halts or resizes (the operator + kill chain decide).

  python3 bin/live_fill_quality.py [--live-dir state/live-premium] [--json]
Env: KALSHI_WEATHER_FILLQ_MIN_N (12), KALSHI_WEATHER_FILLQ_MARKOUT_C (-2.0)
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
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from reconcile_fills import live_fill_stats           # noqa: E402  (fills attributed to OUR quotes)
try:
    from tail_edge import wilson                       # noqa: E402  (same CI as the thesis script)
except Exception:  # pragma: no cover
    wilson = None

MIN_N = int(os.environ.get("KALSHI_WEATHER_FILLQ_MIN_N", "12"))
MARKOUT_C = float(os.environ.get("KALSHI_WEATHER_FILLQ_MARKOUT_C", "-2.0"))
# Markout horizon: measure SHORT-TERM post-fill drift (adverse selection), NOT the settlement
# outcome — observations outside this age window (e.g. ~22h near-settlement marks) are excluded.
MARKOUT_LO_H = float(os.environ.get("KALSHI_WEATHER_FILLQ_MARKOUT_LO_H", "2"))
MARKOUT_HI_H = float(os.environ.get("KALSHI_WEATHER_FILLQ_MARKOUT_HI_H", "8"))
_STATE_FILE = "fill-quality-state.json"
_RANK = {"OK": 0, "ACCUMULATING": 0, "WATCH": 1, "ADVERSE": 2}


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _our_posted_tickers(live_dir: Path) -> set:
    """Tickers we actually posted as live maker quotes — so legacy/whole-account positions (e.g.
    the KXUSAIRANAGREEMENT contract the sync pulls in) don't contaminate the markout. Mirrors the
    attribution in reconcile_fills.live_fill_stats."""
    out = set()
    for r in _read_jsonl(live_dir / "maker-lifecycle.jsonl"):
        if r.get("event") == "posted_live" and r.get("ticker"):
            out.add(r["ticker"])
    return out


def _mean_markout(mk: list[dict], our_tickers=None):
    """Mean SHORT-HORIZON post-fill mid drift (cur_mid − fill_px) — adverse selection, NOT the
    settlement outcome. Per position, take the observation whose age is within [LO, HI] hours and
    closest to the window centre (so the ~22h near-settlement marks, which just reflect win/loss,
    are excluded). Attribute to OUR posted quotes only; drop impossible fill prices. Negative ⇒
    the market moved against our fills shortly after we filled = adverse selection."""
    target = (MARKOUT_LO_H + MARKOUT_HI_H) / 2.0
    chosen: dict = {}  # key -> (distance_to_target, drift)
    for r in mk:
        if r.get("source") == "sync_fill_detect":   # at-fill diagnostic snaps — never a live instrument
            continue
        cm, fp, age = r.get("cur_mid"), r.get("fill_px"), r.get("age_hours")
        tk = r.get("ticker")
        if not (isinstance(cm, (int, float)) and isinstance(fp, (int, float))
                and isinstance(age, (int, float))):
            continue
        if not (1 <= fp <= 99):                          # drop garbage entry prices (e.g. 101)
            continue
        if not (2 <= cm <= 98):                          # cur_mid pinned at a 0/100 rail = the
            continue                                     # settlement outcome (same-day weather
            #                                              resolves intraday), NOT a live mid
        if not (MARKOUT_LO_H <= age <= MARKOUT_HI_H):    # short-horizon only, not settlement
            continue
        if our_tickers is not None and tk not in our_tickers:  # our quotes only
            continue
        key = (tk, r.get("side"), r.get("paper_order_id") or r.get("opened_utc"))
        dist = abs(age - target)
        if key not in chosen or dist < chosen[key][0]:
            chosen[key] = (dist, cm - fp)
    drifts = [d for _, d in chosen.values()]
    return ((sum(drifts) / len(drifts)) if drifts else None, len(drifts))


def _realized_edge(settle: list[dict]):
    """EV/contract after maker fee on settled live fills, with a Wilson WR CI."""
    # Defensive: drop malformed settlement rows (qty<=0 or entry_cents None). (audit 2026-06-25)
    settle = [r for r in settle if (r.get("qty") or 0) > 0 and r.get("entry_cents") is not None]
    if not settle:
        return None
    q = sum(r.get("qty", 0) for r in settle) or 1
    pnl = sum(r.get("pnl_cents", 0) for r in settle)
    wins = sum(1 for r in settle if (r.get("pnl_cents") or 0) > 0)
    ci = wilson(wins, len(settle)) if wilson else (None, None)
    # pnl_cents is ALREADY net of fees/rebate (Kalshi net realized P&L for live rows). The old
    # code subtracted fee_per_contract_cents(maker=True) — a NEGATIVE rebate — re-crediting the
    # rebate a second time and inflating ev_after_fee_c ~+0.47c/ct, which could flip a truly
    # negative (ADVERSE) book to positive right inside this gate (audit 2026-07-06).
    return {"n": len(settle), "ev_after_fee_c": pnl / q,
            "win_rate": wins / len(settle), "wr_ci": ci}


def evaluate(live_dir: Path) -> dict:
    """Pure: compute the live-fill quality status (no I/O side effects, no alerts)."""
    stats = live_fill_stats(live_dir)
    filled = int(stats.get("filled") or 0)
    mk_mean, mk_n = _mean_markout(_read_jsonl(live_dir / "markout-log.jsonl"),
                                  our_tickers=_our_posted_tickers(live_dir))
    edge = _realized_edge(_read_jsonl(live_dir / "settlement-log.jsonl"))

    flags: list[str] = []
    realized_bad = bool(edge and edge["n"] >= MIN_N and edge["ev_after_fee_c"] < 0)
    markout_bad = (mk_mean is not None and mk_mean <= MARKOUT_C)
    if filled < MIN_N:
        status = "ACCUMULATING"
    elif realized_bad:
        # REALIZED money is negative — the real adverse signal for a hold-to-settlement maker -> ADVERSE.
        status = "ADVERSE"
        flags.append(f"negative realized edge {edge['ev_after_fee_c']:+.1f}c/ct (n={edge['n']})")
        if markout_bad:
            flags.append(f"+ adverse markout {mk_mean:+.1f}c")
    elif markout_bad or (mk_mean is not None and mk_mean < 0) or (edge and edge["ev_after_fee_c"] < 1.0):
        # markout / thin-edge is WATCH only — NOT ADVERSE while realized EV is non-negative. On same-day
        # weather markets, markout (cur_mid - fill at 2-8h) measures the contract resolving toward its
        # SETTLEMENT outcome intraday, not microstructure adverse selection, so markout-alone cries wolf
        # (validation 2026-06-28). ADVERSE now requires realized<0; markout demands BOTH realized<0 AND
        # markout<=-2c to escalate. Realized EV-after-fee is the gate.
        status = "WATCH"
        flags.append(f"markout {mk_mean:+.1f}c — settlement drift, watch only" if markout_bad
                     else "borderline (markout/edge thin)")
    else:
        status = "OK"

    return {
        "status": status, "flags": flags,
        "posted": stats.get("posted"), "filled": filled,
        "fill_rate": stats.get("fill_rate"), "mean_slippage_c": stats.get("mean_slippage"),
        "mean_markout_c": mk_mean, "markout_n": mk_n,
        "realized": edge,
        "toward_30": min(filled, 30), "toward_100": min(filled, 100),
    }


def _load_prev(live_dir: Path):
    p = live_dir / _STATE_FILE
    if p.is_file():
        try:
            return json.loads(p.read_text()).get("status")
        except Exception:
            return None
    return None


def _save(live_dir: Path, status: str) -> None:
    try:
        p = live_dir / _STATE_FILE
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"status": status, "updated_utc": datetime.now(timezone.utc).isoformat()}, indent=2))
        tmp.replace(p)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Live fill-quality watchdog (alert-only)")
    ap.add_argument("--live-dir", default="state/live-premium")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    live_dir = ROOT / args.live_dir if not Path(args.live_dir).is_absolute() else Path(args.live_dir)

    res = evaluate(live_dir)
    if args.json:
        print(json.dumps(res))
    else:
        print(f"LIVE FILL QUALITY: {res['status']}  "
              f"(filled={res['filled']} / posted={res['posted']}; toward 30/100)")
        if res["mean_markout_c"] is not None:
            print(f"  markout = {res['mean_markout_c']:+.1f}c (n={res['markout_n']}; <0 = adverse)")
        if res["realized"]:
            e = res["realized"]
            print(f"  realized EV-after-fee = {e['ev_after_fee_c']:+.1f}c/ct (n={e['n']}, WR={e['win_rate']*100:.0f}%)")
        for f in res["flags"]:
            print(f"  WARNING: {f}")

    # Transition-gated alert: ping only when quality WORSENS into WATCH/ADVERSE (mirror healthcheck).
    prev, cur = _load_prev(live_dir), res["status"]
    try:
        if cur in ("WATCH", "ADVERSE") and _RANK.get(cur, 0) > _RANK.get(prev, 0):
            from trader.notify import alert
            alert(f"WARNING: LIVE FILL QUALITY -> {cur}: " + "; ".join(res["flags"])[:200]
                  + f" (filled={res['filled']}). Localize side/city: "
                  + f"bin/fill_edge_breakdown.py --live-dir {args.live_dir}  "
                  + f"(bands: bin/premium_fill_report.py --state-dir {args.live_dir})",
                  key="live_fill_quality", dedup_seconds=3600)
        elif prev in ("WATCH", "ADVERSE") and cur == "OK":
            from trader.notify import alert
            alert(f"live fill quality recovered -> OK (filled={res['filled']})",
                  key="live_fill_quality", dedup_seconds=3600)
    except Exception as e:
        print(f"[fill-quality] alert dispatch failed: {e}", file=sys.stderr)
    _save(live_dir, cur)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # never break the cycle on a watchdog error
        print(f"[fill-quality] non-fatal: {e}", file=sys.stderr)
        raise SystemExit(0)

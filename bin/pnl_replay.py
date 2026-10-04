#!/usr/bin/env python3
"""bin/pnl_replay.py — price-aware forward P&L replay: the bridge from Brier skill to DOLLARS.

Quant review 2026-07-01 experiment #9. No existing backtest turns model skill into money: v2/v3 are
look-ahead + dead, mm_sim assumes guaranteed maker fills + costless flatten, and shadow/CRPS score
Brier only. This replays every eligible forecast-log row (fair_prob + market_mid + a settled outcome)
through the LIVE taker edge-gate (trader.orders.compute_post_fee_edge), simulates a realistic fill
(Kalshi fee + an ASSUMED spread + optional adverse markout), settles at 100¢/0¢, and reports the
event-clustered $/contract edge — swept across the assumption axes that actually drive the number.

CENTRAL CAVEAT: only market_mid is logged (no bid/ask/spread), so every fill price is a MODELED
assumption — hence spread is a first-class sweep axis, not a constant. And this is a COUNTERFACTUAL
model-arm P&L: the live arm prices off the BOOK (join-bid), not the model, so this is NOT the live
arm's realized P&L (reconciled +$7.19/77 closed) — it answers "if we had taken every model-edge
signal at an assumed spread, would it have paid?". Read-only; writes no ledger.

Usage:  python3 bin/pnl_replay.py [--allow-fetch] [--leak1-cutoff]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import json  # noqa: E402
from crps_report import parse_ticker, get_actual, load_cache, STATE  # noqa: E402
from shadow_score import outcome as resolve_outcome  # noqa: E402
from compare_variants import _event, cluster_bootstrap_ci, _read_jsonl  # noqa: E402
from leakfree_skill import _asof_local_date  # noqa: E402
from persistence_leak_probe import old_leak  # noqa: E402  (exp-#8 same-day persistence-leak filter)
from data.weather_data import is_weather_ticker, CITY_ALIASES  # noqa: E402
from trader.orders import compute_post_fee_edge, fee_per_contract_cents  # noqa: E402
from live_fill_quality import _read_jsonl as _rj, _our_posted_tickers, _mean_markout  # noqa: E402

FLOG = STATE / "forecast-log.jsonl"
QTY = 10                       # live gate uses qty=10 for the fee tier (fees ~flat per ct here)
SLIPPAGE_CENTS = 0.84          # FILL_SLIPPAGE_CENTS (paper_book) — a separate maker debit
MIN_EVENTS = 10                # matches leakfree_skill; no ruling below this many events
REP_LEAD_H = 18.0              # per-contract representative snapshot: the model's designed trading
                              # window is 12-24h (scanner.py; best Brier ~18-24h), so pick the
                              # eligible snapshot nearest 18h — not the latest (which biases to the
                              # 0-6h dead zone where the gate is strictest and calibration worst).

LEAD_EDGE = {"0-6h": 25, "6-12h": 15, "12-18h": 15, "18-24h": 10, "24h+": 20}
SPREADS = [2, 4, 8]                                  # tight / typical / wide (ASSUMED — only mid logged)
THRESHOLDS = ["lead_keyed", "flat_10", "flat_20"]
FILLS = ["taker", "maker", "maker+markout"]


def _parse_asof(row):
    try:
        return datetime.fromisoformat(str(row.get("asof_utc")).replace("Z", "+00:00"))
    except Exception:
        return None


def hours_to_close(asof: datetime, market_date: str) -> float:
    close = datetime.fromisoformat(market_date + "T06:00:00+00:00") + timedelta(days=1)
    return (close - asof).total_seconds() / 3600.0


def _lead_bucket(hours: float) -> str:
    for lo, hi, name in [(0, 6, "0-6h"), (6, 12, "6-12h"), (12, 18, "12-18h"),
                         (18, 24, "18-24h"), (24, 1e9, "24h+")]:
        if lo <= hours < hi:
            return name
    return "24h+"


def min_edge_for(threshold: str, hours: float) -> int:
    if threshold == "flat_10":
        return 10
    if threshold == "flat_20":
        return 20
    return LEAD_EDGE.get(_lead_bucket(hours), 20)   # lead_keyed


def decide(fair_prob: float, mid_cents: int, spread: int):
    """Live taker edge decision. Returns (side_bit, ask_cents, edge_cents) or None (invalid book)."""
    half = spread / 2.0
    yes_ask = round(mid_cents + half)
    no_ask = round((100 - mid_cents) + half)
    d = compute_post_fee_edge(fair_prob, yes_ask, no_ask, QTY)
    if d.get("status") == "invalid_input":
        return None
    side_bit = 1 if d["side"] == "yes" else 0
    return side_bit, int(d["limit_price_cents"]), float(d["edge_cents_per_contract"])


def fill_cost_ct(side_bit: int, mid_cents: int, ask_cents: int, fill: str, adverse_markout: float) -> float:
    """Per-contract cost (¢) of the chosen side under a fill model. Invariant: cost = fill_px + fees."""
    if fill == "taker":
        px = ask_cents                                   # pay the ask
        return px + fee_per_contract_cents(QTY, px, maker=False)
    # maker: rest at the side's mid (captures ~half-spread vs taker); fee is a rebate (negative)
    px = mid_cents if side_bit == 1 else (100 - mid_cents)
    cost = px + fee_per_contract_cents(QTY, px, maker=True)
    if fill == "maker+markout":
        cost += SLIPPAGE_CENTS + adverse_markout         # adverse selection drag, debited at entry
    return cost


def replay(rows, actual_of, today_iso, adverse_markout: float, leak1_cutoff=None):
    """Returns {(spread,threshold,fill): [(event, pnl_ct, side_bit), ...]} plus filter counts.

    ONE TRADE PER CONTRACT: the forecast-log re-logs each bin-ticker every scan cycle (~68x). A
    strategy trades a contract at most once, so we collapse each bin-ticker to a single latest-asof
    representative BEFORE scoring — otherwise the estimate is a scan-frequency-weighted mean over log
    rows (heavily re-scanned contracts dominate the headline). Event-clustering then groups the bins."""
    reps: dict = {}                     # full bin-ticker -> latest-asof representative
    awaiting: set = set()               # eligible contracts whose settled actual isn't in the archive yet
    n_sameday = n_oldleak = n_leak1 = 0
    for r in rows:
        mid, fp = r.get("market_mid"), r.get("fair_prob")
        if fp is None or mid is None or r.get("bin_kind") is None:
            continue
        tk = r.get("ticker", "")
        tp = parse_ticker(tk)
        if not tp or not is_weather_ticker(tk):
            continue
        city, mtype, date_iso = tp
        city = CITY_ALIASES.get(city, city)
        if date_iso >= today_iso:
            continue
        asof = _parse_asof(r)
        if asof is None or not (0 < mid < 1):
            continue
        if leak1_cutoff is not None and asof <= leak1_cutoff:
            n_leak1 += 1
            continue
        if date_iso <= _asof_local_date(asof, city):        # same-day forecast → clairvoyant, drop
            n_sameday += 1
            continue
        if old_leak(asof, date_iso):                        # exp-#8 persistence same-day leak, drop
            n_oldleak += 1
            continue
        actual = actual_of(city, mtype, date_iso)
        if actual is None:
            awaiting.add(tk)                                # substantiates the "awaiting actuals" verdict
            continue
        out = resolve_outcome(r.get("bin_kind"), r.get("thr"), r.get("lo"), r.get("hi"), actual)
        if out is None:
            continue
        hrs = hours_to_close(asof, date_iso)
        cur = reps.get(tk)
        if cur is None or abs(hrs - REP_LEAD_H) < abs(cur["hours"] - REP_LEAD_H):   # nearest ~18h
            reps[tk] = {"asof": asof, "fp": fp, "mid_cents": round(mid * 100), "out": out,
                        "ev": _event(tk), "hours": hrs}

    cells = defaultdict(list)
    for d in reps.values():
        for spread in SPREADS:
            dec = decide(d["fp"], d["mid_cents"], spread)
            if dec is None:
                continue
            side_bit, ask, edge = dec
            if not (20 <= ask <= 80):                       # price floor/ceiling (risk.py)
                continue
            for thr in THRESHOLDS:
                if edge < min_edge_for(thr, d["hours"]):    # edge gate
                    continue
                payout = 100 if side_bit == d["out"] else 0
                for fill in FILLS:
                    cost = fill_cost_ct(side_bit, d["mid_cents"], ask, fill, adverse_markout)
                    cells[(spread, thr, fill)].append((d["ev"], payout - cost, side_bit))
    return cells, {"settleable": len(reps), "dropped_sameday": n_sameday,
                   "dropped_oldleak": n_oldleak, "dropped_leak1": n_leak1,
                   "awaiting_actual": len(awaiting)}


def _cell_stats(items):
    if not items:
        return {"n": 0, "n_events": 0, "ci": (None, None, None), "yes_frac": None, "mean_entry": None}
    pnl = [(ev, p) for ev, p, _ in items]
    ci = cluster_bootstrap_ci(pnl)
    yes = sum(1 for _, _, sb in items if sb == 1)
    return {"n": len(items), "n_events": len({ev for ev, _, _ in items}), "ci": ci,
            "yes_frac": yes / len(items)}


# LEAK-1 forecast contamination guard became empirically effective here (quant review §2.4). fair_prob
# BEFORE it is same-day-contaminated → a replay on it measures a clairvoyant model. Leak-free is the
# valid estimand; the contaminated grid is shown only to SIZE the leak (à la leakfree_skill RAW vs LEAK-FREE).
LEAK1_CUTOFF = datetime.fromisoformat("2026-06-30T01:21:00+00:00")


def evaluate(flog: Path = None, allow_fetch=False) -> dict:
    flog = flog or FLOG
    if not flog.exists():
        return {"error": "no forecast-log.jsonl"}
    rows = [json.loads(l) for l in open(flog) if l.strip()]
    today_iso = datetime.now(timezone.utc).date().isoformat()
    cache = load_cache()

    # Re-verify the adverse-markout magnitude from live data (don't hardcode) — falls back to 5.1
    mk = _rj(STATE / "live-premium" / "markout-log.jsonl")
    our = _our_posted_tickers(STATE / "live-premium")
    lm, lm_n = (_mean_markout(mk, our_tickers=our) if (mk and our) else (None, 0))
    adverse = round(abs(lm), 2) if (lm is not None and lm_n) else 5.1

    def af(c, m, d):
        return get_actual(c, m, d, cache, allow_fetch)

    cont_cells, cont_counts = replay(rows, af, today_iso, adverse, leak1_cutoff=None)
    lf_cells, lf_counts = replay(rows, af, today_iso, adverse, leak1_cutoff=LEAK1_CUTOFF)
    cont_grid = {k: _cell_stats(v) for k, v in cont_cells.items()}
    lf_grid = {k: _cell_stats(v) for k, v in lf_cells.items()}
    return {"contaminated": {"grid": cont_grid, "counts": cont_counts},
            "leakfree": {"grid": lf_grid, "counts": lf_counts},
            "adverse_markout": adverse,
            "verdict": _verdict(lf_grid, cont_grid, lf_counts.get("awaiting_actual", 0)),
            "today": today_iso}


def _clear_pos(ci):
    _, lo, hi = ci
    return lo is not None and lo != hi and lo > 0


def _clear_neg(ci):
    _, lo, hi = ci
    return hi is not None and lo != hi and hi < 0


_NA = {"n_events": 0, "n": 0, "ci": (None, None, None)}


def _verdict(lf_grid, cont_grid, lf_awaiting: int = 0) -> dict:
    # Rule on the REALISTIC cell (spread 4, taker, live lead-gate). NOT the wide-spread "conservative"
    # cell: a wider assumed spread lowers the edge, so the gate admits FEWER, higher-edge survivors —
    # a self-selected subpopulation, not a harder bar on the same trades (adversarial review #9).
    typ = lf_grid.get((4, "lead_keyed", "taker"), _NA)
    opt = lf_grid.get((2, "flat_10", "taker"), _NA)          # loosest gate + best fills = optimistic bound
    cont_typ = cont_grid.get((4, "lead_keyed", "taker"), _NA)
    # Cell-fan sign spread — with a MIN_EVENTS floor. NOTE: these are the SAME trades under different
    # assumptions, NOT independent tests, so this is a descriptive fan, not a multiplicity-safe count.
    n_lo_pos = sum(1 for c in lf_grid.values() if c["n_events"] >= MIN_EVENTS and _clear_pos(c["ci"]))
    n_hi_neg = sum(1 for c in lf_grid.values() if c["n_events"] >= MIN_EVENTS and _clear_neg(c["ci"]))
    lf_trades = sum(c["n"] for c in lf_grid.values())
    typ_enough = typ["n_events"] >= MIN_EVENTS
    if lf_trades == 0 or not typ_enough:
        detail = (f"{lf_awaiting} leak-free contracts awaiting settled archive actuals (~6-7d lag)"
                  if lf_awaiting else "no leak-free contracts pass the filters yet")
        head = (f"INCONCLUSIVE — the LEAK-FREE replay is underpowered (realistic cell "
                f"{typ['n_events']} events; {detail}). The LEAK-1-CONTAMINATED replay shows a spurious "
                f"{_fmt(cont_typ['ci'])}/ct (typical) — that magnitude is LEAKAGE, not edge.")
    elif _clear_pos(typ["ci"]):
        head = "PAYS — the realistic leak-free cell (spread 4, taker, live lead-gate) has CI > 0"
    elif opt["n_events"] >= MIN_EVENTS and _clear_neg(opt["ci"]):
        head = "DOES NOT PAY — even the optimistic leak-free cell (tight spread, taker, flat-10) has CI < 0"
    else:
        head = "INCONCLUSIVE — the realistic leak-free cell's event-clustered CI spans 0 (skill not shown to pay)"
    return {"headline": head, "cells_lo_pos": n_lo_pos, "cells_hi_neg": n_hi_neg,
            "cells_span0": len(lf_grid) - n_lo_pos - n_hi_neg, "n_cells": len(lf_grid),
            "typical_ci": typ["ci"], "optimistic_ci": opt["ci"], "contaminated_typical_ci": cont_typ["ci"]}


def _fmt(ci):
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.2f}¢ [{ci[1]:+.2f}, {ci[2]:+.2f}]"


def _print_grid(grid):
    print(f"  {'spread':>6} {'threshold':>10} {'fill':>13} {'events':>6} {'trades':>7}   $/ct edge (event-clustered 95%)")
    for spread in SPREADS:
        for thr in THRESHOLDS:
            for fill in FILLS:
                s = grid.get((spread, thr, fill), _NA)
                print(f"  {spread:>6} {thr:>10} {fill:>13} {s['n_events']:>6} {s['n']:>7}   {_fmt(s['ci'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-fetch", action="store_true", help="fetch missing actuals (default cache-only)")
    args = ap.parse_args()
    r = evaluate(allow_fetch=args.allow_fetch)
    if "error" in r:
        print(f"[pnl-replay] {r['error']}")
        return 1
    lf, cont, v = r["leakfree"], r["contaminated"], r["verdict"]
    print("[pnl-replay] price-aware model-edge P&L replay (COUNTERFACTUAL, per-contract, event-clustered)")
    print(f"  adverse_markout={r['adverse_markout']}¢ (live-measured)")
    print("  ⚠ spread is ASSUMED (only mid is logged) AND this is NOT the live book's realized P&L —")
    print("     the live arm prices off the book, not the model; this is 'if we'd taken every model-edge signal'.\n")

    lfc = lf["counts"]
    print(f"── LEAK-FREE (asof > {LEAK1_CUTOFF.date()} guard) — the VALID estimand — "
          f"{lfc['settleable']} contracts scored, {lfc.get('awaiting_actual', 0)} awaiting actuals ──")
    if lf["grid"]:
        _print_grid(lf["grid"])
        print(f"  assumption fan (NOT independent tests — same trades, different assumptions; wider-spread "
              f"cells trade a self-selected subset): {v['cells_lo_pos']} cells >0, {v['cells_hi_neg']} <0, "
              f"{v['cells_span0']} span 0 (≥{MIN_EVENTS} events, of {v['n_cells']})")
        print(f"  realistic (spread 4, taker, lead-gate): {_fmt(v['typical_ci'])}   "
              f"optimistic bound (spread 2, taker, flat-10): {_fmt(v['optimistic_ci'])}")
    else:
        print(f"  (0 settleable leak-free contracts yet; {lfc.get('awaiting_actual', 0)} awaiting the "
              "Open-Meteo archive, ~6-7d lag; accrues from here)")

    cc = cont["counts"]
    print(f"\n── LEAK-1-CONTAMINATED (pre-guard fair_prob) — LEAK-INFLATED, sizes the leak, NOT a valid edge — "
          f"{cc['settleable']} contracts (dropped same-day {cc['dropped_sameday']}, persist-leak {cc['dropped_oldleak']}) ──")
    _print_grid(cont["grid"])
    print("  (per-contract: one latest-asof trade per bin-ticker; 'trades' = contracts, not scan-rows)")

    print(f"\n  VERDICT: {v['headline']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

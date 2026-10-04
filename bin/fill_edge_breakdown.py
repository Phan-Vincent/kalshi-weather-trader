#!/usr/bin/env python3
"""bin/fill_edge_breakdown.py — CONDITIONAL realized-edge + live fill-rate, sliced.

The aggregate tools already exist: bin/live_fill_quality.py (OK/WATCH/ADVERSE on overall realized
EV + markout) and bin/reconcile_fills.py (overall live-vs-paper fill rate → de-bias params).
bin/premium_fill_report.py slices realized edge by PRICE BAND. What no standing tool does — and what
matters most as live fills accumulate — is slicing the realized edge by the dimensions that localize
*where* the edge or the adverse selection lives:

    • SIDE        — the 2026-06-25 dig found YES-bin −23.5¢/ct (lose) vs NO-bin +68¢/ct (win):
                    climatology can't pin a narrow high-temp bin, so YES entries scatter off and miss.
                    This turns that manual dig into a repeatable instrument (feeds the YES-bin haircut).
    • CITY        — HOU is the worst city; per-city edge decides down-weight/drop (feeds selection).
    • MARKET TYPE — HIGH vs LOW, bin (B) vs threshold (T).
    • ENTRY BAND  — same cut premium_fill_report does, kept here for one-stop comparison.
    • LEAD TIME   — open→settle hours: same-day intraday bins vs multi-day.

Plus the LIVE maker fill RATE sliced by side / entry band (its attribution mirrors
reconcile_fills.live_fill_stats — pinned equal by a test). All read-only; places no orders; reads the
already-synced state (settlement-log.jsonl + maker-lifecycle.jsonl + paper-book.json).

Each slice reports n, contracts, EV/ct, EV-after-maker-fee/ct, win-rate with a Wilson 95% CI, and a
"real?" flag (CI lower bound beats the breakeven implied by the avg entry price — same test as
premium_fill_report). Small n ⇒ wide CI ⇒ directional only; that is the point of watching it grow.

Usage:
  python3 bin/fill_edge_breakdown.py                          # state/live-premium, human report
  python3 bin/fill_edge_breakdown.py --live-dir state/paper   # any book's state dir
  python3 bin/fill_edge_breakdown.py --json                   # machine-readable to stdout
  python3 bin/fill_edge_breakdown.py --write                  # also persist a snapshot JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from tail_edge import wilson                               # noqa: E402  (same CI as the thesis script)
from reconcile_fills import _read_jsonl, _load_book        # noqa: E402  (one JSONL/book reader)
from compare_variants import _event                        # noqa: E402  (the mandated sample unit)

BANDS = [(1, 15), (16, 25), (26, 40), (41, 60), (61, 99)]
_CITY_RE = re.compile(r"KX(?:HIGH|LOW)T?([A-Z]+)")         # mirrors sync_live_positions._city


def _city(tk: str) -> str | None:
    m = _CITY_RE.match(tk or "")
    return m.group(1) if m else None


def _market_type(tk: str) -> str:
    kind = "HIGH" if "HIGH" in (tk or "") else ("LOW" if "LOW" in (tk or "") else "?")
    strike = (tk or "").rsplit("-", 1)[-1][:1]             # B (bin) | T (threshold)
    return f"{kind}/{strike}" if strike in ("B", "T") else kind


def _band_label(px) -> str | None:
    if px is None:
        return None
    for lo, hi in BANDS:
        if lo <= px <= hi:
            return f"{lo}-{hi}c"
    return None


def _lead_bucket(opened: str, settled: str) -> str | None:
    def _parse(s):
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            # python<3.11 rejects 4/5-digit fractional seconds (…23.8307+00:00) — ~22% of the live
            # log would silently fall out of every lead bucket, biasing the slice to short-fraction
            # rows. Strip the fraction and retry (mirrors yes_lever_decision.load_rows' guard).
            try:
                return datetime.fromisoformat(re.sub(r"\.\d+", "", s))
            except ValueError:
                return None
    o, s = _parse(opened), _parse(settled)
    if not o or not s:
        return None
    h = (s - o).total_seconds() / 3600.0
    if h < 0:
        return None
    if h < 12:
        return "<12h (same-day)"
    if h < 24:
        return "12-24h"
    if h < 48:
        return "24-48h"
    return ">=48h"


def _clean_settlements(settle: list[dict]) -> list[dict]:
    """Attributed settlements only — drop gap_settled / malformed rows (qty<=0, or entry None/<=0)
    that would crash fee_per_contract_cents or pollute the entry-priced stats (a disappeared-reconcile
    row can carry entry_cents=0 for unknown entry — authoritative pnl but no usable price to slice on,
    so it's excluded like gap_settled). (mirrors live_fill_quality; QA 2026-06-30)"""
    return [r for r in settle if (r.get("qty") or 0) > 0
            and r.get("entry_cents") is not None and r.get("entry_cents") > 0]


def _slice(rows: list[dict]) -> dict:
    """Realized-edge stats for one group of settled fills."""
    n = len(rows)
    q = sum(r["qty"] for r in rows) or 1
    pnl = sum(r.get("pnl_cents", 0) for r in rows)
    wins = sum(1 for r in rows if (r.get("pnl_cents") or 0) > 0)
    px = sum(r["entry_cents"] for r in rows) / n
    clo, chi = wilson(wins, n)
    # pnl_cents is ALREADY net of fees/rebate: live rows are Kalshi's net realized P&L
    # (sync_live_positions.py:469), paper rows credit the rebate into cost (paper_book).
    # The old code re-subtracted fee_per_contract_cents(maker=True) — a NEGATIVE rebate —
    # which ADDED the rebate a second time, inflating EV ~+0.47c/ct on the live-premium
    # ledger (audit 2026-07-06). ev_after_fee == net EV; no further adjustment.
    return {
        "n": n, "contracts": q, "pnl_cents": pnl,
        # events = distinct series+city+date clusters — the repo's mandated sample unit. Slices
        # with few events (HIGH/T sat at 13 for weeks while showing −18c/ct) are concentration
        # artifacts until bin/pocket_lever_decision.py adjudicates them.
        "events": len({_event(r.get("ticker", "")) for r in rows}),
        "ev_ct": pnl / q, "ev_after_fee_ct": pnl / q,
        "win_rate": wins / n, "wr_ci": [clo, chi], "avg_entry_c": px,
        # "real?": even the pessimistic CI floor wins more often than the entry price implies (>breakeven)
        "real": clo * 100 > px,
    }


def edge_by(settle: list[dict], keyfn) -> list[dict]:
    groups: dict = defaultdict(list)
    for r in settle:
        k = keyfn(r)
        if k is not None:
            groups[k].append(r)
    out = [dict(key=k, **_slice(rows)) for k, rows in groups.items()]
    return sorted(out, key=lambda d: d["ev_after_fee_ct"])   # worst first


# ── LIVE fill rate, sliced. Attribution MIRRORS reconcile_fills.live_fill_stats (pinned by test). ──
def _live_join(live_dir: Path):
    posted = [r for r in _read_jsonl(live_dir / "maker-lifecycle.jsonl")
              if r.get("event") == "posted_live"]
    quote: dict = {}
    for r in posted:
        quote.setdefault((r.get("ticker"), r.get("side")), r.get("limit_price_cents"))
    book = _load_book(live_dir)
    posted_keys = set(quote)
    filled_keys = {(f.get("ticker"), f.get("side"))
                   for f in (list(book.get("open", [])) + list(book.get("closed", [])))
                   if (f.get("ticker"), f.get("side")) in posted_keys}
    return quote, posted_keys, filled_keys


def _rate(num: set, den: set) -> dict:
    return {"posted": len(den), "filled": len(num & den),
            "fill_rate": (len(num & den) / len(den)) if den else None}


def live_fill_rate(live_dir: Path) -> dict:
    quote, posted_keys, filled_keys = _live_join(live_dir)
    by_side: dict = {}
    for s in ("yes", "no"):
        den = {k for k in posted_keys if k[1] == s}
        by_side[s] = _rate(filled_keys, den)
    by_band: dict = {}
    for lo, hi in BANDS:
        lab = f"{lo}-{hi}c"
        den = {k for k in posted_keys if (lambda p: p is not None and lo <= p <= hi)(quote.get(k))}
        if den:
            by_band[lab] = _rate(filled_keys, den)
    return {"overall": _rate(filled_keys, posted_keys), "by_side": by_side, "by_band": by_band}


DIMENSIONS = [
    ("SIDE", lambda r: r.get("side")),
    ("CITY", lambda r: _city(r.get("ticker", ""))),
    ("MARKET TYPE", lambda r: _market_type(r.get("ticker", ""))),
    ("ENTRY BAND", lambda r: _band_label(r.get("entry_cents"))),
    ("LEAD TIME", lambda r: _lead_bucket(r.get("opened_utc", ""), r.get("settled_at_utc", ""))),
]


def analyze(live_dir: Path) -> dict:
    settle = _clean_settlements(_read_jsonl(live_dir / "settlement-log.jsonl"))
    overall = _slice(settle) if settle else None
    by = {name: edge_by(settle, keyfn) for name, keyfn in DIMENSIONS}
    total_pnl = sum(r.get("pnl_cents", 0) for r in settle)
    # Invariant: SIDE is present on every attributed fill, so its slices must repartition the full
    # realized P&L exactly. A mismatch means rows were dropped or miskeyed — a real bug signal.
    reconcile_ok = sum(s["pnl_cents"] for s in by["SIDE"]) == total_pnl
    return {
        "live_dir": str(live_dir),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        # These slices are OBSERVABILITY, not evidence: per-contract, Wilson WR CI only, no EV
        # bootstrap, no event clustering, multiplicity-uncorrected (QA-quant-review-2026-07-01).
        # The 2026-07-09 session was briefed "same-day −4c" off a stale read of this file while
        # the artifact said +0.9c — hence the explicit labels.
        "decision_grade": False,
        "weighting": "ev_ct is contract-weighted pooled (sum pnl / sum qty); "
                     "wr_ci is Wilson per-POSITION (not event-clustered)",
        "note": "post-hoc slices, multiplicity-uncorrected; "
                "the pre-registered gate is bin/pocket_lever_decision.py",
        "n_settled": len(settle),
        "total_pnl_cents": total_pnl,
        "overall": overall,
        "by": by,
        "live_fill_rate": live_fill_rate(live_dir),
        "reconcile_ok": reconcile_ok,
    }


def _fmt_slice(label: str, s: dict) -> str:
    clo, chi = s["wr_ci"]
    flag = "real" if s["real"] else ""
    return (f"  {label:>18}  n={s['n']:>3} ev={s['events']:>3} ct={s['contracts']:>4}  "
            f"EV={s['ev_ct']:>+6.1f}c  EV-fee={s['ev_after_fee_ct']:>+6.1f}c  "
            f"WR={s['win_rate']*100:>3.0f}% [{clo*100:>3.0f}-{chi*100:<3.0f}]  "
            f"@{s['avg_entry_c']:>4.0f}c  {flag}")


def report(a: dict) -> str:
    L = [f"\nLIVE FILL-EDGE BREAKDOWN — {a['live_dir']}", "=" * 78]
    if not a["overall"]:
        L.append("  (no attributed settlements yet)")
        return "\n".join(L)
    o = a["overall"]
    L.append(f"OVERALL: n={o['n']} events={o['events']} contracts={o['contracts']}  "
             f"EV-after-fee={o['ev_after_fee_ct']:+.1f}c/ct  "
             f"realized=${a['total_pnl_cents']/100:+.2f}  WR={o['win_rate']*100:.0f}%")
    L.append(f"  reconcile_ok={a['reconcile_ok']}  (slice pnl sums to total)")
    L.append("  NOT DECISION-GRADE: per-contract slices, Wilson WR CI only (not event-clustered,"
             " multiplicity-uncorrected); ev = event count; gate = bin/pocket_lever_decision.py")
    for name, _ in DIMENSIONS:
        L.append(f"\n{name}  (worst EV-after-fee first):")
        for s in a["by"][name]:
            L.append(_fmt_slice(str(s["key"]), s))
    fr = a["live_fill_rate"]
    ov = fr["overall"]
    L.append("\nLIVE MAKER FILL RATE  (attribution = reconcile_fills):")
    if ov["fill_rate"] is not None:
        L.append(f"  overall: {ov['filled']}/{ov['posted']} = {ov['fill_rate']*100:.0f}%")
        for s, r in fr["by_side"].items():
            if r["fill_rate"] is not None:
                L.append(f"  {s:>3}-side: {r['filled']}/{r['posted']} = {r['fill_rate']*100:.0f}%")
        for b, r in fr["by_band"].items():
            L.append(f"  band {b:>7}: {r['filled']}/{r['posted']} = {r['fill_rate']*100:.0f}%")
    else:
        L.append("  (no posted_live quotes yet)")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sliced realized-edge + live fill-rate breakdown.")
    ap.add_argument("--live-dir", default="state/live-premium")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON to stdout")
    ap.add_argument("--write", action="store_true",
                    help="also persist a snapshot to <live-dir>/fill-edge-breakdown.json")
    args = ap.parse_args()
    live_dir = ROOT / args.live_dir if not Path(args.live_dir).is_absolute() else Path(args.live_dir)

    a = analyze(live_dir)
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        print(report(a))
    if args.write:
        out = live_dir / "fill-edge-breakdown.json"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(a, indent=2))
            print(f"\n[written] {out}", file=sys.stderr)
        except OSError as e:                     # fail-soft: a read-only/missing dir must not exit 1
            print(f"\n[write skipped] {out}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

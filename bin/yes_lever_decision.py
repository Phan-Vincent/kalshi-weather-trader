#!/usr/bin/env python3
"""bin/yes_lever_decision.py — standing, re-runnable YES live-skip lever decision.

Background. On 2026-07-05 a pre-registered decision packet asked "is the premium-live YES side a real
losing edge, or a size/concentration artifact?" and ruled **HOLD** (reviews/yes-lever-decision-2026-
07-05.md, 1 of 3 gate conditions met). It pre-committed the un-block criteria — ≥40 *post-fix* YES
settlement events + spread-tagged fills, then "re-run this exact packet" — but its reproduction script
(scratchpad/yes_ci.py) was a throwaway and has since been DELETED. So the deferred decision is no
longer re-runnable: whoever hits the threshold in a few weeks would rebuild it from scratch and risk
silently changing the frozen estimand.

This turns that one-off into a checked-in, auto-tracked standing tool — a sibling of
check_futility_checkpoint.py / markout_kill_test.py. It reuses the validated machinery rather than
re-deriving stats or P&L:
  • per-side realized edge / rows            → the SAME filter as fill_edge_breakdown (_clean_settlements)
  • event clustering + bootstrap CI          → compare_variants._event / cluster_bootstrap_ci
  • weather-only guard                       → data.weather_data.is_weather_ticker

The pre-registered gate (all 3 must FIRE to flip the lever), the two prerequisites, and the drop-top-3
/ qty-split robustness checks are reproduced verbatim from the packet. The *primary* gate CIs use the
percentile bootstrap (what the 7/5 packet froze); the house-standard BCa CI is reported alongside as a
cross-check. Read-only: this tool NEVER trades, halts, or resizes — a human makes the live-money call.
The lever itself is KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT (trader/scanner.py); this tool does not set it.

Usage:
  python3 bin/yes_lever_decision.py                          # current live-premium book, human report
  python3 bin/yes_lever_decision.py --settled-through 2026-07-05   # reproduce the frozen 7/5 packet
  python3 bin/yes_lever_decision.py --fix-cutoff 26JUL06     # move the post-fix clock (see note below)
  python3 bin/yes_lever_decision.py --json                   # machine-readable to stdout
  python3 bin/yes_lever_decision.py --write                  # also persist state/live-premium/yes-lever-state.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import _read_jsonl, _event, cluster_bootstrap_ci, _mean   # noqa: E402
from fill_edge_breakdown import _clean_settlements                              # noqa: E402  (identical filter)
from data.weather_data import is_weather_ticker                                # noqa: E402

STATE_DIR = ROOT / "state" / "live-premium"
STATE_PATH = STATE_DIR / "yes-lever-state.json"
_NOW = lambda: datetime.now(timezone.utc).isoformat()   # noqa: E731

# Pre-registered thresholds, frozen from the 7/5 packet.
MIN_YES_EVENTS = 40          # gate (i) and prereq: ≥40 post-fix YES events
DEFAULT_FIX_CUTOFF = "26JUL04"   # the 7/3–04 model fixes the packet defined "post-fix" against
BOOTSTRAP_B = 8000           # matches the packet's resolution (RNG differs → CIs match within noise)
DROP_TOP_N = 3               # concentration robustness: drop the 3 worst YES events


def _ticker_date(ticker: str) -> date | None:
    """The event date embedded in the ticker, e.g. 'KXHIGHTNOLA-26JUL01-B588' → 2026-07-01.
    _event() drops the strike; the date is the second '-' segment (YYMMMDD)."""
    parts = (ticker or "").split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1], "%y%b%d").date()
    except ValueError:
        return None


def load_rows(state_dir: Path, settled_through: date | None = None, weather_only: bool = True) -> list[dict]:
    """Attributed settlements for the book — SAME clean filter as fill_edge_breakdown, plus the
    weather guard (the live-premium book is weather-only) and an optional as-of settlement cutoff so
    the frozen 7/5 packet can be reproduced from the grown append-only log."""
    rows = _clean_settlements(_read_jsonl(state_dir / "settlement-log.jsonl"))
    if weather_only:
        rows = [r for r in rows if is_weather_ticker(r.get("ticker", ""))]
    if settled_through is not None:
        def _settled_date(r):
            # date portion only: robust to any fractional-second precision. datetime.fromisoformat on
            # Python <3.11 rejects 4/5-digit fractions (e.g. '…23.8307Z'), silently dropping ~14% of
            # the log — the exact rows that decide event count (this repo runs on python3.9).
            s = r.get("settled_at_utc") or ""
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                return None
        rows = [r for r in rows if (_settled_date(r) is not None and _settled_date(r) <= settled_through)]
    # Join spread-at-post telemetry (2026-07-13 fix). spread_cents_at_post is logged ONLY in
    # maker-lifecycle.jsonl (posted_live rows), never in settlement-log.jsonl — so the lever's
    # spread_telemetry_present prerequisite counted 0/N and the kill-switch could NEVER fire even
    # after telemetry began accruing (it read the wrong log). Attach it to each settled row by
    # (ticker, side) — a settled position maps to the maker order that was posted for it — so the
    # coverage the lever gates on reflects reality. Additive; other consumers ignore the field.
    spread_by_key: dict = {}
    for m in _read_jsonl(state_dir / "maker-lifecycle.jsonl"):
        if m.get("event") == "posted_live" and m.get("spread_cents_at_post") is not None:
            spread_by_key.setdefault((m.get("ticker", ""), str(m.get("side") or "").lower()),
                                     m.get("spread_cents_at_post"))
    for r in rows:
        if r.get("spread_cents_at_post") is None:
            sp = spread_by_key.get((r.get("ticker", ""), str(r.get("side") or "").lower()))
            if sp is not None:
                r["spread_cents_at_post"] = sp
    return rows


def _side_items(rows: list[dict]) -> list[tuple]:
    """Event-clustered per-contract items [(event, pnl_cents/qty)] — the SAME estimand as
    arm_metrics.pnl_per_contract_ci and the futility checkpoint (pnl_cents is already net of fees)."""
    return [(_event(r.get("ticker", "")), float(r.get("pnl_cents", 0)) / r["qty"]) for r in rows]


def _n_events(rows: list[dict]) -> int:
    return len({_event(r.get("ticker", "")) for r in rows})


def _contract_weighted_ev(rows: list[dict]) -> float | None:
    """Contract-weighted EV/ct = total pnl / total contracts — reproduces fill_edge_breakdown's SIDE
    slice (different weighting from the clustered per-trade mean; both are reported)."""
    q = sum(r["qty"] for r in rows)
    return (sum(r.get("pnl_cents", 0) for r in rows) / q) if q else None


def _cw_groups(rows: list[dict]) -> tuple[list, dict]:
    """Group rows by event → (event_keys, {event: [(pnl, qty), …]}) for contract-weighted resampling."""
    g: dict = defaultdict(list)
    for r in rows:
        g[_event(r.get("ticker", ""))].append((float(r.get("pnl_cents", 0)), float(r["qty"])))
    return list(g.keys()), g


def _ratio(pairs) -> float:
    sp = sum(p for p, _ in pairs)
    sq = sum(q for _, q in pairs)
    return (sp / sq) if sq else 0.0


def _cw_cluster_ci(rows: list[dict], B: int = BOOTSTRAP_B, alpha: float = 0.05,
                   seed: int = 7, method: str = "percentile"):
    """Event-clustered bootstrap CI of the CONTRACT-WEIGHTED edge sum(pnl)/sum(qty). This is the 7/5
    packet's gate estimand: contract weighting is deliberate — the equal-per-trade mean mathematically
    dampens the big-lot bleed that IS the question (qty≥6 = −17.7¢/ct). Resamples EVENTS with
    replacement so within-event correlation doesn't understate variance. method='percentile' is the
    frozen-packet primary; method='bca' is the house-standard cross-check (bias-corrected+accelerated,
    event-level jackknife) — mirrors compare_variants.cluster_bootstrap_ci."""
    import random
    if not rows:
        return (None, None, None)
    events, groups = _cw_groups(rows)
    all_pairs = [pr for ev in events for pr in groups[ev]]
    point = _ratio(all_pairs)
    rng = random.Random(seed)
    k = len(events)
    samples = []
    for _ in range(B):
        pairs: list = []
        for _ in range(k):
            pairs.extend(groups[events[rng.randrange(k)]])
        samples.append(_ratio(pairs))
    samples.sort()

    def _pct(qq: float) -> float:
        return samples[min(B - 1, max(0, int(qq * B)))]

    if method == "percentile" or k < 2 or samples[0] == samples[-1]:
        return (point, _pct(alpha / 2), _pct(1 - alpha / 2))

    # ── BCa (same construction as compare_variants.cluster_bootstrap_ci, ratio statistic) ──
    from statistics import NormalDist
    nd = NormalDist()
    n_lt = sum(1 for s in samples if s < point)
    n_eq = sum(1 for s in samples if s == point)
    p0 = min(max((n_lt + 0.5 * n_eq) / B, 1.0 / (B + 1)), B / (B + 1.0))
    z0 = nd.inv_cdf(p0)
    tot_p = sum(p for p, _ in all_pairs)
    tot_q = sum(q for _, q in all_pairs)
    jk = []
    for ev in events:                                   # leave-one-EVENT-out ratios
        sp = sum(p for p, _ in groups[ev]); sq = sum(q for _, q in groups[ev])
        rem_q = tot_q - sq
        jk.append(((tot_p - sp) / rem_q) if rem_q > 0 else point)
    jm = _mean(jk)
    num = sum((jm - t) ** 3 for t in jk)
    den = 6.0 * (sum((jm - t) ** 2 for t in jk) ** 1.5)
    a = (num / den) if den > 0 else 0.0

    def _bca_q(qq: float) -> float:
        z = nd.inv_cdf(qq)
        denom = 1.0 - a * (z0 + z)
        if denom <= 0:
            return qq
        return min(max(nd.cdf(z0 + (z0 + z) / denom), 0.5 / B), 1.0 - 0.5 / B)

    return (point, _pct(_bca_q(alpha / 2)), _pct(_bca_q(1 - alpha / 2)))


def _cw_diff_ci(rows_a: list[dict], rows_b: list[dict],
                B: int = BOOTSTRAP_B, alpha: float = 0.05, seed: int = 5):
    """Event-clustered percentile CI for contract-weighted mean(A) − mean(B), A and B INDEPENDENT
    (YES vs NO are different markets — a two-sample diff, not compare_variants.pairwise's paired
    ticker-match). Resamples each side's events independently; matches the packet's YES−NO CI."""
    import random
    if not rows_a or not rows_b:
        return (None, None, None)
    ea, ga = _cw_groups(rows_a)
    eb, gb = _cw_groups(rows_b)
    point = _ratio([pr for ev in ea for pr in ga[ev]]) - _ratio([pr for ev in eb for pr in gb[ev]])
    rng = random.Random(seed)

    def _resample_ratio(events, groups):
        pairs: list = []
        k = len(events)
        for _ in range(k):
            pairs.extend(groups[events[rng.randrange(k)]])
        return _ratio(pairs)

    samples = sorted(_resample_ratio(ea, ga) - _resample_ratio(eb, gb) for _ in range(B))
    lo = samples[min(B - 1, max(0, int((alpha / 2) * B)))]
    hi = samples[min(B - 1, max(0, int((1 - alpha / 2) * B)))]
    return (point, lo, hi)


def _drop_top_n_events(rows: list[dict], n: int = DROP_TOP_N) -> tuple[list[dict], list[tuple]]:
    """Drop the n events with the most-negative NET pnl → (surviving rows, [(event, net_pnl)] dropped).
    Tests whether the YES deficit survives removing its worst-concentration events."""
    by_ev: dict = defaultdict(float)
    for r in rows:
        by_ev[_event(r.get("ticker", ""))] += float(r.get("pnl_cents", 0))
    worst = sorted(by_ev.items(), key=lambda kv: kv[1])[:n]
    dropped = {ev for ev, _ in worst}
    survivors = [r for r in rows if _event(r.get("ticker", "")) not in dropped]
    return survivors, worst


def _qty_split(rows: list[dict]) -> dict:
    """The 7/5 finding: the YES bleed lives entirely in oversized bets (qty≥6). Report both weightings
    for each bucket + contract share, so 'size effect' vs 'concentration overlap' is visible."""
    out: dict = {}
    tot_q = sum(r["qty"] for r in rows) or 1
    for label, keep in (("qty<=5", lambda q: q <= 5), ("qty>=6", lambda q: q >= 6)):
        sub = [r for r in rows if keep(r["qty"])]
        q = sum(r["qty"] for r in sub)
        items = _side_items(sub)
        out[label] = {
            "n": len(sub), "contracts": q, "contract_share": round(q / tot_q, 3),
            "ev_ct_contract_wt": (round(_contract_weighted_ev(sub), 2) if sub else None),
            "ev_ct_pertrade_mean": (round(_mean([v for _, v in items]), 2) if items else None),
        }
    return out


def evaluate(state_dir: Path, settled_through: date | None = None,
             fix_cutoff: str = DEFAULT_FIX_CUTOFF, B: int = BOOTSTRAP_B) -> dict:
    rows = load_rows(state_dir, settled_through=settled_through)
    yes = [r for r in rows if r.get("side") == "yes"]
    no = [r for r in rows if r.get("side") == "no"]

    yes_n_events, no_n_events = _n_events(yes), _n_events(no)
    # PRIMARY gate estimand: contract-weighted, event-clustered, percentile (reproduces the 7/5 packet).
    yes_ci_pct = _cw_cluster_ci(yes, B=B, method="percentile", seed=7)
    yes_ci_bca = _cw_cluster_ci(yes, B=B, method="bca", seed=7)              # house-standard cross-check
    no_ci_pct = _cw_cluster_ci(no, B=B, method="percentile", seed=8)
    diff_ci = _cw_diff_ci(yes, no, B=B)
    # CROSS-CHECK estimand: equal-per-trade clustered mean. It downweights big lots, so the gap between
    # this and the contract-weighted point (≈ +5¢ less negative) IS the size/concentration signal.
    yes_eqw_bca = cluster_bootstrap_ci(_side_items(yes), B=B, method="bca", seed=7) if yes else (None, None, None)

    # post-fix regime (the un-block clock). fix_cutoff is a PARAMETER, not baked: the packet defined
    # "post-fix" vs the 7/3–04 fixes, but material fixes shipped since (Dallas airport + grid→station
    # correction, 7/6) — if those matter to YES calibration the operator moves --fix-cutoff and the
    # 40-event clock resets. Surfaced explicitly below rather than silently chosen.
    try:
        cutoff_date = datetime.strptime(fix_cutoff, "%y%b%d").date()
    except ValueError:
        cutoff_date = datetime.strptime(DEFAULT_FIX_CUTOFF, "%y%b%d").date()
        fix_cutoff = DEFAULT_FIX_CUTOFF
    yes_postfix = [r for r in yes if (_ticker_date(r.get("ticker", "")) is not None
                                      and _ticker_date(r.get("ticker", "")) >= cutoff_date)]
    postfix_events = _n_events(yes_postfix)
    postfix_ci = _cw_cluster_ci(yes_postfix, B=B, method="percentile", seed=9)

    spread_tagged = sum(1 for r in rows if r.get("spread_cents_at_post") is not None)

    drop_rows, dropped = _drop_top_n_events(yes, DROP_TOP_N)
    drop_ci = _cw_cluster_ci(drop_rows, B=B, method="percentile", seed=11)
    top_n_deficit_share = None
    yes_total = sum(r.get("pnl_cents", 0) for r in yes)
    if yes_total < 0 and dropped:
        top_n_deficit_share = round(sum(min(0.0, p) for _, p in dropped) / yes_total, 3)

    # ── pre-registered gate: ALL 3 must FIRE to flip the lever ──
    gate = {
        "i_ge40_yes_events": yes_n_events >= MIN_YES_EVENTS,
        "ii_yes_ev_confidently_neg": (yes_ci_pct[2] is not None and yes_ci_pct[2] < 0),   # CI upper < 0
        "iii_yes_confidently_lt_no": (diff_ci[2] is not None and diff_ci[2] < 0),          # CI upper < 0
    }
    # ── prerequisites (the other two of the packet's "five FIRE conditions") ──
    prereq = {
        "regime_usable_ge40_postfix": postfix_events >= MIN_YES_EVENTS,
        "spread_telemetry_present": spread_tagged > 0,
    }
    # ── robustness: the deficit must survive removing the top-3 concentration events ──
    robust = {"survives_drop_top3": (drop_ci[2] is not None and drop_ci[2] < 0)}

    # BCa cross-check must AGREE with the percentile gate on condition (ii); a disagreement means the
    # gate sits on the estimator boundary and should not be trusted to fire (flagged, not silent).
    ii_bca = (yes_ci_bca[2] is not None and yes_ci_bca[2] < 0)
    method_agreement = (gate["ii_yes_ev_confidently_neg"] == ii_bca)

    fire = all(gate.values()) and all(prereq.values()) and all(robust.values()) and method_agreement
    verdict = "FIRE-YES-LEVER" if fire else "HOLD"

    def _ci(t):
        return None if t[0] is None else {"point": round(t[0], 2), "lo": round(t[1], 2), "hi": round(t[2], 2)}

    return {
        "generated_utc": _NOW(),
        "state_dir": str(state_dir),
        "settled_through": settled_through.isoformat() if settled_through else None,
        "fix_cutoff": fix_cutoff,
        "counts": {
            "n_settled": len(rows), "yes_positions": len(yes), "no_positions": len(no),
            "yes_events": yes_n_events, "no_events": no_n_events,
            "yes_postfix_events": postfix_events, "spread_tagged_fills": spread_tagged,
        },
        "yes_ev_ct": {
            "contract_weighted_percentile": _ci(yes_ci_pct),           # PRIMARY gate estimand
            "contract_weighted_bca": _ci(yes_ci_bca),                  # house-standard cross-check
            "pertrade_mean_bca": _ci(yes_eqw_bca),                     # size-dampened view (gap = size signal)
            "contract_weighted_point": (round(_contract_weighted_ev(yes), 2) if yes else None),
        },
        "no_ev_ct": {
            "contract_weighted_percentile": _ci(no_ci_pct),
            "contract_weighted_point": (round(_contract_weighted_ev(no), 2) if no else None),
        },
        "yes_minus_no_ev_ct": {"contract_weighted_percentile": _ci(diff_ci)},
        "postfix_yes_ev_ct": {"contract_weighted_percentile": _ci(postfix_ci)},
        "method_agreement_pct_vs_bca": method_agreement,
        "robustness": {
            "drop_top3": {
                "dropped_events": [{"event": ev, "net_pnl_cents": round(p, 1)} for ev, p in dropped],
                "top3_share_of_deficit": top_n_deficit_share,
                "surviving_yes_ev_ct": _ci(drop_ci),
            },
            "qty_split": _qty_split(yes),
        },
        "gate": gate, "prerequisites": prereq, "robustness_flags": robust,
        "gate_conditions_met": sum(gate.values()),
        "verdict": verdict,
    }


# ── frozen-estimand state (mirrors futility-checkpoint-state.json) + transition alerts ──

_ESTIMAND = {
    "metric": "premium-live YES-side per-contract realized edge (pnl_cents/qty), after fees",
    "unit": "event-clustered (series+city+date)",
    "primary_ci": "percentile bootstrap, 95% (frozen from the 2026-07-05 packet)",
    "crosscheck_ci": "BCa bootstrap, 95% (house standard)",
    "source": "state/live-premium/settlement-log.jsonl",
    "filter": "qty>0 AND entry_cents present AND is_weather_ticker",
    "gate": "flip KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT=0 ONLY if ALL of: (i) ≥40 YES events; "
            "(ii) YES EV CI upper<0; (iii) YES−NO CI upper<0; AND both prerequisites "
            "(≥40 POST-FIX YES events; spread telemetry present); AND the deficit survives drop-top-3.",
    "reference": "reviews/yes-lever-decision-2026-07-05.md",
}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"tool_version": "1.0", "created_utc": _NOW(), "estimand": _ESTIMAND,
            "regime_unblocked_alerted": False, "fire_verdict": None}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def _alert(msg: str, key: str) -> None:
    try:
        from trader.notify import alert
        alert(msg, key=key)
    except Exception as e:                       # notify is best-effort; never break the cycle
        print(f"  [yes-lever] alert dispatch failed: {e}", file=sys.stderr)


def _fmt_ci(c) -> str:
    return "n/a" if not c else f"{c['point']:+.2f}¢ [{c['lo']:+.2f}, {c['hi']:+.2f}]"


def report(d: dict) -> str:
    c = d["counts"]
    L = ["\nYES LIVE-SKIP LEVER — pre-registered decision (report+alert only)", "=" * 74]
    L.append(f"book={d['state_dir']}"
             + (f"  as-of≤{d['settled_through']}" if d["settled_through"] else "")
             + f"  post-fix cutoff={d['fix_cutoff']}")
    L.append(f"settled={c['n_settled']}  YES={c['yes_positions']}pos/{c['yes_events']}ev  "
             f"NO={c['no_positions']}pos/{c['no_events']}ev")
    L.append("")
    L.append(f"  YES EV/ct  contract-wt (percentile, GATE) : {_fmt_ci(d['yes_ev_ct']['contract_weighted_percentile'])}")
    L.append(f"  YES EV/ct  contract-wt (BCa cross-check)  : {_fmt_ci(d['yes_ev_ct']['contract_weighted_bca'])}")
    L.append(f"  YES EV/ct  per-trade   (BCa, size-damped) : {_fmt_ci(d['yes_ev_ct']['pertrade_mean_bca'])}"
             f"   ← gap vs contract-wt = the big-lot bleed")
    L.append(f"  NO  EV/ct  contract-wt (percentile)       : {_fmt_ci(d['no_ev_ct']['contract_weighted_percentile'])}")
    L.append(f"  YES−NO EV/ct contract-wt (percentile)     : {_fmt_ci(d['yes_minus_no_ev_ct']['contract_weighted_percentile'])}")
    L.append("")
    g, p, r = d["gate"], d["prerequisites"], d["robustness_flags"]
    tick = lambda b: "✅" if b else "❌"   # noqa: E731
    L.append("  PRE-REGISTERED GATE (all 3 must FIRE):")
    L.append(f"    (i)   ≥40 YES events                 {tick(g['i_ge40_yes_events'])}  ({c['yes_events']})")
    L.append(f"    (ii)  YES EV CI upper < 0            {tick(g['ii_yes_ev_confidently_neg'])}")
    L.append(f"    (iii) YES−NO CI upper < 0            {tick(g['iii_yes_confidently_lt_no'])}")
    L.append("  PREREQUISITES:")
    L.append(f"    post-fix YES events ≥40             {tick(p['regime_usable_ge40_postfix'])}  "
             f"({c['yes_postfix_events']}/{MIN_YES_EVENTS})   post-fix EV "
             f"{_fmt_ci(d['postfix_yes_ev_ct']['contract_weighted_percentile'])}")
    L.append(f"    spread telemetry present           {tick(p['spread_telemetry_present'])}  "
             f"({c['spread_tagged_fills']} tagged fills)")
    L.append("  ROBUSTNESS:")
    dt = d["robustness"]["drop_top3"]
    share = dt["top3_share_of_deficit"]
    L.append(f"    deficit survives drop-top-3        {tick(r['survives_drop_top3'])}  "
             f"(top-3 = {int(share*100) if share is not None else '?'}% of deficit; "
             f"surviving YES {_fmt_ci(dt['surviving_yes_ev_ct'])})")
    qs = d["robustness"]["qty_split"]
    L.append(f"    qty split: qty≤5 {qs['qty<=5']['ev_ct_contract_wt']}¢/ct "
             f"({qs['qty<=5']['contracts']}ct) vs qty≥6 {qs['qty>=6']['ev_ct_contract_wt']}¢/ct "
             f"({qs['qty>=6']['contracts']}ct)")
    if not d.get("method_agreement_pct_vs_bca", True):
        L.append("    ⚠ percentile vs BCa DISAGREE on condition (ii) — gate on estimator boundary, do not fire")
    L.append("")
    L.append(f"  VERDICT: {d['verdict']}  ({d['gate_conditions_met']}/3 gate conditions met)")
    if d["verdict"] == "HOLD":
        L.append("  → keep trading YES live under existing rails; lever stays at default (no size change).")
    else:
        L.append("  → all conditions FIRE: flip KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT=0 on premium-live "
                 "(operator action; verify out-of-sample first).")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-registered YES live-skip lever decision (report+alert only).")
    ap.add_argument("--state-dir", default="state/live-premium")
    ap.add_argument("--settled-through", default=None,
                    help="only settlements on/before this date (YYYY-MM-DD) — reproduces the frozen packet")
    ap.add_argument("--fix-cutoff", default=DEFAULT_FIX_CUTOFF,
                    help=f"post-fix regime start, ticker date format YYMMMDD (default {DEFAULT_FIX_CUTOFF})")
    ap.add_argument("--bootstrap", type=int, default=BOOTSTRAP_B)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="persist state/live-premium/yes-lever-state.json + alert on transitions")
    args = ap.parse_args()

    state_dir = ROOT / args.state_dir if not Path(args.state_dir).is_absolute() else Path(args.state_dir)
    through = None
    if args.settled_through:
        through = datetime.strptime(args.settled_through, "%Y-%m-%d").date()

    d = evaluate(state_dir, settled_through=through, fix_cutoff=args.fix_cutoff, B=args.bootstrap)
    print(json.dumps(d, indent=2) if args.json else report(d))

    if args.write:
        st = _load_state()
        st["last_run_utc"] = _NOW()
        st["last_reading"] = {"counts": d["counts"], "verdict": d["verdict"],
                              "gate_conditions_met": d["gate_conditions_met"]}
        # Transition-gated alerts: fire ONLY when the decision becomes actionable, not every cycle.
        if not st.get("regime_unblocked_alerted") and d["prerequisites"]["regime_usable_ge40_postfix"]:
            st["regime_unblocked_alerted"] = True
            _alert(f"YES-lever now DECIDABLE: post-fix YES events crossed {MIN_YES_EVENTS} "
                   f"({d['counts']['yes_postfix_events']}). Re-run/verify the pre-registered packet. "
                   f"Report only; the lever flip is your call.", key="yes_lever_regime")
        if st.get("fire_verdict") is None and d["verdict"] == "FIRE-YES-LEVER":
            st["fire_verdict"] = {"verdict_utc": _NOW(), "counts": d["counts"]}
            _alert("YES-lever GATE FIRED: all pre-registered conditions met — YES side confidently "
                   "negative and < NO on the post-fix sample, survives drop-top-3. Consider flipping "
                   "KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT=0. Report only; your call.", key="yes_lever_fire")
        _save_state(st)
        print(f"\n[written] {STATE_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

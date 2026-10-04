#!/usr/bin/env python3
"""bin/pocket_lever_decision.py — standing, pre-registered POCKET-lever decision (report+alert only).

Background. After the live arm crossed 182 fills (2026-07-09) the tempting next move was a
"selection tightening pass": skip the pockets fill_edge_breakdown shows bleeding (HIGH/T −18¢/ct,
41-60¢ band, …). But those are per-contract, multiplicity-uncorrected slices — the exact trap the
repo has been burned by twice (the 2026-06-28 band A/B overturn; the 7/5 YES-lever HOLD, where the
"YES deficit" dissolved into 13 concentrated events). HIGH/T is 13 events with WY-adjusted p≈0.97
(reviews/harvest-edge-crosscheck-2026-07-06.md); the briefed "same-day −4¢" was stale (artifact:
+0.9¢). So instead of flipping filters, this tool freezes the decision the same way
yes_lever_decision.py froze the YES question: a pre-registered pocket family, event-clustered
contract-weighted CIs, Westfall–Young FWER across the family, drop-top-3 concentration robustness,
and a hard prerequisite that the futility n=200 verdict exists first (a live selection change
before that verdict would splice regimes into check_futility_checkpoint's frozen anytime-valid
stream — its docstring's "no continue" rule).

The FAMILY (13 one-dimensional pockets, immutable once frozen in state):
  • MARKET TYPE ×4  (HIGH/B, HIGH/T, LOW/B, LOW/T — strike-suffix B=bin / T=threshold)
  • ENTRY BAND  ×5  (1-15c, 16-25c, 26-40c, 41-60c, 61-99c)
  • LEAD TIME   ×4  (<12h same-day, 12-24h, 24-48h, >=48h; opened→settled hours)
SIDE is excluded — the YES/NO hypothesis already has its own frozen gate (yes_lever_decision.py);
testing it in two families would corrupt both FWER accountings. CITY is excluded — 9+ cities would
double the family and dilute Westfall–Young power, and city skips already have their own decision
path (KALSHI_WEATHER_PREMIUM_SKIP_CITIES). No interaction cells — per-cell event counts collapse
below any decidable floor. Classifier definitions are FROZEN COPIES here (pinned by test to
fill_edge_breakdown's, so drift is caught in CI, not silently forked estimands).

Known caveat (recorded, accepted): Westfall–Young tests the equal-per-trade clustered mean while
gate conditions (ii)/(iii) are contract-weighted — the three-weightings finding
(reviews/unverified-assumptions-audit-2026-07-06.md). Requiring BOTH to reject is strictly
conservative; the qty-split report catches contract-weighting-only (big-lot) deficits.

Read-only: NEVER trades, halts, or resizes. If a pocket ever fires, the flip is an OPERATOR
decision, and it must ship with a --fix-cutoff regime reset here (and a fresh futility discussion).

Usage:
  python3 bin/pocket_lever_decision.py                    # human report on the live book
  python3 bin/pocket_lever_decision.py --json             # machine-readable
  python3 bin/pocket_lever_decision.py --write            # persist state + transition alerts
  python3 bin/pocket_lever_decision.py --fix-cutoff 26AUG01   # regime reset after any selection change
  python3 bin/pocket_lever_decision.py --settled-through 2026-07-09   # as-of reproduction (no state write)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import _event, westfall_young                     # noqa: E402
from yes_lever_decision import (_cw_cluster_ci, _drop_top_n_events,     # noqa: E402
                                _qty_split, _ratio, _ticker_date, load_rows)

_NOW = lambda: datetime.now(timezone.utc).isoformat()   # noqa: E731

# Pre-registered thresholds — mirrored from the YES-lever packet (same bar, same B).
MIN_POCKET_EVENTS = 40
BOOTSTRAP_B = 8000
DROP_TOP_N = 3
ALPHA = 0.05

# ── FROZEN classifier copies (do NOT import from fill_edge_breakdown — an edit there must not
#    silently move this tool's estimand; tests pin the two implementations equal instead). ──
_BANDS = [(1, 15), (16, 25), (26, 40), (41, 60), (61, 99)]
_LEAD_EDGES_H = [12, 24, 48]


def _pk_market_type(tk: str) -> str | None:
    kind = "HIGH" if "HIGH" in (tk or "") else ("LOW" if "LOW" in (tk or "") else None)
    strike = (tk or "").rsplit("-", 1)[-1][:1]
    return f"{kind}/{strike}" if (kind and strike in ("B", "T")) else None


def _pk_band_label(px) -> str | None:
    if px is None:
        return None
    for lo, hi in _BANDS:
        if lo <= px <= hi:
            return f"{lo}-{hi}c"
    return None


def _pk_lead_bucket(opened: str, settled: str) -> str | None:
    def _parse(s):
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            # python<3.11 rejects 4/5-digit fractional seconds (…23.8307+00:00) — ~22% of the live
            # log; strip the fraction and retry so lead buckets aren't interpreter-dependent (mirrors
            # yes_lever_decision.load_rows' guard, and kept identical to fill_edge_breakdown._lead_bucket).
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
    if h < _LEAD_EDGES_H[0]:
        return "<12h (same-day)"
    if h < _LEAD_EDGES_H[1]:
        return "12-24h"
    if h < _LEAD_EDGES_H[2]:
        return "24-48h"
    return ">=48h"


def _classify(r: dict) -> dict:
    """One row → its pocket membership per dimension (None = belongs to no pocket in that dim)."""
    return {
        "market_type": _pk_market_type(r.get("ticker", "")),
        "entry_band": _pk_band_label(r.get("entry_cents")),
        "lead_time": _pk_lead_bucket(r.get("opened_utc", ""), r.get("settled_at_utc", "")),
    }


FAMILY: list[tuple[str, str]] = (
    [(f"{k}/{s}", "market_type") for k in ("HIGH", "LOW") for s in ("B", "T")]
    + [(f"{lo}-{hi}c", "entry_band") for lo, hi in _BANDS]
    + [(lab, "lead_time") for lab in ("<12h (same-day)", "12-24h", "24-48h", ">=48h")]
)

# The lever each dimension maps to IF its pocket ever fires (operator action, never automated).
_LEVER_HINT = {
    "market_type": "live-only strike-type filter — NO standing env key yet (paper arm declined "
                   "2026-07-09); design + paper-validate before any flip",
    "entry_band": "KALSHI_WEATHER_PREMIUM_MIN_CENTS / MAX_CENTS on premium-live",
    "lead_time": "TBD — no lead-time lever exists for premium (scanner's live window exempts "
                 "premium); a fire mandates designing one, not improvising",
}


def family_hash() -> str:
    # Freezes BOTH the pocket family AND the decision thresholds — editing MIN_POCKET_EVENTS/ALPHA/
    # DROP_TOP_N/BOOTSTRAP_B after pre-registration must trip the integrity guard, not slip through
    # (the thresholds ARE the estimand). Hash is over the module defaults; a test's --bootstrap
    # override doesn't touch it.
    canon = {"version": 1, "family": [{"name": n, "dim": d} for n, d in FAMILY],
             "bands": _BANDS, "lead_edges_h": _LEAD_EDGES_H,
             "thresholds": {"min_events": MIN_POCKET_EVENTS, "alpha": ALPHA,
                            "bootstrap_B": BOOTSTRAP_B, "drop_top_n": DROP_TOP_N},
             "market_type": "kind(HIGH|LOW substring) / strike(first char of last '-' segment, B|T)"}
    return "sha256:" + hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _pocket_diff_ci(rows: list[dict], member, B: int = BOOTSTRAP_B,
                    alpha: float = ALPHA, seed: int = 5):
    """Joint event-clustered percentile CI for cw_edge(pocket) − cw_edge(complement).

    NOT _cw_diff_ci: that resamples the two sides' events INDEPENDENTLY (fine for YES vs NO,
    different markets), but a pocket and its complement SHARE events — one event can hold fills in
    two bands — so independent resampling would fake independence of the same weather outcome.
    Here the UNION event pool is resampled ONCE per iteration and each drawn event contributes its
    pocket rows to side A and its complement rows to side B, keeping shared events paired."""
    g_p: dict = defaultdict(list)
    g_c: dict = defaultdict(list)
    for r in rows:
        ev = _event(r.get("ticker", ""))
        (g_p if member(r) else g_c)[ev].append((float(r.get("pnl_cents", 0)), float(r["qty"])))
    p_pairs = [pr for ev in g_p for pr in g_p[ev]]
    c_pairs = [pr for ev in g_c for pr in g_c[ev]]
    if not p_pairs or not c_pairs:
        return (None, None, None)
    point = _ratio(p_pairs) - _ratio(c_pairs)
    if len(g_p) < 2 or len(g_c) < 2:
        return (point, None, None)
    union = sorted(set(g_p) | set(g_c))
    k = len(union)
    rng = random.Random(seed)
    samples = []
    for _ in range(B):
        pp: list = []
        cc: list = []
        for _ in range(k):
            ev = union[rng.randrange(k)]
            pp.extend(g_p.get(ev, ()))
            cc.extend(g_c.get(ev, ()))
        if pp and cc:                              # a draw can miss one side entirely — skip it
            samples.append(_ratio(pp) - _ratio(cc))
    if len(samples) < max(100, B // 10):           # too degenerate to trust
        return (point, None, None)
    samples.sort()
    n = len(samples)
    lo = samples[min(n - 1, max(0, int((alpha / 2) * n)))]
    hi = samples[min(n - 1, max(0, int((1 - alpha / 2) * n)))]
    return (point, lo, hi)


def _n_events(rows: list[dict]) -> int:
    return len({_event(r.get("ticker", "")) for r in rows})


def futility_decided(state_dir: Path) -> tuple[bool, str]:
    """The hard prerequisite: no pocket may fire before check_futility_checkpoint has ruled.
    Fail-closed — unreadable/missing futility state counts as NOT decided."""
    p = state_dir / "futility-checkpoint-state.json"
    try:
        s = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return False, "futility state unreadable/missing (fail-closed: not decided)"
    if s.get("anytime_verdict") is not None:
        return True, f"anytime CS ruled: {s['anytime_verdict'].get('decision')}"
    for cp in s.get("checkpoints", []):
        if cp.get("decision") is not None:
            return True, f"checkpoint n={cp.get('n_events')} ruled: {cp.get('decision')}"
    n = s.get("n_events_current")
    return False, f"awaiting first checkpoint ({n}/200 events)"


def evaluate(state_dir: Path, settled_through: date | None = None,
             fix_cutoff: str | None = None, B: int = BOOTSTRAP_B) -> dict:
    rows = load_rows(state_dir, settled_through=settled_through)

    # Regime clock: a cutoff restricts the WHOLE evaluation to post-cutoff events — a selection
    # change makes earlier fills a different data-generating process, so they don't just stop
    # counting toward (i), they leave the estimand entirely.
    cutoff_date = None
    if fix_cutoff:
        cutoff_date = datetime.strptime(fix_cutoff, "%y%b%d").date()
        rows = [r for r in rows if (_ticker_date(r.get("ticker", "")) is not None
                                    and _ticker_date(r.get("ticker", "")) >= cutoff_date)]

    fut_ok, fut_detail = futility_decided(state_dir)

    pockets = []
    wy_items: dict = {}
    for idx, (name, dim) in enumerate(FAMILY):
        member = lambda r, _n=name, _d=dim: _classify(r)[_d] == _n    # noqa: E731
        sub = [r for r in rows if member(r)]
        if sub:
            wy_items[name] = [(_event(r.get("ticker", "")),
                               float(r.get("pnl_cents", 0)) / r["qty"]) for r in sub]
        seed = 100 + idx                                   # deterministic, distinct per pocket

        def _guarded_ci(rr, method, s):
            """<2 events → a bootstrap 'CI' collapses to the point (lo==hi==point) and would
            vacuously satisfy 'hi<0'. Report the point but no interval — it can't gate."""
            if not rr:
                return (None, None, None)
            ci = _cw_cluster_ci(rr, B=B, method=method, seed=s)
            return ci if _n_events(rr) >= 2 else (ci[0], None, None)

        ci_pct = _guarded_ci(sub, "percentile", seed)
        ci_bca = _guarded_ci(sub, "bca", seed)
        diff_ci = _pocket_diff_ci(rows, member, B=B, seed=seed)
        drop_rows, dropped = _drop_top_n_events(sub, DROP_TOP_N)
        drop_ci = _guarded_ci(drop_rows, "percentile", seed + 50)
        pockets.append({
            "name": name, "dim": dim,
            "positions": len(sub), "contracts": sum(r["qty"] for r in sub),
            "events": _n_events(sub),
            "ci_pct": ci_pct, "ci_bca": ci_bca, "diff_ci": diff_ci, "drop_ci": drop_ci,
            "dropped_events": [{"event": ev, "net_pnl_cents": round(p, 1)} for ev, p in dropped],
            "qty_split": _qty_split(sub) if sub else None,
        })

    wy = westfall_young(wy_items, alpha=ALPHA, B=B) if wy_items else {}

    def _ci(t):
        return None if t[0] is None else {
            "point": round(t[0], 2),
            "lo": (round(t[1], 2) if t[1] is not None else None),
            "hi": (round(t[2], 2) if t[2] is not None else None),
        }

    out_pockets = []
    any_fire = False
    for p in pockets:
        w = wy.get(p["name"])
        gate = {
            "i_ge40_events": p["events"] >= MIN_POCKET_EVENTS,
            "ii_ev_ci_hi_neg": (p["ci_pct"][2] is not None and p["ci_pct"][2] < 0),
            "iii_diff_ci_hi_neg": (p["diff_ci"][2] is not None and p["diff_ci"][2] < 0),
            "iv_wy_reject": bool(w and w["reject"]),
            "v_survives_drop_top3": (p["drop_ci"][2] is not None and p["drop_ci"][2] < 0),
        }
        ii_bca = (p["ci_bca"][2] is not None and p["ci_bca"][2] < 0)
        method_agreement = (gate["ii_ev_ci_hi_neg"] == ii_bca)
        pocket_fire = all(gate.values()) and method_agreement
        if pocket_fire and fut_ok:
            verdict = f"FIRE-POCKET-{p['name']}"
            any_fire = True
        elif pocket_fire:
            verdict = "HOLD (blocked-pre-futility)"
        else:
            verdict = "HOLD"
        out_pockets.append({
            "name": p["name"], "dim": p["dim"], "lever": _LEVER_HINT[p["dim"]],
            "positions": p["positions"], "contracts": p["contracts"], "events": p["events"],
            "ev_ci_pct": _ci(p["ci_pct"]), "ev_ci_bca": _ci(p["ci_bca"]),
            "diff_vs_complement_ci_pct": _ci(p["diff_ci"]),
            "wy": ({"t": round(w["t"], 3), "adj_p": round(w["adj_p"], 4), "reject": w["reject"]}
                   if w else None),
            "drop_top3": {"dropped_events": p["dropped_events"], "surviving_ci_pct": _ci(p["drop_ci"])},
            "qty_split": p["qty_split"],
            "gate": gate, "method_agreement_pct_vs_bca": method_agreement,
            "conditions_met": sum(gate.values()),
            "verdict": verdict,
        })

    return {
        "generated_utc": _NOW(),
        "state_dir": str(state_dir),
        "settled_through": settled_through.isoformat() if settled_through else None,
        "regime_cutoff": fix_cutoff,
        "family_hash": family_hash(),
        "futility": {"decided": fut_ok, "detail": fut_detail},
        "counts": {"n_settled": len(rows), "n_events": _n_events(rows)},
        "pockets": out_pockets,
        "any_fire": any_fire,
    }


# ── frozen-estimand state (mirrors yes-lever-state.json) + transition alerts ──

_PREREGISTRATION = {
    "estimand": "per-pocket premium-live contract-weighted edge sum(pnl_cents)/sum(qty), after fees",
    "unit": "event-clustered (series+city+date)",
    "primary_ci": "percentile bootstrap, 95%",
    "crosscheck_ci": "BCa bootstrap, 95% (house standard; must agree on condition ii)",
    "thresholds": {"min_events": MIN_POCKET_EVENTS, "alpha": ALPHA,
                   "bootstrap_B": BOOTSTRAP_B, "drop_top_n": DROP_TOP_N},
    "multiplicity": "Westfall-Young max-t step-down across the 13-pocket family, event-clustered, "
                    "jointly resampled (equal-per-trade weighting — known divergence from the "
                    "contract-weighted gate estimand; both must reject = strictly conservative)",
    "family_exclusions": "SIDE (owned by yes_lever_decision.py's frozen gate), CITY (granularity/"
                         "data-mining; PREMIUM_SKIP_CITIES has its own path), interaction cells "
                         "(event counts collapse)",
    "gate": "fire pocket P ONLY if ALL of: (i) >=40 post-cutoff events; (ii) cw EV CI hi<0 "
            "(percentile, BCa agrees); (iii) pocket-complement JOINT diff CI hi<0; "
            "(iv) WY adj_p<0.05; (v) survives drop-top-3; AND futility n=200 verdict exists "
            "(a live selection change before that verdict splices regimes into the frozen "
            "anytime-valid futility stream)",
    "source": "state/<live-dir>/settlement-log.jsonl",
    "filter": "qty>0 AND entry_cents>0 AND is_weather_ticker",
    "reference": "session 2026-07-09 (post-182-fill selection-tightening decision)",
}


def _state_path(state_dir: Path) -> Path:
    return state_dir / "pocket-lever-state.json"


def _load_state(state_dir: Path) -> dict:
    p = _state_path(state_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"tool_version": "1.0", "created_utc": _NOW(),
            "preregistration": dict(_PREREGISTRATION,
                                    family=[{"name": n, "dim": d} for n, d in FAMILY],
                                    family_hash=family_hash()),
            "regime": {"cutoff": None, "history": []},
            "alerts": {"decidable": {}, "fired": {}}}


def _save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = _state_path(state_dir)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


def check_family_integrity(state: dict) -> bool:
    """The family is IMMUTABLE once frozen. A hash mismatch means someone edited the pocket
    definitions after pre-registration — refuse to evaluate rather than silently move the estimand
    (the futility tool's 'no continue' spirit)."""
    frozen = (state.get("preregistration") or {}).get("family_hash")
    return frozen is None or frozen == family_hash()


def _alert(msg: str, key: str) -> None:
    try:
        from trader.notify import alert
        alert(msg, key=key)
    except Exception as e:                       # notify is best-effort; never break the cycle
        print(f"  [pocket-lever] alert dispatch failed: {e}", file=sys.stderr)


def _alert_key(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _fingerprint(state_dir: Path, fix_cutoff: str | None, B: int) -> str:
    """Cheap change-detector so the ~13×4×B bootstrap only reruns when something that can change a
    verdict changed: the settlement log, the regime cutoff, B, or futility becoming decided."""
    rows = load_rows(state_dir)
    last = max((r.get("settled_at_utc") or "" for r in rows), default="")
    fut_ok, _ = futility_decided(state_dir)
    return f"nrows={len(rows)}|last_settled={last}|cutoff={fix_cutoff}|B={B}|futility={fut_ok}"


def _fmt_ci(c) -> str:
    if not c:
        return "n/a"
    if c["lo"] is None:
        return f"{c['point']:+.2f}¢ [degenerate]"
    return f"{c['point']:+.2f}¢ [{c['lo']:+.2f}, {c['hi']:+.2f}]"


_DIM_LABEL = {"market_type": "MARKET TYPE", "entry_band": "ENTRY BAND", "lead_time": "LEAD TIME"}


def report(d: dict) -> str:
    L = ["\nPOCKET-LEVER DECISION — pre-registered 13-pocket family (report+alert only)", "=" * 78]
    L.append(f"book={d['state_dir']}"
             + (f"  as-of≤{d['settled_through']}" if d["settled_through"] else "")
             + (f"  regime-cutoff={d['regime_cutoff']}" if d["regime_cutoff"] else ""))
    c = d["counts"]
    fut = d["futility"]
    L.append(f"settled={c['n_settled']}  events={c['n_events']}  "
             f"futility-prerequisite: {'DECIDED — ' if fut['decided'] else 'NOT DECIDED — '}{fut['detail']}")
    if not fut["decided"]:
        L.append("  ⛔ ALL pockets clamped to HOLD until the futility n=200 verdict exists "
                 "(pre-registered: no selection change may splice the frozen futility stream).")
    L.append("  gate: ≥40ev ∧ EV-CI<0 (pct+BCa) ∧ diff-CI<0 ∧ WY adj_p<.05 ∧ drop-top-3  → operator flip")
    for dim in ("market_type", "entry_band", "lead_time"):
        L.append(f"\n{_DIM_LABEL[dim]}  (worst contract-weighted EV first):")
        ps = sorted((p for p in d["pockets"] if p["dim"] == dim),
                    key=lambda p: (p["ev_ci_pct"] or {"point": 0.0})["point"])
        for p in ps:
            wy = p["wy"]
            L.append(f"  {p['name']:>16}  ev={p['events']:>3} n={p['positions']:>3} "
                     f"ct={p['contracts']:>4}  EV {_fmt_ci(p['ev_ci_pct']):>26}  "
                     f"vs-rest {_fmt_ci(p['diff_vs_complement_ci_pct']):>26}  "
                     f"adj_p={wy['adj_p'] if wy else 'n/a'}"
                     f"  [{p['conditions_met']}/5] {p['verdict']}")
            if not p["method_agreement_pct_vs_bca"]:
                L.append(f"  {'':>16}  ⚠ percentile vs BCa DISAGREE on (ii) — estimator boundary, do not trust")
    fires = [p for p in d["pockets"] if p["verdict"].startswith("FIRE")]
    blocked = [p for p in d["pockets"] if p["verdict"] == "HOLD (blocked-pre-futility)"]
    L.append("")
    if fires:
        for p in fires:
            L.append(f"  🔥 {p['verdict']}: operator lever → {p['lever']}")
            L.append("     Flipping REQUIRES: --fix-cutoff regime reset here + futility-stream discussion.")
    elif blocked:
        L.append(f"  VERDICT: HOLD — {len(blocked)} pocket(s) pass the statistical gate but are "
                 f"BLOCKED pending the futility verdict: {', '.join(p['name'] for p in blocked)}")
    else:
        L.append("  VERDICT: HOLD — no pocket meets the pre-registered gate.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-registered pocket-lever decision (report+alert only).")
    ap.add_argument("--state-dir", default="state/live-premium")
    ap.add_argument("--settled-through", default=None,
                    help="only settlements on/before this date (YYYY-MM-DD) — as-of reproduction; "
                         "state is NOT written for as-of runs")
    ap.add_argument("--fix-cutoff", default=None,
                    help="regime start, ticker date format YYMMMDD; restricts the WHOLE evaluation "
                         "to post-cutoff events (use after any live selection change)")
    ap.add_argument("--bootstrap", type=int, default=BOOTSTRAP_B)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="persist <state-dir>/pocket-lever-state.json + alert on transitions")
    args = ap.parse_args()

    state_dir = ROOT / args.state_dir if not Path(args.state_dir).is_absolute() else Path(args.state_dir)
    through = None
    if args.settled_through:
        through = datetime.strptime(args.settled_through, "%Y-%m-%d").date()
    if args.fix_cutoff:
        try:
            datetime.strptime(args.fix_cutoff, "%y%b%d")
        except ValueError:
            print(f"[pocket-lever] bad --fix-cutoff {args.fix_cutoff!r} (want YYMMMDD, e.g. 26JUL06)",
                  file=sys.stderr)
            return 2

    st = _load_state(state_dir)
    if not check_family_integrity(st):
        print("🛑 [pocket-lever] FAMILY HASH MISMATCH: the pocket definitions in this tool no longer "
              "match the pre-registered family frozen in state. The family is immutable — revert the "
              "definition change, or consciously retire the state file and re-register (new clock).",
              file=sys.stderr)
        return 3

    # The regime cutoff is PERSISTENT: once an operator sets it (after a live selection change), the
    # standing cron runs WITHOUT --fix-cutoff must INHERIT it, never silently clear it. Only an
    # explicit --fix-cutoff on the CLI changes it (recorded below). Without this, the daily cron
    # would revert the operator's reset and re-evaluate on the spliced pre-change data — the exact
    # contamination this tool exists to prevent.
    persisted_cutoff = (st.get("regime") or {}).get("cutoff")
    effective_cutoff = args.fix_cutoff if args.fix_cutoff is not None else persisted_cutoff

    # Fingerprint short-circuit (standing runs only): settlements land ~once a day; don't pay
    # ~13×4×B bootstraps per 20-min cycle when nothing decision-relevant changed.
    fp = None
    if through is None:
        fp = _fingerprint(state_dir, effective_cutoff, args.bootstrap)
        if args.write and st.get("last_fingerprint") == fp and st.get("last_reading"):
            lr = st["last_reading"]
            print(f"[pocket-lever] no new settlements (fingerprint match) — cached verdict stands: "
                  f"any_fire={lr.get('any_fire')}, "
                  f"{sum(1 for p in lr.get('pockets', []) if p.get('events', 0) >= MIN_POCKET_EVENTS)}"
                  f"/13 pockets decidable. Full report: rerun without --write.")
            return 0

    d = evaluate(state_dir, settled_through=through, fix_cutoff=effective_cutoff, B=args.bootstrap)
    print(json.dumps(d, indent=2) if args.json else report(d))

    if args.write and through is not None:
        print("\n[pocket-lever] as-of run (--settled-through): state NOT written.", file=sys.stderr)
    elif args.write:
        # Regime change bookkeeping: ONLY an explicit --fix-cutoff that differs from the persisted
        # value moves the clock (a plain cron run inherits persisted_cutoff and changes nothing).
        if args.fix_cutoff is not None and args.fix_cutoff != persisted_cutoff:
            st.setdefault("regime", {"cutoff": None, "history": []})
            st["regime"]["history"].append({"old": persisted_cutoff, "new": args.fix_cutoff,
                                            "changed_utc": _NOW()})
            st["regime"]["cutoff"] = args.fix_cutoff
            st["alerts"] = {"decidable": {}, "fired": {}}
            print(f"\n⚠️  [pocket-lever] REGIME CUTOFF CHANGED {persisted_cutoff!r} → {args.fix_cutoff!r}: "
                  f"all pocket clocks and alert flags reset.", file=sys.stderr)
            if not d["futility"]["decided"]:
                print("⚠️  [pocket-lever] regime moved while the futility stream is UNDECIDED — "
                      "verify the futility estimand is still meaningful.", file=sys.stderr)
        st["last_run_utc"] = _NOW()
        st["last_fingerprint"] = fp
        st["last_reading"] = {
            "counts": d["counts"], "any_fire": d["any_fire"], "futility": d["futility"],
            "pockets": [{"name": p["name"], "events": p["events"],
                         "ev_ci_pct": p["ev_ci_pct"], "adj_p": (p["wy"] or {}).get("adj_p"),
                         "conditions_met": p["conditions_met"], "verdict": p["verdict"]}
                        for p in d["pockets"]],
        }
        alerts = st.setdefault("alerts", {"decidable": {}, "fired": {}})
        for p in d["pockets"]:
            k = _alert_key(p["name"])
            if (p["events"] >= MIN_POCKET_EVENTS and d["futility"]["decided"]
                    and not alerts["decidable"].get(k)):
                alerts["decidable"][k] = _NOW()
                _alert(f"Pocket lever {p['name']} now DECIDABLE: {p['events']} events "
                       f"(≥{MIN_POCKET_EVENTS}) and the futility verdict exists. Review the "
                       f"pre-registered gate ({p['conditions_met']}/5 conditions). Report only.",
                       key=f"pocket_lever_decidable_{k}")
            if p["verdict"].startswith("FIRE") and not alerts["fired"].get(k):
                alerts["fired"][k] = _NOW()
                _alert(f"POCKET-LEVER GATE FIRED for {p['name']}: all pre-registered conditions met "
                       f"(EV {_fmt_ci(p['ev_ci_pct'])}, WY adj_p={(p['wy'] or {}).get('adj_p')}). "
                       f"Operator lever: {p['lever']}. Any flip needs a --fix-cutoff regime reset. "
                       f"Report only; your call.", key=f"pocket_lever_fire_{k}")
        _save_state(state_dir, st)
        print(f"\n[written] {_state_path(state_dir)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

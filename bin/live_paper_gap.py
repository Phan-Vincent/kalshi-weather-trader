#!/usr/bin/env python3
"""bin/live_paper_gap.py — paired live-vs-paper tracker + gap decomposition.

Quant review 2026-07-01 experiment #6. Paper P&L is a ~6x-inflated proxy for the live edge (§2.5:
matched paired gap persisted after the 2026-06-29 FILL_REALISM fix). This tool does the ONLY
legitimate thing with paper — track the per-contract PAPER-minus-LIVE gap on SHARED markets,
event-clustered, as a monitoring instrument — and decomposes that gap into its two mechanisms:

  TOTAL   = paper full-book per-ct  −  live full-book per-ct        (the headline inflation)
  EXEC    = mean over JOINTLY-filled tickers of (paper_per_ct − live_per_ct)   [= pairwise diff_ci]
            → better paper fill price + paper never pays post-fill adverse markout (it charges a
              STATIC ~5.6c entry haircut, live pays a DYNAMIC markout that drifts against the fill)
  SELECT  = TOTAL − EXEC (residual)                                 → paper books P&L on events
            live never filled (fill-selection), net of each arm's all-vs-joint composition shift

The decisive gate is the EXEC CI (the paired, best-powered estimate); SELECT is a residual and is
reported as suggestive, corroborated by the DIRECTLY-measured live markout from markout-log.jsonl.
Per-contract throughout (paper trades ~40 lots/mkt vs live ~5 — a $-gap would be a pure size
artifact). Read-only; writes only the JSON artifact under --write.

2026-07-12 (entry-timing fix; see reviews/live-vs-paper-gap-2026-07-12.md): every live settlement
row is synced_from_kalshi (source=live_settlement_reconcile), so its opened_utc is fill-DISCOVERY
time — when the :10/:40 sync found the fill (overnight pickoffs surface at ~05:00Z when no cron
trades) — NOT entry time. Entries are now anchored to the first maker POST in maker-lifecycle.jsonl
('posted'/'posted_live'); rows resolvable only to discovery time are labeled and EXCLUDED from
timing reads. On top of the canonical decomposition (semantics unchanged — additive only) the tool
reports the paper↔live entry skew on joint same-side tickers and an EXEC estimate restricted to
same-cycle pairs (|Δt| ≤ --same-cycle-minutes, default 60): a drift-free execution read that the
multi-hour clock skew between the hourly paper cron and the 6×/day live cron can't contaminate.

Usage:  python3 bin/live_paper_gap.py [--write] [--same-cycle-minutes N]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import (  # noqa: E402  (reuse the validated pairing/clustering instruments)
    _per_market_stats, pairwise, _event, cluster_bootstrap_ci, _read_jsonl,
)
from live_fill_quality import _our_posted_tickers, _mean_markout  # noqa: E402  (direct live markout)

PAPER_DIR = ROOT / "state" / "paper-premium"   # variant `premium`, offset 0 — the live arm's twin
LIVE_DIR = ROOT / "state" / "live-premium"      # variant `premium-live`, offset 0
MIN_EVENTS = 5                                   # matches pairwise's own too-few-events guard


def _pt(ci):
    return ci[0] if ci else None


def _excludes_zero(ci) -> bool:
    _, lo, hi = ci
    if lo is None or lo == hi:      # a zero-width (degenerate) bootstrap CI = no variance info, not significance
        return False
    return (lo > 0 and hi > 0) or (lo < 0 and hi < 0)


def _pc_items(stats: dict, keys=None) -> list[tuple]:
    """[(event, pnl_cents/qty)] per ticker — the canonical per-ticker, event-clustered convention
    (identical to what pairwise/_per_market_stats use; do NOT average within-event first)."""
    keys = stats.keys() if keys is None else keys
    return [(_event(stats[t]["ticker"]), stats[t]["pnl"] / stats[t]["qty"])
            for t in keys if stats.get(t) and stats[t]["qty"] > 0]


def _sides_by_ticker(state_dir: Path) -> dict:
    """ticker -> set of sides actually traded (qty>0, non-gap), to flag side-disagreement pairs."""
    out: dict = {}
    for s in _read_jsonl(state_dir / "settlement-log.jsonl"):
        if (s.get("qty") or 0) <= 0 or s.get("gap_settled"):
            continue
        out.setdefault(s.get("ticker", ""), set()).add(s.get("side"))
    return out


_POSTED_EVENTS = ("posted", "posted_live")   # paper lifecycle logs 'posted', live logs 'posted_live'


_FRAC_RE = re.compile(r"\.(\d+)")


def _parse_ts(s) -> _dt.datetime | None:
    """ISO-8601 → aware-UTC datetime; naive treated as UTC; None on missing/garbage.

    3.9.6's datetime.fromisoformat (the cron interpreter) accepts ONLY 3- or 6-digit
    fractional seconds, but Kalshi strips trailing zeros (e.g. '…08.19355Z', '…08.6Z') —
    valid ISO-8601 that 3.9 rejects. ~10% of live settlement opened_utc rows carry such
    fractions, so normalize any fractional field to exactly 6 digits (pad/truncate,
    same instant) before parsing. Without this those rows silently return None and drop
    out of the synced_only_excluded count (QA 2026-07-12)."""
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    m = _FRAC_RE.search(s)
    if m:
        s = s[:m.start()] + "." + (m.group(1) + "000000")[:6] + s[m.end():]
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _entry_ts_by_ticker(state_dir: Path) -> dict:
    """ticker -> {"ts": aware datetime, "source": "lifecycle"|"opened_utc"|"synced_discovery"}.

    True ENTRY time = first maker POST from maker-lifecycle.jsonl. settlement-log opened_utc is only
    a fallback: on the live book every row is synced_from_kalshi, so opened_utc there is when the
    SYNC DISCOVERED the fill (hours after the post for overnight pickoffs) — those fall back with
    source='synced_discovery' so callers can exclude them from timing arithmetic."""
    out: dict = {}
    for r in _read_jsonl(state_dir / "maker-lifecycle.jsonl"):
        if r.get("event") in _POSTED_EVENTS and r.get("ticker"):
            ts = _parse_ts(r.get("ts"))
            if ts is None:
                continue
            cur = out.get(r["ticker"])
            if cur is None or ts < cur["ts"]:
                out[r["ticker"]] = {"ts": ts, "source": "lifecycle"}
    fallback: dict = {}
    for s in _read_jsonl(state_dir / "settlement-log.jsonl"):
        if (s.get("qty") or 0) <= 0 or s.get("gap_settled"):
            continue
        tk = s.get("ticker", "")
        ts = _parse_ts(s.get("opened_utc"))
        if not tk or ts is None:
            continue
        src = "synced_discovery" if s.get("synced_from_kalshi") else "opened_utc"
        cur = fallback.get(tk)
        if cur is None or ts < cur["ts"]:
            fallback[tk] = {"ts": ts, "source": src}
    for tk, v in fallback.items():
        out.setdefault(tk, v)
    return out


_DOMINANCE = 1.5   # exec must be >=1.5x the selection residual to claim it "dominates" (avoid knife-edge)


def _classify(exec_ci, selection_pt, n_events) -> str:
    if exec_ci is None or exec_ci[0] is None or n_events < MIN_EVENTS:
        return f"INSUFFICIENT DATA — only {n_events} matched events (need ≥{MIN_EVENTS}) to decompose"
    exec_pt = exec_ci[0]
    sel = abs(selection_pt or 0.0)
    clear = _excludes_zero(exec_ci)
    # Direction-aware wording: EXEC = paper_per_ct − live_per_ct on joint markets. >0 ⇒ paper over-books
    # live; <0 ⇒ live out-executes paper (its dynamic markout beat paper's static entry haircut).
    direction = ("paper over-books vs live — it under-charges post-fill markout / fills at a better price"
                 if exec_pt > 0 else
                 "live OUT-executes paper on joint markets (paper's per-ct diff is negative)")
    if clear:
        if abs(exec_pt) >= _DOMINANCE * sel:
            return f"MOSTLY EXECUTION — {direction} (exec CI excludes 0 and dominates the selection residual)"
        return (f"BOTH — execution confirmed (exec CI excludes 0: {direction}) plus a comparable selection "
                "residual; selection is a residual, not independently CI-tested")
    # execution within noise → cannot confirm it; be explicit that the residual is untested either way
    if sel > abs(exec_pt):
        return ("SUGGESTS SELECTION — execution is within noise; the selection residual is the larger "
                "component (residual not CI-tested — suggestive only)")
    return (f"INCONCLUSIVE — execution is the larger component but its CI spans 0 (not significant at "
            f"n_events={n_events}); selection not CI-tested; the gap is not confidently attributable")


def evaluate(paper_dir: Path = None, live_dir: Path = None, same_cycle_minutes: int = 60) -> dict:
    paper_dir = paper_dir or PAPER_DIR
    live_dir = live_dir or LIVE_DIR
    paper = _per_market_stats(paper_dir, by_ticker_only=True)
    live = _per_market_stats(live_dir, by_ticker_only=True)

    joint = set(paper) & set(live)
    paper_only = set(paper) - set(live)
    live_only = set(live) - set(paper)

    # Per-arm full-book per-ct (canonical per-ticker), event-clustered
    paper_all_ci = cluster_bootstrap_ci(_pc_items(paper)) if paper else (None, None, None)
    live_all_ci = cluster_bootstrap_ci(_pc_items(live)) if live else (None, None, None)
    total = (_pt(paper_all_ci) - _pt(live_all_ci)
             if _pt(paper_all_ci) is not None and _pt(live_all_ci) is not None else None)

    # EXEC component = paired diff on jointly-filled tickers (== pairwise diff_ci)
    pw = pairwise(paper_dir, live_dir, by_ticker_only=True)
    exec_ci = pw["diff_ci"]
    exec_pt = _pt(exec_ci)

    # Side-disagreement pairs (pairwise differences a paper-YES vs a live-NO as if same-side)
    ps, ls = _sides_by_ticker(paper_dir), _sides_by_ticker(live_dir)
    side_disagree = sorted(t for t in joint if ps.get(t) and ls.get(t) and not (ps[t] & ls[t]))
    # Robustness: EXEC recomputed EXCLUDING those tickers, so a directional/side difference can't
    # masquerade as an execution-quality difference in the decisive number.
    # sorted(): set iteration order varies with hash randomization, and the seeded bootstrap resamples
    # by index — unsorted items made the printed CI bounds jitter across runs on IDENTICAL data
    # (caught in the 2026-07-12 QA determinism check). Point estimate was never affected.
    exec_ex_items = [(_event(paper[t]["ticker"]), paper[t]["pnl"] / paper[t]["qty"] - live[t]["pnl"] / live[t]["qty"])
                     for t in sorted(joint - set(side_disagree))
                     if paper[t]["qty"] > 0 and live[t]["qty"] > 0]
    exec_ex_ci = cluster_bootstrap_ci(exec_ex_items) if exec_ex_items else (None, None, None)

    # SELECT residual + the opposite-signed "direct" split (sign-trap: report both, labeled)
    selection = (total - exec_pt) if (total is not None and exec_pt is not None) else None
    paper_joint_ci = cluster_bootstrap_ci(_pc_items(paper, joint)) if joint else (None, None, None)
    paper_only_ci = cluster_bootstrap_ci(_pc_items(paper, paper_only)) if paper_only else (None, None, None)
    direct_selection = (_pt(paper_all_ci) - _pt(paper_joint_ci)
                        if _pt(paper_all_ci) is not None and _pt(paper_joint_ci) is not None else None)

    # ENTRY TIMING (2026-07-12): anchor entries to first maker POST; skew = live − paper, minutes.
    # Same-side joint tickers only (a side flip is a different decision, not an execution read), and
    # only pairs whose LIVE entry is lifecycle-backed — synced discovery times would re-import the
    # exact fill-time/post-time confusion this block exists to remove. The same-cycle EXEC subset
    # (|Δt| ≤ same_cycle_minutes) is the drift-free execution estimate: both arms decided on the
    # same (or adjacent) fair-value build and market state.
    pe, le = _entry_ts_by_ticker(paper_dir), _entry_ts_by_ticker(live_dir)
    skews, synced_only = [], 0
    for t in sorted(joint - set(side_disagree)):
        pv, lv = pe.get(t), le.get(t)
        if not pv or not lv:
            continue
        # LIVE must be lifecycle-backed (all its settlement rows are synced ⇒ opened_utc is discovery
        # time); PAPER may fall back to opened_utc (booked in-cycle) but never to a synced discovery
        # ts — evaluate() is generic, so guard BOTH sides against re-importing the confound.
        if lv["source"] != "lifecycle" or pv["source"] == "synced_discovery":
            synced_only += 1
            continue
        skews.append((t, (lv["ts"] - pv["ts"]).total_seconds() / 60.0))
    median_skew = statistics.median(s for _, s in skews) if skews else None
    sc_items = [(_event(paper[t]["ticker"]), paper[t]["pnl"] / paper[t]["qty"] - live[t]["pnl"] / live[t]["qty"])
                for t, s in skews if abs(s) <= same_cycle_minutes
                and paper[t]["qty"] > 0 and live[t]["qty"] > 0]
    sc_ci = cluster_bootstrap_ci(sc_items) if sc_items else (None, None, None)

    # Direct live markout corroboration (independent of the P&L residual). Attribute to OUR posted
    # weather tickers only; if that set is empty (no maker-lifecycle posted_live rows) we CANNOT
    # attribute, so report n/a — never fall back to attributing ALL rows (that re-admits the
    # non-weather commingle _our_posted_tickers exists to exclude).
    mk = _read_jsonl(live_dir / "markout-log.jsonl")
    our = _our_posted_tickers(live_dir)
    live_markout, mk_n = (_mean_markout(mk, our_tickers=our) if (mk and our) else (None, 0))
    if not mk_n:
        live_markout = None

    n_events = pw["matched_events"]
    return {
        "total_gap": {"point": total, "paper_all_ci": paper_all_ci, "live_all_ci": live_all_ci},
        "execution": {"ci": exec_ci, "ci_ex_side_disagree": exec_ex_ci,
                      "matched_markets": pw["matched_markets"], "matched_events": n_events,
                      "qty_per_mkt_paper": pw["qty_per_mkt_a"], "qty_per_mkt_live": pw["qty_per_mkt_b"]},
        "selection_residual": {"point": selection, "direct_selection": direct_selection,
                               "paper_joint_ci": paper_joint_ci, "paper_only_ci": paper_only_ci},
        "sets": {"joint": len(joint), "paper_only": len(paper_only), "live_only": len(live_only)},
        "entry_timing": {"timed_pairs": len(skews), "median_skew_min": median_skew,
                         "synced_only_excluded": synced_only,
                         "same_cycle_minutes": same_cycle_minutes,
                         "same_cycle": {"ci": sc_ci, "markets": len(sc_items),
                                        "events": len({e for e, _ in sc_items})}},
        "side_disagreement": side_disagree,
        "live_markout": {"mean_c": live_markout, "n": mk_n},
        "verdict": _verdict(exec_ci, exec_ex_ci, selection, n_events, side_disagree),
    }


def _verdict(exec_ci, exec_ex_ci, selection, n_events, side_disagree) -> str:
    """Primary classification on the canonical (ticker-summed) EXEC, annotated with a sensitivity
    note when dropping the side-disagreement tickers would change the classification — so a headline
    driven by a few directionally-confounded markets can't stand unqualified."""
    v = _classify(exec_ci, selection, n_events)
    if side_disagree and exec_ex_ci and exec_ex_ci[0] is not None:
        alt = _classify(exec_ex_ci, selection, n_events)
        head, alt_head = v.split("—")[0].strip(), alt.split("—")[0].strip()
        if head != alt_head:
            v += (f"  [sensitivity: excluding {len(side_disagree)} side-disagreement ticker(s) → {alt_head}]")
    return v


def _fmt(ci):
    return "n/a" if not ci or ci[0] is None else f"{ci[0]:+.2f}¢ [{ci[1]:+.2f}, {ci[2]:+.2f}]"


def _fmtpt(x):
    return "n/a" if x is None else f"{x:+.2f}¢"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="persist state/live-premium/live-paper-gap.json")
    ap.add_argument("--same-cycle-minutes", type=int, default=60,
                    help="max |live−paper| entry skew (POST-time) for the same-cycle EXEC subset (default 60)")
    args = ap.parse_args()
    r = evaluate(same_cycle_minutes=args.same_cycle_minutes)
    tg, ex, sel = r["total_gap"], r["execution"], r["selection_residual"]
    print("[live-paper-gap] premium arm — paper (state/paper-premium) vs live (state/live-premium), "
          "per-contract, event-clustered")
    print(f"  matched: {ex['matched_markets']} tickers / {ex['matched_events']} events  "
          f"(joint={r['sets']['joint']}, paper-only={r['sets']['paper_only']}, live-only={r['sets']['live_only']})")
    print(f"  book size: paper {ex['qty_per_mkt_paper']:.1f} lots/mkt vs live {ex['qty_per_mkt_live']:.1f} "
          f"(→ per-contract is mandatory)")
    print(f"  paper full-book per-ct: {_fmt(tg['paper_all_ci'])}   live full-book per-ct: {_fmt(tg['live_all_ci'])}")
    print(f"  TOTAL gap (paper−live):        {_fmtpt(tg['point'])}")
    print(f"  EXECUTION/markout (paired):    {_fmt(ex['ci'])}   ← decisive component")
    if r["side_disagreement"]:
        print(f"     ex. {len(r['side_disagreement'])} side-disagreement tickers: {_fmt(ex['ci_ex_side_disagree'])} "
              f"(robustness — same-side only)")
    et = r["entry_timing"]
    if et["timed_pairs"]:
        excl = (f"; {et['synced_only_excluded']} pair(s) excluded (fill-discovery ts only)"
                if et["synced_only_excluded"] else "")
        print(f"  ENTRY TIMING (joint same-side, POST-anchored): {et['timed_pairs']} pairs, "
              f"median skew live−paper = {et['median_skew_min']:+.0f}m{excl}")
        sc = et["same_cycle"]
        if sc["ci"][0] is not None:
            print(f"  EXEC same-cycle only (|Δt|≤{et['same_cycle_minutes']}m): {_fmt(sc['ci'])} "
                  f"({sc['markets']} mkts / {sc['events']} events)   ← drift-free execution read")
        else:
            print(f"  EXEC same-cycle only (|Δt|≤{et['same_cycle_minutes']}m): n/a — no qualifying pairs")
    else:
        print("  ENTRY TIMING: n/a — no lifecycle POST times for joint same-side tickers")
    print(f"  SELECTION residual (TOTAL−EXEC): {_fmtpt(sel['point'])}   "
          f"(direct split, opposite sign: {_fmtpt(sel['direct_selection'])})")
    lm = r["live_markout"]
    if lm["mean_c"] is not None:
        print(f"  direct live markout (corroboration): {lm['mean_c']:+.2f}¢ (n={lm['n']}, negative=adverse)")
    if r["side_disagreement"]:
        print(f"  ⚠ {len(r['side_disagreement'])} side-disagreement tickers (paper vs live opposite side): "
              f"{', '.join(r['side_disagreement'][:5])}{'…' if len(r['side_disagreement']) > 5 else ''}")
    print(f"  VERDICT: {r['verdict']}")

    if args.write:
        try:
            out = LIVE_DIR / "live-paper-gap.json"
            tmp = out.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(r, f, indent=2)
            os.replace(tmp, out)
            print(f"  [wrote] {out}")
        except Exception as e:
            print(f"  [warn] artifact write failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

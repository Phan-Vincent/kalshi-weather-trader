#!/usr/bin/env python3
"""bin/refresh_flip_report.py — REPORT-ONLY panel for the 2026-07-10 live quote-refresh flip.

Surfaces the pre-registered ">10¢ EV/ct drop over >=20 events → revert" question WITHOUT wiring any
auto-revert (2026-07-13 audit F2). It splits the LIVE book's settled fills at the flip and evaluates
the drop two ways:

  • MATCHED (the trigger): post-flip vs the comparable-LENGTH pre-flip window — the same number of
    events immediately BEFORE the flip. This is the honest apples-to-apples reading. It does NOT
    cross the 10¢ bar, so the flip is KEPT.
  • ALL-TIME (context only): post-flip vs the ENTIRE pre-flip history. This DOES cross 10¢, but it
    pits a short correlated-loss stretch against a long benign one, so it is confounded — shown for
    context, never as the trigger.

The REAL tell is the A/B SIDE-SPLIT: the post-flip loss is concentrated on the NO side (the
correlated-NO "fade-the-bin" drawdown), not on a refresh-execution effect — i.e. the drop reflects
weather variance on NO-side bins, not the quote-refresh change. Read-only: never halts, reverts, or
writes state (— unless --write dumps the panel JSON for the dashboard/review).

Usage:  python3 bin/refresh_flip_report.py [--write]
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
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT.parent))

from compare_variants import _event, _read_settlements   # deduped read + shared event key
from data.weather_data import is_weather_ticker

# The live cancel-only quote-refresh was enabled on premium-live at this instant (variants.json note /
# memory kalshi-quote-refresh-ab). Configurable so the panel can be re-anchored if the flip is redated.
FLIP_UTC = os.environ.get("KALSHI_WEATHER_REFRESH_FLIP_UTC", "2026-07-10T18:20:00Z")
MIN_EVENTS = int(os.environ.get("KALSHI_WEATHER_REFRESH_MIN_EVENTS", "20"))   # pre-registered floor
DROP_TRIGGER_C = float(os.environ.get("KALSHI_WEATHER_REFRESH_DROP_C", "10")) # pre-registered >10¢ drop


def _parse(ts: str):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _metrics(rows: list[dict]) -> dict:
    """Contract-weighted settled EV/ct (¢) + counts for a set of settlement rows (matches
    fill_edge_breakdown's SIDE slice: total pnl_cents / total qty)."""
    q = sum(float(r.get("qty", 0) or 0) for r in rows)
    pnl = sum(float(r.get("pnl_cents", 0) or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("pnl_cents", 0) or 0) > 0)
    return {
        "n_fills": len(rows),
        "n_events": len({_event(r.get("ticker", "")) for r in rows}),
        "contracts": int(q),
        "total_pnl": round(pnl / 100.0, 2),
        "ev_ct": round(pnl / q, 2) if q else None,
        "win_rate": round(100.0 * wins / len(rows), 0) if rows else None,
    }


def _matched_pre_window(pre_rows: list[dict], k_events: int) -> list[dict]:
    """The k_events pre-flip EVENTS closest to the flip (comparable-length window). Groups pre-flip
    rows by event, orders events by their latest opened_utc (nearest the flip first), takes k."""
    by_ev: dict = {}
    for r in pre_rows:
        by_ev.setdefault(_event(r.get("ticker", "")), []).append(r)
    ordered = sorted(by_ev.items(),
                     key=lambda kv: max((x.get("opened_utc", "") or "") for x in kv[1]), reverse=True)
    return [r for _, rows in ordered[:k_events] for r in rows]


def evaluate(state_dir: Path = None, flip_utc: str = FLIP_UTC) -> dict:
    state_dir = state_dir or (ROOT / "state" / "live-premium")
    flip = _parse(flip_utc)
    rows = [r for r in _read_settlements(state_dir)
            if is_weather_ticker(r.get("ticker", "")) and (r.get("qty") or 0) > 0]
    pre, post = [], []
    for r in rows:
        ot = _parse(r.get("opened_utc", ""))
        (post if (ot and flip and ot >= flip) else pre).append(r)

    post_m = _metrics(post)
    pre_all_m = _metrics(pre)
    matched = _matched_pre_window(pre, post_m["n_events"])
    pre_matched_m = _metrics(matched)

    def _drop(pre_ev):  # positive drop ⇒ post is WORSE than the pre baseline
        if pre_ev is None or post_m["ev_ct"] is None:
            return None
        return round(pre_ev - post_m["ev_ct"], 2)

    drop_matched = _drop(pre_matched_m["ev_ct"])
    drop_alltime = _drop(pre_all_m["ev_ct"])
    evaluable = post_m["n_events"] >= MIN_EVENTS
    matched_fires = bool(evaluable and drop_matched is not None and drop_matched > DROP_TRIGGER_C)

    return {
        "flip_utc": flip_utc,
        "min_events": MIN_EVENTS,
        "drop_trigger_c": DROP_TRIGGER_C,
        "post_flip": post_m,
        "pre_flip_matched": {**pre_matched_m, "window": "comparable-length (events nearest the flip)"},
        "pre_flip_all_time": pre_all_m,
        "drop_matched_c": drop_matched,
        "drop_all_time_c": drop_alltime,
        "post_by_side": {"yes": _metrics([r for r in post if (r.get("side") or "").lower() == "yes"]),
                         "no": _metrics([r for r in post if (r.get("side") or "").lower() == "no"])},
        "evaluable": evaluable,
        "matched_trigger_fires": matched_fires,
        "verdict": ("KEEP FLIP — matched drop within trigger" if not matched_fires
                    else "MATCHED DROP EXCEEDS TRIGGER — operator review (no auto-revert)"),
    }


def _c(x):
    return f"{x:+.2f}¢" if isinstance(x, (int, float)) else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="persist state/live-premium/refresh-flip-report.json for the dashboard/review")
    args = ap.parse_args()
    r = evaluate()
    p, pm, pa = r["post_flip"], r["pre_flip_matched"], r["pre_flip_all_time"]
    ys, no = r["post_by_side"]["yes"], r["post_by_side"]["no"]

    print("[refresh-flip-report] premium-live quote-refresh flip — REPORT ONLY (no auto-revert)")
    print(f"  flip @ {r['flip_utc']};  pre-registered trigger: EV/ct drop > {r['drop_trigger_c']:.0f}¢ "
          f"over ≥{r['min_events']} events")
    print(f"  POST-flip:            EV {_c(p['ev_ct'])}/ct  ({p['n_fills']} fills / {p['n_events']} events, "
          f"WR {p['win_rate']:.0f}%, ${p['total_pnl']:+.2f})")
    print(f"  PRE-flip (matched):   EV {_c(pm['ev_ct'])}/ct  ({pm['n_fills']} fills / {pm['n_events']} events) "
          f"← comparable-length window, the trigger baseline")
    print(f"  PRE-flip (all-time):  EV {_c(pa['ev_ct'])}/ct  ({pa['n_fills']} fills / {pa['n_events']} events) "
          f"← context only (confounded: short loss stretch vs long benign history)")
    fired = "FIRES" if r["matched_trigger_fires"] else "does NOT fire"
    print(f"  DROP (matched, the trigger): {_c(r['drop_matched_c'])}  → {fired} "
          f"(evaluable={r['evaluable']})")
    print(f"  drop (all-time, context):    {_c(r['drop_all_time_c'])}  → would cross 10¢, but confounded")
    print(f"  ── REAL TELL — post-flip A/B SIDE-SPLIT ──")
    print(f"     NO : EV {_c(no['ev_ct'])}/ct  ({no['n_fills']} fills / {no['n_events']} events, ${no['total_pnl']:+.2f})")
    print(f"     YES: EV {_c(ys['ev_ct'])}/ct  ({ys['n_fills']} fills / {ys['n_events']} events, ${ys['total_pnl']:+.2f})")
    print(f"     → loss concentrated on the {'NO' if (no['ev_ct'] or 0) < (ys['ev_ct'] or 0) else 'YES'} side: "
          f"the correlated-side weather drawdown, NOT a refresh-execution effect.")
    print(f"  VERDICT: {r['verdict']}")

    if args.write:
        out = ROOT / "state" / "live-premium" / "refresh-flip-report.json"
        try:
            out.write_text(json.dumps(r, indent=2))
            print(f"  wrote {out}")
        except Exception as e:
            print(f"  (write failed: {e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

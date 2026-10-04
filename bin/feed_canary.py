#!/usr/bin/env python3
"""bin/feed_canary.py — detect a feed that "reads real today, 0 tomorrow".

The whole live book + kill-switch rest on Kalshi feeds we've proven are partly
degraded (the settlements feed already zeroes its count/cost fields). The danger is
a field that returns real data today and silently drops to 0/null tomorrow — mtime
health would still say OK. This canary snapshots the load-bearing scalar fields each
cycle to state/feed-canary.jsonl and ALERTS when a field goes non-zero → 0/null and
STAYS there for >= MIN_STREAK consecutive cycles (a single transient empty window is
not enough to fire).

Monitored fields are chosen to be legitimately non-zero in steady state. NOTE:
settlements.count/cost are ALWAYS 0 on this feed (known degradation) — we deliberately
do NOT monitor them; we monitor settlements.rows and settlements.nonzero_revenue_rows,
which are real.

ZERO vs NULL (QA 2026-07-03): for a few fields 0 is a LEGITIMATE state, not
degradation — the weather book demonstrably goes flat ("Synced 0 positions",
cycle-2026-07-02.log) and all resting quotes filling/expiring overnight is routine
with halt-cancel default-on. Those fields (ZERO_OK_FIELDS) alert only on null (the
CLI/API call itself failed); treating their 0 as degraded would false-page every
flat night now that run-cycle.sh runs this canary every cycle (17 paper + 6 live/day).

Strictly READ-ONLY. Exit: 0 no new degradation · 1 a field newly degraded (alerted).

Usage: python3 bin/feed_canary.py [--dir live-premium] [--min-streak 2] [--json] [--no-alert]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

CANARY_LOG = ROOT / "state" / "feed-canary.jsonl"

# Fields where 0 is a legitimate steady state (flat book / no resting quotes): only a
# null reading (the underlying CLI/API call failed) counts as degradation for these.
# Every OTHER monitored field must be non-zero in steady state (balance.available,
# fair_values.records, settlements.rows, ...) so 0 AND null both count. (QA 2026-07-03)
ZERO_OK_FIELDS = frozenset({
    "positions.market_count",
    "positions.weather_count",
    "orders.resting_count",
})


def _is_bad(name: str, val) -> bool:
    """Per-field degradation test — see ZERO_OK_FIELDS. Used for the current
    snapshot, the streak count, AND ever_good, so a legitimate 0 in history
    breaks a null-streak instead of extending it."""
    if name in ZERO_OK_FIELDS:
        return val is None
    return val in (0, None)


def _cli_json(*args) -> dict | None:
    try:
        out = subprocess.run(["kalshi-cli", "--prod", *args, "--json"],
                             capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def snapshot(state_dir: str) -> dict:
    """Gather the monitored scalar fields. A source that errors yields None (is_null)."""
    fields: dict[str, object] = {}

    bal = _cli_json("portfolio", "balance")
    fields["balance.available"] = None if bal is None else int(bal.get("balance", 0))
    fields["balance.portfolio"] = None if bal is None else int(bal.get("portfolio_value", 0))

    pos = _cli_json("portfolio", "positions")
    if pos is None:
        fields["positions.market_count"] = None
        fields["positions.weather_count"] = None
    else:
        mp = pos.get("market_positions", [])
        def _f(x):
            try: return float(x)
            except (TypeError, ValueError): return 0.0
        live = [p for p in mp if abs(_f(p.get("position_fp") or p.get("position") or 0)) >= 0.01]
        fields["positions.market_count"] = len(live)
        fields["positions.weather_count"] = sum(
            1 for p in live if p.get("ticker", "").startswith(("KXHIGH", "KXLOW")))

    st = _cli_json("portfolio", "settlements", "--limit", "200")
    if st is None:
        fields["settlements.rows"] = None
        fields["settlements.nonzero_revenue_rows"] = None
    else:
        rows = st.get("settlements", [])
        fields["settlements.rows"] = len(rows)
        fields["settlements.nonzero_revenue_rows"] = sum(1 for r in rows if (r.get("revenue") or 0) != 0)

    try:
        from trader import orders
        resting, _ = orders.list_resting_orders(prod=True)
        fields["orders.resting_count"] = len(resting) if resting is not None else None
    except Exception:  # noqa: BLE001
        fields["orders.resting_count"] = None

    try:
        fv = json.loads((ROOT / "fair-values.json").read_text())
        recs = fv.get("records", [])
        fields["fair_values.records"] = len(recs)
        fields["fair_values.series_present"] = len({r.get("ticker", "").split("-")[0] for r in recs})
    except Exception:  # noqa: BLE001
        fields["fair_values.records"] = None
        fields["fair_values.series_present"] = None

    return fields


def _load_history(limit: int = 12) -> list[dict]:
    if not CANARY_LOG.exists():
        return []
    lines = CANARY_LOG.read_text().splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def evaluate(state_dir: str = "live-premium", min_streak: int = 2, alert: bool = True) -> dict:
    hist = _load_history()
    snap = snapshot(state_dir)
    now = datetime.now(timezone.utc).isoformat()

    degraded = []  # fields that are bad NOW (per-field test), streak>=min_streak, and were good earlier
    for name, val in snap.items():
        if not _is_bad(name, val):
            continue
        # consecutive bad streak ending now (this snapshot counts as 1)
        streak = 1
        for h in reversed(hist):
            hv = h.get("fields", {}).get(name, {}).get("value")
            if _is_bad(name, hv):
                streak += 1
            else:
                break
        ever_good = any(not _is_bad(name, h.get("fields", {}).get(name, {}).get("value"))
                        for h in hist)
        if streak >= min_streak and ever_good:
            degraded.append({"field": name, "value": val, "streak": streak})

    # append this snapshot
    rec = {"ts": now, "fields": {k: {"value": v, "is_zero": v == 0, "is_null": v is None}
                                 for k, v in snap.items()}}
    try:
        CANARY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CANARY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # alert per newly-degraded field
    if alert and degraded:
        try:
            from trader.notify import alert as _send
            for d in degraded:
                _send(f"FEED CANARY: {d['field']} went 0/null for {d['streak']} cycles "
                      f"(was non-zero before) — a feed the live book rests on may be silently degrading.",
                      key=f"feed_canary_{d['field']}", dedup_seconds=3600)
        except Exception:  # noqa: BLE001
            pass

    return {"severity": 1 if degraded else 0, "snapshot": snap,
            "degraded": degraded, "history_len": len(hist)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Field-level feed degradation canary.")
    ap.add_argument("--dir", default="live-premium")
    ap.add_argument("--min-streak", type=int, default=2)
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = evaluate(args.dir, min_streak=args.min_streak, alert=not args.no_alert)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"FEED CANARY: {'DEGRADED' if res['severity'] else 'OK'}  (history={res['history_len']})")
        for k, v in res["snapshot"].items():
            flag = " ←0/null" if _is_bad(k, v) else ""   # per-field: a legit 0 isn't flagged
            print(f"  {k}: {v}{flag}")
        for d in res["degraded"]:
            print(f"  ⚠️ {d['field']} degraded {d['streak']} cycles")
    return res["severity"]


if __name__ == "__main__":
    sys.exit(main())

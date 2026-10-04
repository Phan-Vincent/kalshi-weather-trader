#!/usr/bin/env python3
"""bin/generate_ab_data.py — per-arm paper performance for the dashboard A/B tab.

Loops the non-live arms in variants.json and writes dashboard/ab-data.json with, per arm:
headline P&L / return / win-rate, the bootstrap per-contract CI + Brier/BSS (reused from
bin/compare_variants), the maker fill funnel (from maker-lifecycle.jsonl), and a cumulative
P&L equity curve. The dashboard's "A/B Tests" tab fetches this file. Read-only.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))
from compare_variants import arm_metrics, _read_jsonl, _read_settlements  # reuse the tested stats


def fill_funnel(state_dir: Path) -> dict:
    c = Counter(r.get("event") for r in _read_jsonl(state_dir / "maker-lifecycle.jsonl"))
    filled, expired = c.get("filled", 0), c.get("expired", 0)
    resolved = filled + expired
    return {
        "posted": c.get("posted", 0), "replaced": c.get("replaced", 0),
        "filled": filled, "expired": expired, "cancelled_dup": c.get("cancelled_dup", 0),
        "fill_rate": round(filled / resolved * 100, 1) if resolved else None,
    }


def equity_curve(state_dir: Path, cap: int = 400) -> list:
    rows = _read_settlements(state_dir)   # deduped — double-appended rows would inflate the curve
    rows.sort(key=lambda r: r.get("settled_at_utc", ""))
    cum, pts = 0.0, []
    for r in rows:
        cum += r.get("pnl_cents", 0) / 100.0
        pts.append(round(cum, 2))
    if len(pts) > cap:  # downsample to keep the JSON small
        step = len(pts) / cap
        pts = [pts[int(i * step)] for i in range(cap)]
    return pts


def book_stats(state_dir: Path) -> dict:
    p = state_dir / "paper-book.json"
    if not p.is_file():
        return {"cash": None, "start_bank": None, "open_positions": 0}
    b = json.loads(p.read_text())
    return {
        "cash": round(b.get("cash_cents", 0) / 100, 2),
        "start_bank": round(b.get("starting_bank_cents", 0) / 100, 2),
        "open_positions": len(b.get("open", [])),
    }


def main() -> int:
    cfg = json.loads((ROOT / "variants.json").read_text())
    arms = []
    for v in cfg.get("variants", []):
        if v.get("live"):
            continue  # paper arms only
        sd = ROOT / "state" / v["dir"]
        if not sd.exists():
            continue
        m = arm_metrics(sd)
        pc = m.get("pnl_per_contract_ci") or (None, None, None)
        env = v.get("env", {}) or {}
        bk = book_stats(sd)
        # Realized return = settled P&L ÷ start bank. (Cash-based return is misleading for
        # arms holding open positions — deployed capital isn't a loss.)
        ret = round(m["total_pnl"] / bk["start_bank"] * 100, 2) if bk.get("start_bank") else None
        arms.append({
            "name": v["name"], "dir": v["dir"], "enabled": bool(v.get("enabled")),
            "premium": env.get("KALSHI_WEATHER_PREMIUM_MODE") == "1",
            "mode": v.get("mode", "mm"), "experiment": v.get("_experiment", ""),
            "n": m["n"], "win_rate": round(m["win_rate"], 1), "total_pnl": round(m["total_pnl"], 2),
            "return_pct": ret, **bk,
            "brier": round(m["brier"], 4) if m["brier"] is not None else None,
            "bss": round(m["bss"] * 100, 1) if m["bss"] is not None else None,
            "brier_n": m["brier_n"],
            "pc_pnl": round(pc[0], 2) if pc[0] is not None else None,
            "pc_lo": round(pc[1], 2) if pc[1] is not None else None,
            "pc_hi": round(pc[2], 2) if pc[2] is not None else None,
            "pc_sig": bool(pc[1] is not None and (pc[1] > 0 or pc[2] < 0)),
            "fill": fill_funnel(sd),
            "equity": equity_curve(sd),
        })
    out = {"arms": arms, "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    dash = ROOT / "dashboard"
    dash.mkdir(exist_ok=True)
    (dash / "ab-data.json").write_text(json.dumps(out, indent=2))
    print(f"ab-data.json: {len(arms)} arms ({', '.join(a['name'] for a in arms)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

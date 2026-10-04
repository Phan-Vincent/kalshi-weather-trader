#!/usr/bin/env python3
"""bin/ab_health.py — A/B standings + live-arm activity flag for the nightly review.

Reads dashboard/ab-data.json (per-arm paper metrics, regenerated each cycle by
generate_ab_data.py) for the standings, and checks state/live-premium/maker-lifecycle.jsonl
for posted_live activity in the last 24h — so an idle live arm can't go unnoticed.
Prints a compact text block; importable via render(). Read-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _live_posted_24h() -> tuple[int, bool]:
    """(orders posted in last 24h, ever_posted). Reads the live-premium lifecycle log."""
    p = ROOT / "state" / "live-premium" / "maker-lifecycle.jsonl"
    if not p.is_file():
        return 0, False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    n24, ever = 0, False
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") != "posted_live":
            continue
        ever = True
        try:
            if datetime.fromisoformat((r.get("ts") or "").replace("Z", "+00:00")) >= cutoff:
                n24 += 1
        except ValueError:
            pass
    return n24, ever


def render() -> str:
    lines = ["", "=" * 52, "A/B ARMS — PAPER STANDINGS", "=" * 52]
    abp = ROOT / "dashboard" / "ab-data.json"
    if abp.is_file():
        d = json.loads(abp.read_text())
        arms = sorted(d.get("arms", []), key=lambda a: (-(a.get("n") or 0), -(a.get("total_pnl") or 0)))
        for a in arms:
            n = a.get("n", 0)
            fr = (a.get("fill") or {}).get("fill_rate")
            frs = f"{fr:.0f}%" if fr is not None else "—"
            if n > 0:
                ret = a.get("return_pct")
                rets = f"{ret:+.1f}%" if ret is not None else "—"
                sig = " ✓edge" if a.get("pc_sig") else ""
                lines.append(f"  {a['name']:20s} {n:>3}t  ${a.get('total_pnl',0):>+8.2f}  {rets:>7}  "
                             f"WR {a.get('win_rate',0):.0f}%  fill {frs}{sig}")
            else:
                lines.append(f"  {a['name']:20s}   0t  pending: {a.get('open_positions',0)} open, fill {frs}")
        lines.append(f"  updated {d.get('updated_at','?')}")
    else:
        lines.append("  (ab-data.json not found — run bin/generate_ab_data.py)")

    n24, ever = _live_posted_24h()
    lines.append("")
    if n24 == 0:
        lines.append("  ⚠️ live-premium placed 0 orders in last 24h"
                     + ("" if ever else " (no live orders ever — arm not trading)"))
    else:
        lines.append(f"  live-premium: {n24} order(s) posted in last 24h")
    lines.append("=" * 52)
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""bin/rain_screen_score.py — read-out for the monthly-rain KILL SCREEN (see rain_screen_snapshot.py).

Reads the frozen snapshots, settles each completed city-month on the ACIS GAUGE total (the same source
Kalshi resolves on: "total precipitation at CLIxxx > N inches"), and scores three probabilities per bin
— MARKET mid, model FULL, model BANKED-ONLY — on the LEAK-FREE month-open snapshots only. Reports:

  • Brier(market) vs Brier(FULL), event-clustered (city+month) BCa CI on the per-bin difference.
  • FIREWALL: Brier(BANKED-ONLY) − Brier(FULL) — is any edge the remaining-precip FORECAST or just the
    banked observation? If FULL ≈ BANKED-ONLY, there is no forecast contribution.

Pre-registered decision (ROADMAP): SUCCESS only if FULL beats MARKET leak-free (CI excludes 0) AND the
firewall is positive AND it survives ≥2 seasons (~150 effective events). KILL if the month-open edge CI
includes 0, or the edge lives only in mid-month (leak-exposed) snapshots, or FULL ≈ BANKED-ONLY. Slow by
design: a CI-excludes-0 CONFIRM needs ~2-3 yr; a null/leak KILL reads out in ~12 months. Read-only.

Usage:
    python3 bin/rain_screen_score.py            # score all completed city-months
    python3 bin/rain_screen_score.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import cluster_bootstrap_ci  # noqa: E402
import rain_screen_snapshot as rss  # noqa: E402  (ACIS station map + helpers)

LOG = rss.LOG
ACIS = rss.ACIS


def acis_month_total(sid: str, month_iso: str) -> float | None:
    """Realized GAUGE monthly precip total (inches) for the settlement station — the resolution value."""
    y, m = (int(x) for x in month_iso.split("-"))
    body = json.dumps({"sid": sid, "sdate": f"{y}-{m:02d}-01", "edate": f"{y}-{m:02d}-31",
                       "elems": [{"name": "pcpn", "interval": "mly", "reduce": "sum"}]}).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(ACIS, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "kwb-rainscreen/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0); continue
    else:
        return None
    for _, v in d.get("data", []):
        if v not in ("M", None) and rss._isnum(v):
            return float(v)
    return None


def load_snapshots(path: Path = LOG) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def leakfree_monthopen(recs: list[dict]) -> list[dict]:
    """One record per (city, month, ticker): the EARLIEST leak-free (month-open) snapshot — the metric."""
    best = {}
    for r in recs:
        if not r.get("leakfree_monthopen"):
            continue
        k = (r["city"], r["month"], r["ticker"])
        if k not in best or r["ts_utc"] < best[k]["ts_utc"]:
            best[k] = r
    return list(best.values())


def _brier_items(scored: list[dict], prob_key: str):
    """[(event, brier)] for records with a non-null prob and a known outcome. event = city+month cluster."""
    out = []
    for r in scored:
        p, y = r.get(prob_key), r.get("_outcome")
        if p is not None and y is not None:
            out.append((f"{r['city']}-{r['month']}", (p - y) ** 2))
    return out


def _mean(items):
    return sum(v for _, v in items) / len(items) if items else None


def score(recs: list[dict], settle=acis_month_total) -> dict:
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    lf = [r for r in leakfree_monthopen(recs) if r["month"] < this_month]  # only completed months
    # settle each city-month once
    totals = {}
    for r in lf:
        k = (r["city"], r["month"])
        if k not in totals:
            totals[k] = settle(r.get("acis_station", ""), r["month"])
    scored = []
    for r in lf:
        tot = totals.get((r["city"], r["month"]))
        if tot is None:
            continue
        r = {**r, "_outcome": 1.0 if tot > r["threshold_in"] else 0.0}
        scored.append(r)

    mk, fu, bo = _brier_items(scored, "market_prob"), _brier_items(scored, "full_prob"), _brier_items(scored, "banked_only_prob")
    # per-bin paired diffs (same records) for the CIs
    paired = [(f"{r['city']}-{r['month']}", r) for r in scored
              if r.get("market_prob") is not None and r.get("full_prob") is not None]
    edge_items = [(ev, (r["market_prob"] - r["_outcome"]) ** 2 - (r["full_prob"] - r["_outcome"]) ** 2)
                  for ev, r in paired]                                  # market_b − full_b  (>0 ⇒ model better)
    fw_items = [(ev, (r["banked_only_prob"] - r["_outcome"]) ** 2 - (r["full_prob"] - r["_outcome"]) ** 2)
                for ev, r in paired if r.get("banked_only_prob") is not None]  # banked_b − full_b (>0 ⇒ forecast adds)
    n_events = len({ev for ev, _ in paired})
    return {
        "n_bins": len(scored), "n_events": n_events, "completed_months": sorted({r["month"] for r in scored}),
        "brier_market": _mean(mk), "brier_full": _mean(fu), "brier_banked_only": _mean(bo),
        "edge_ci": cluster_bootstrap_ci(edge_items) if edge_items else (None, None, None),
        "firewall_ci": cluster_bootstrap_ci(fw_items) if fw_items else (None, None, None),
        "verdict": _verdict(edge_items, fw_items, n_events),
    }


def _verdict(edge_items, fw_items, n_events, min_events=30) -> str:
    if not edge_items:
        return ("ACCRUING — no completed leak-free month yet. First month-open snapshots are the metric; "
                "the earliest scorable month reads out after its month closes.")
    if n_events < min_events:
        return (f"ACCRUING — only {n_events} leak-free city-months settled (need ≥{min_events} to rule; "
                f"~150 for a powered CONFIRM, ~2-3 yr). A null/leak KILL can read out sooner.")
    ec = cluster_bootstrap_ci(edge_items)
    fc = cluster_bootstrap_ci(fw_items) if fw_items else (None, None, None)
    edge_pos = ec[1] is not None and ec[1] > 0
    fw_pos = fc[1] is not None and fc[1] > 0
    if edge_pos and fw_pos:
        return ("SIGNAL — FULL beats market leak-free (CI excludes 0) AND the firewall is positive "
                "(forecast adds over climatology). Escalate to a tiny paper pilot; keep accruing.")
    if edge_pos and not fw_pos:
        return ("LEAK/OBSERVATION — FULL beats market but FULL ≈ BANKED-ONLY, so the edge is banked "
                "observation, not forecast. Not a tradeable forecast edge.")
    return ("KILL-LEANING — no leak-free forecast edge (market ≥ FULL, CI spans/negative). Consistent "
            "with the prior that the venue is picked clean; keep accruing to firm the null.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read out the monthly-rain kill screen")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    recs = load_snapshots()
    if not recs:
        print(f"[rain-score] no snapshots yet at {LOG.relative_to(ROOT)} — run bin/rain_screen_snapshot.py daily")
        return 0
    r = score(recs)
    if args.json:
        print(json.dumps(r, default=str)); return 0

    def ci(c): return "n/a" if not c or c[0] is None else f"{c[0]:+.4f} [{c[1]:+.4f}, {c[2]:+.4f}]"
    print(f"[rain-score] leak-free (month-open) read — {r['n_bins']} bins over {r['n_events']} city-months "
          f"{r['completed_months'] or '(none complete yet)'}")
    if r["brier_market"] is not None:
        print(f"  Brier: market={r['brier_market']:.4f}  FULL={r['brier_full']:.4f}  "
              f"BANKED-ONLY={r['brier_banked_only']:.4f}")
        print(f"  edge (market_b − FULL_b, >0 ⇒ model beats market): {ci(r['edge_ci'])}")
        print(f"  firewall (BANKED_b − FULL_b, >0 ⇒ forecast adds):  {ci(r['firewall_ci'])}")
    print(f"\n  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

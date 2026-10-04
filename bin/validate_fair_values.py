#!/usr/bin/env python3
"""bin/validate_fair_values.py — CONTENT validation for a fair-values file.

healthcheck.py's `fair_values_fresh` only checks the file's MTIME — a fresh
timestamp on a truncated, degenerate, or fabricated file passes today. This tool
validates the SUBSTANCE:

  Structural   parses; markets_processed == len(records) == total_markets_fetched;
               every queried series has >=1 record (coverage — a live market not in
               the file is never quoted); generated_at within the freshness window.
  Distribution fair_prob in [floor, 1]; not degenerate (stdev, not-all-0.5,
               not-all-floor); every record carries >=1 ensemble member.
  Fabrication  the invented-forecast fallback (model/fair_value.py: a single synthetic
               member at threshold±2°F when ALL real sources fail) never reaches a
               traded file — flagged CRITICAL if found.

Note on scope: the LIVE arm (premium-live) runs PREMIUM_MODE and prices 100% off
the order book — it does NOT price on fair_prob (trader/scanner.py). So the DISTRIBUTION
checks chiefly protect the paper forecast arms; the STRUCTURAL/coverage checks protect
the live arm (only markets present in the file are eligible to be quoted). The
climatology-only / same-day-leak question is intentionally NOT decided from stored
fields (they are ambiguous — likelihood_prob is moved by persistence too); that is
covered by tests/test_sameday_leak_guard.py + bin/validate_forecast_gate.py.

Exit: 0 OK · 1 DEGRADED · 2 CRITICAL. Importable evaluate() for a healthcheck rollup.

Usage:
  python3 bin/validate_fair_values.py [fair-values.json] [--floor 0.12] [--fresh-min 180] [--json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def evaluate(path: Path, floor: float = 0.12, fresh_min: float = 180.0) -> dict:
    checks: list[tuple[str, int, str]] = []  # (name, severity, detail)

    # ── parse ──
    try:
        d = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"severity": 2, "checks": [("parse", 2, f"{path} missing")], "file": str(path)}
    except json.JSONDecodeError as e:
        return {"severity": 2, "checks": [("parse", 2, f"corrupt JSON: {e}")], "file": str(path)}

    recs = d.get("records", d if isinstance(d, list) else [])
    meta = d if isinstance(d, dict) else {}
    n = len(recs)
    checks.append(("parse", 0 if n else 2, f"{n} records"))
    if not n:
        return {"severity": 2, "checks": checks, "file": str(path)}

    # ── structural / counts ──
    mp = meta.get("markets_processed")
    tf = meta.get("total_markets_fetched")
    if mp is not None and tf is not None:
        if mp == n == tf:
            checks.append(("counts", 0, f"markets_processed==records==fetched=={n}"))
        else:
            checks.append(("counts", 2, f"MISMATCH markets_processed={mp} records={n} fetched={tf} "
                                        "(markets silently dropped mid-build)"))

    # ── coverage: every queried series has a record ──
    series = set(meta.get("series_queried", []))
    if series:
        seen = {r.get("ticker", "").split("-")[0] for r in recs}
        missing = sorted(series - seen)
        if missing:
            checks.append(("coverage", 2, f"{len(missing)} queried series with 0 records: {missing[:8]}"))
        else:
            checks.append(("coverage", 0, f"all {len(series)} queried series present"))

    # ── freshness (content-side mirror of healthcheck) ──
    gen = meta.get("generated_at_utc")
    if gen:
        try:
            age_m = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds() / 60.0
            checks.append(("freshness", 1 if age_m > fresh_min else 0,
                           f"generated {age_m:.0f}m ago" + (f" (> {fresh_min:.0f}m)" if age_m > fresh_min else "")))
        except ValueError:
            checks.append(("freshness", 1, f"unparseable generated_at_utc={gen!r}"))

    # ── probability range + non-degenerate distribution ──
    fps = [r.get("fair_prob") for r in recs if isinstance(r.get("fair_prob"), (int, float))]
    oob = [r.get("ticker") for r in recs
           if isinstance(r.get("fair_prob"), (int, float)) and not (floor - 1e-9 <= r["fair_prob"] <= 1.0 + 1e-9)]
    if oob:
        checks.append(("prob_range", 2, f"{len(oob)} fair_prob outside [{floor},1]: {oob[:5]}"))
    else:
        checks.append(("prob_range", 0, f"all fair_prob in [{floor},1]"))
    if fps:
        sd = statistics.pstdev(fps)
        frac_half = sum(1 for x in fps if abs(x - 0.5) < 1e-9) / len(fps)
        frac_floor = sum(1 for x in fps if abs(x - floor) < 1e-9) / len(fps)
        degen = sd < 0.05 or frac_half >= 0.20 or frac_floor >= 0.90
        checks.append(("distribution", 2 if degen else 0,
                       f"stdev={sd:.3f} frac@0.5={frac_half:.0%} frac@floor={frac_floor:.0%}"
                       + (" DEGENERATE (fresh-but-dead)" if degen else "")))

    # ── ensemble presence ──
    empty_ens = [r.get("ticker") for r in recs if not (r.get("ensemble_forecasts") or [])]
    if empty_ens:
        checks.append(("ensemble", 1, f"{len(empty_ens)}/{n} records with 0 ensemble members: {empty_ens[:5]}"))
    else:
        checks.append(("ensemble", 0, f"all {n} records carry >=1 ensemble member"))

    # ── invented-forecast fabrication detector ──
    invented = []
    for r in recs:
        ens = r.get("ensemble_forecasts") or []
        thr = r.get("_threshold_f")
        if len(ens) == 1 and isinstance(thr, (int, float)) and abs(abs(ens[0] - thr) - 2.0) < 0.01:
            invented.append(r.get("ticker"))
    if invented:
        checks.append(("invented_forecast", 2,
                       f"{len(invented)} record(s) trading on a FABRICATED forecast (all real sources failed): {invented[:8]}"))
    else:
        checks.append(("invented_forecast", 0, "no fabricated forecasts"))

    severity = max((s for _, s, _ in checks), default=0)
    return {"severity": severity, "checks": checks, "file": str(path),
            "records": n, "n_series": len(series)}


def _render(res: dict) -> str:
    lab = {0: "OK", 1: "DEGRADED", 2: "CRITICAL"}
    mark = {0: "✓", 1: "▲", 2: "✗"}
    lines = [f"VALIDATE {res['file']}: {lab[res['severity']]}"]
    for name, s, detail in res["checks"]:
        lines.append(f"  {mark[s]} {name}: {detail}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Content-validate a fair-values file.")
    ap.add_argument("file", nargs="?", default=str(ROOT / "fair-values.json"))
    ap.add_argument("--floor", type=float, default=0.12)
    ap.add_argument("--fresh-min", type=float, default=180.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = evaluate(Path(args.file), floor=args.floor, fresh_min=args.fresh_min)
    print(json.dumps(res, indent=2) if args.json else _render(res))
    return res["severity"]


if __name__ == "__main__":
    sys.exit(main())

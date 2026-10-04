#!/usr/bin/env python3
"""bin/triangulate_balance.py — cross-check the balance & positions feeds the live
book and kill-switch rest on, across THREE independent paths, and flag divergence.

Motivation: the live book, $-stop and Kelly sizing are all fed by ONE transport
(`kalshi-cli`). If that feed lies (returns 0/null, or drifts), nothing catches it —
we only find out by digging. This tool supplies the missing redundancy:

  Leg A  kalshi-cli --prod portfolio {balance,positions}   (the wired transport)
  Leg B  signed-HTTP GET on the reads host, reusing trader.orders auth
         (DIFFERENT transport + auth path — agreement is meaningful)
  Leg C  the local book state/<dir>/paper-book.json (cash_cents, open[])

It is strictly READ-ONLY: only GETs on the reads host (api.elections.kalshi.com),
never the orders host, never a write. The Kalshi *web account* (Leg D) remains the
only fully-independent ground truth and must be eyeballed by a human.

Exit: 0 all legs agree · 1 divergence between legs · 2 a leg read 0/null/error.

2026-07-12 (snapshot fallback for gateway/sandbox readers): cowork sessions can't run Legs A/B
(no kalshi-cli binary, no signed HTTP from the sandbox), so operator status checks from there
always reported ZERO/ERROR — a permanent false alarm. The Mac-side launchd sync job now writes a
full triangulation snapshot each :10/:40 slot (`--write-snapshot` → state/<dir>/triangulation-
snapshot.json, atomic). When kalshi-cli is ABSENT, this tool auto-falls back to a FRESH snapshot
(≤ --snapshot-max-age-min, default 75 = two slots + slack), prints it clearly labeled with its
age, and exits with the SNAPSHOT's severity. A missing/stale snapshot still exits 2 — the
fallback can only relay a real Mac-side read, never fabricate one. `--write-snapshot` refuses to
run without kalshi-cli (a snapshot must never be written FROM a snapshot), and
`--no-snapshot-fallback` forces the live attempt for debugging.

Usage:
  python3 bin/triangulate_balance.py [--dir live-premium] [--json]
                                     [--write-snapshot] [--no-snapshot-fallback]
                                     [--snapshot-max-age-min 75]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

READS_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _is_weather(tk: str) -> bool:
    # QA-10 (2026-07-01): shared anchored matcher (was a startswith that still matched
    # KXHIGHESTGROSSINGMOVIE etc.), so all legs scope "weather" identically.
    from data.weather_data import is_weather_ticker
    return is_weather_ticker(tk)


# ── Leg A: kalshi-cli ──────────────────────────────────────────────────
def leg_a_balance() -> dict:
    try:
        out = subprocess.run(
            ["kalshi-cli", "--prod", "portfolio", "balance", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return {"_error": out.stderr.strip()[:200]}
        d = json.loads(out.stdout)
        return {"available_cents": int(d.get("balance", 0)),
                "portfolio_cents": int(d.get("portfolio_value", 0))}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


def leg_a_positions() -> dict:
    try:
        out = subprocess.run(
            ["kalshi-cli", "--prod", "portfolio", "positions", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return {"_error": out.stderr.strip()[:200]}
        return _summarize_positions(json.loads(out.stdout))
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


# ── Leg B: signed-HTTP on the reads host (independent transport) ─────────
def _signed_get(path: str) -> dict:
    """GET READS_BASE+path with a fresh RSA-PSS signature. Read-only.

    Kalshi signs the path WITHOUT the query string, so `path` may include a
    ?query for the URL but only the part before '?' is signed."""
    try:
        from trader import orders
        url = READS_BASE + path
        sign_path = "/trade-api/v2" + path.split("?", 1)[0]
        headers = orders.kalshi_auth_headers("GET", sign_path, prod=True)
        return orders._http_request("GET", url, headers=headers)
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


def leg_b_balance() -> dict:
    d = _signed_get("/portfolio/balance")
    if d.get("_error"):
        return {"_error": d.get("body") or d.get("exception") or "signed-http error"}
    return {"available_cents": int(d.get("balance", 0)),
            "portfolio_cents": int(d.get("portfolio_value", 0))}


def leg_b_positions() -> dict:
    d = _signed_get("/portfolio/positions?limit=1000")
    if d.get("_error"):
        return {"_error": d.get("body") or d.get("exception") or "signed-http error"}
    return _summarize_positions(d)


def _summarize_positions(d: dict) -> dict:
    mp = d.get("market_positions", []) if isinstance(d, dict) else []
    live = {}
    nonwx = {}
    for p in mp:
        tk = p.get("ticker", "")
        fp = _f(p.get("position_fp") or p.get("position") or 0)
        if abs(fp) < 0.01:
            continue
        (live if _is_weather(tk) else nonwx)[tk] = round(fp, 2)
    return {"weather": live, "non_weather": nonwx,
            "weather_count": len(live), "non_weather_count": len(nonwx)}


# ── Leg C: local book ──────────────────────────────────────────────────
def leg_c(state_dir: str) -> dict:
    try:
        bk = json.loads((ROOT / "state" / state_dir / "paper-book.json").read_text())
        open_tk = {o["ticker"]: abs(o.get("qty", 0) or 0) for o in bk.get("open", [])}
        return {"available_cents": int(bk.get("cash_cents", 0)),
                "realized_cents": int(bk.get("realized_pnl_cents", 0)),
                "open": open_tk, "open_count": len(open_tk)}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


# ── Compare ─────────────────────────────────────────────────────────────
def run(state_dir: str = "live-premium") -> dict:
    a_bal, b_bal = leg_a_balance(), leg_b_balance()
    a_pos, b_pos = leg_a_positions(), leg_b_positions()
    c = leg_c(state_dir)

    findings, sev = [], 0

    def _zero_or_err(leg, name, field="available_cents"):
        nonlocal sev
        if leg.get("_error"):
            findings.append(f"{name}: ERROR {leg['_error']}")
            sev = max(sev, 2)
            return True
        if field and (leg.get(field) in (0, None)):
            findings.append(f"{name}: {field}=0/null while another leg is non-zero")
            sev = max(sev, 2)
            return True
        return False

    ea = _zero_or_err(a_bal, "Leg A balance")
    eb = _zero_or_err(b_bal, "Leg B balance")
    ec = _zero_or_err(c, "Leg C book")
    # Position legs must fail CLOSED too (2026-07-13): a failed positions read (timeout, non-zero
    # exit code, malformed JSON) used to bypass _zero_or_err entirely, so run() skipped both
    # position checks, left sev=0, and STILL emitted the affirmative "weather positions ticker/qty
    # exact" all-clear — a phantom/missing exchange position went undetected. field=None → check
    # ONLY the _error sentinel (position legs carry a "weather" dict, not available_cents).
    ea_pos = _zero_or_err(a_pos, "Leg A positions", field=None)
    eb_pos = _zero_or_err(b_pos, "Leg B positions", field=None)

    # Balance agreement
    if not ea and not eb and a_bal["available_cents"] != b_bal["available_cents"]:
        findings.append(f"balance A={a_bal['available_cents']} != B={b_bal['available_cents']} "
                        "(transport divergence)")
        sev = max(sev, 1)
    if not ea and not ec:
        # QA-11 (2026-07-01): book cash == live available ONLY right after a sync; between syncs it
        # legitimately drifts by every fill/settlement. The old `drift > 0` fired DIVERGENCE (sev 1)
        # on every inter-sync run and drowned the real A-vs-B transport-lie signal. Report drift
        # informationally; escalate to DIVERGENCE only beyond a tolerance that can't be normal.
        drift = abs(a_bal["available_cents"] - c["available_cents"])
        if drift > 2500:   # > $25 is larger than normal inter-sync fill/settlement movement
            findings.append(f"balance A={a_bal['available_cents']} vs book C={c['available_cents']} "
                            f"(drift {drift}c > $25 — larger than normal inter-sync movement)")
            sev = max(sev, 1)
        elif drift > 0:
            findings.append(f"balance A={a_bal['available_cents']} vs book C={c['available_cents']} "
                            f"(drift {drift}c — expected between syncs; informational)")

    # Positions agreement (weather subset A/B vs book C)
    if not ea_pos and not c.get("_error"):
        aw = set(a_pos["weather"]); cw = set(c["open"])
        if aw != cw:
            findings.append(f"positions: A-weather {sorted(aw-cw)} not in book; "
                            f"book-only {sorted(cw-aw)}")
            sev = max(sev, 1)
        else:
            for tk in aw:
                if abs(abs(a_pos['weather'][tk]) - c['open'][tk]) > 0.51:
                    findings.append(f"positions qty mismatch {tk}: A={a_pos['weather'][tk]} book={c['open'][tk]}")
                    sev = max(sev, 1)
    if not ea_pos and not eb_pos:
        if set(a_pos["weather"]) != set(b_pos["weather"]):
            findings.append("positions: Leg A weather set != Leg B weather set (transport divergence)")
            sev = max(sev, 1)

    if not findings:
        findings.append("all legs agree (balance to the cent; weather positions ticker/qty exact)")

    return {"severity": sev, "state_dir": state_dir,
            "leg_a_balance": a_bal, "leg_b_balance": b_bal, "leg_c": c,
            "leg_a_positions": a_pos, "leg_b_positions": b_pos,
            "findings": findings,
            "note": "Leg D (Kalshi web account) is the only fully-independent ground truth — verify by hand."}


# ── Snapshot plumbing (2026-07-12) ──────────────────────────────────────
SNAPSHOT_NAME = "triangulation-snapshot.json"


def snapshot_path(state_dir: str) -> Path:
    return ROOT / "state" / state_dir / SNAPSHOT_NAME


def write_snapshot(res: dict, state_dir: str) -> Path:
    """Atomic (tmp + os.replace) so a concurrent reader never sees a torn file."""
    out = snapshot_path(state_dir)
    rec = dict(res)
    rec["ts_utc"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, out)
    return out


def read_snapshot(state_dir: str) -> dict | None:
    try:
        return json.loads(snapshot_path(state_dir).read_text())
    except Exception:  # noqa: BLE001  (missing/torn/garbage all mean "no snapshot")
        return None


def snapshot_age_min(snap: dict) -> float | None:
    try:
        ts = _dt.datetime.fromisoformat(str(snap["ts_utc"]).replace("Z", "+00:00"))
        return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None


def _have_transport() -> bool:
    return shutil.which("kalshi-cli") is not None


def _print_result(res: dict, header_suffix: str = "") -> None:
    lab = {0: "OK", 1: "DIVERGENCE", 2: "ZERO/ERROR"}[res["severity"]]
    print(f"TRIANGULATION: {lab}  (dir={res['state_dir']}){header_suffix}")
    print(f"  Leg A balance: {res['leg_a_balance']}")
    print(f"  Leg B balance: {res['leg_b_balance']}")
    print(f"  Leg C book:    avail={res['leg_c'].get('available_cents')} "
          f"realized={res['leg_c'].get('realized_cents')} open={res['leg_c'].get('open_count')}")
    print(f"  Leg A pos: wx={res['leg_a_positions'].get('weather_count')} "
          f"non-wx={res['leg_a_positions'].get('non_weather_count')}  "
          f"Leg B pos: wx={res['leg_b_positions'].get('weather_count')}")
    for f in res["findings"]:
        print(f"    • {f}")
    print(f"  {res['note']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Triangulate the balance/positions feeds.")
    ap.add_argument("--dir", default="live-premium", help="state subdir for the book (Leg C)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-snapshot", action="store_true",
                    help="persist the result to state/<dir>/triangulation-snapshot.json (Mac sync job)")
    ap.add_argument("--no-snapshot-fallback", action="store_true",
                    help="force a live read even without kalshi-cli (debugging)")
    ap.add_argument("--snapshot-max-age-min", type=float, default=75.0,
                    help="max snapshot age accepted by the fallback (default 75 = two sync slots + slack)")
    args = ap.parse_args()

    if args.write_snapshot and not _have_transport():
        print("TRIANGULATION: ERROR — --write-snapshot requires kalshi-cli (a snapshot must be a real "
              "Mac-side read, never derived from another snapshot)", file=sys.stderr)
        return 2

    if not _have_transport() and not args.no_snapshot_fallback:
        snap = read_snapshot(args.dir)
        age = snapshot_age_min(snap) if snap else None
        # Freshness is BOTH-sided: a negative age means the snapshot's ts_utc is in the reader's
        # future (writer/reader clock skew or an NTP jump) — reject it, else a bad-clock snapshot
        # would relay a dead severity as "fresh" indefinitely, defeating the staleness contract
        # (QA 2026-07-12).
        if snap is not None and age is not None and 0 <= age <= args.snapshot_max_age_min:
            if args.json:
                print(json.dumps(snap, indent=2))
            else:
                _print_result(snap, header_suffix=f"  [SNAPSHOT from Mac sync, {age:.0f}m old]")
            return int(snap.get("severity", 2))
        if snap is None:
            # read_snapshot collapses absent + torn to None; split them so the operator isn't
            # sent to "wait for the next slot" when a file IS present but being written corrupt.
            why = "unreadable/torn" if snapshot_path(args.dir).exists() else "missing"
        elif age is None:
            why = "unreadable ts"
        elif age < 0:
            why = f"future-dated ({age:.0f}m — writer/reader clock skew; not trusted)"
        else:
            why = f"stale ({age:.0f}m > {args.snapshot_max_age_min:.0f}m)"
        print(f"TRIANGULATION: UNAVAILABLE  (dir={args.dir})")
        print(f"    • no live transport here (kalshi-cli not found) and snapshot {why}")
        print("    • the Mac launchd sync job writes a fresh snapshot each :10/:40 — wait for the next "
              "slot, or run this tool on the Mac")
        return 2

    res = run(args.dir)
    if args.write_snapshot:
        write_snapshot(res, args.dir)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_result(res)
    return res["severity"]


if __name__ == "__main__":
    sys.exit(main())

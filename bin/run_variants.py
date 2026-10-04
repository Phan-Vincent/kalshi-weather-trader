#!/usr/bin/env python3
"""bin/run_variants.py — Run each enabled A/B arm defined in variants.json.

Replaces the hand-copied per-stream env blocks in run-cycle.sh. Each arm is a
parallel paper book (its own state/<dir>/) trading the SAME live order books with
a different env/argv config. For each enabled arm we run the existing pipeline,
unchanged, with KALSHI_WEATHER_STATE_DIR pointed at the arm's state dir:

    settle_paper.py
    [postmortem.py]                 # only if arm.postmortem (writes global LESSONS.md)
    paper_trade.py  (trade)
    paper_trade.py --report
    [generate_dashboard_data.py]    # only if arm.dashboard (writes dashboard/data.json)

postmortem/dashboard default OFF for every arm except `main`, so the shared
LESSONS.md and dashboard/data.json keep reflecting the main book exactly as they
did before this refactor.

Usage:
    python3 bin/run_variants.py                 # run all enabled arms
    python3 bin/run_variants.py main taker      # run only these arms (by name)
    python3 bin/run_variants.py --config foo.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))   # for the `kalshi-weather` namespace (mirror paper_trade.py)
PY = sys.executable or "python3"

# Imported at module top (after sys.path setup) ON PURPOSE: if the project root ever falls
# off the path again, this fails LOUDLY at load instead of silently fail-safe-skipping the
# live arm every cycle (the 2026-06-24 bug — see plan). It is fail-safe internally.
from trader.halt import is_halted_safe  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


def _resolve_fv(fv: str) -> str:
    """Mirror run-cycle.sh: use the requested fair-values file if it built and is
    non-empty, else fall back to the legacy fair-values.json."""
    p = ROOT / fv
    if p.is_file() and p.stat().st_size > 0:
        return str(p)
    fallback = ROOT / "fair-values.json"
    _log(f"    (fv {fv} missing/empty → falling back to fair-values.json)")
    return str(fallback)


DRY_RUN = False


def _run(cmd: list[str], env: dict) -> int:
    """Run a subprocess, streaming output. Never raises (mirrors the bash `|| true`)."""
    _log(f"    $ {' '.join(str(c) for c in cmd)}")
    if DRY_RUN:
        shown = {k: env[k] for k in sorted(env) if k.startswith("KALSHI_")}
        _log(f"      env: {shown}")
        return 0
    try:
        return subprocess.run(cmd, cwd=str(ROOT), env=env, check=False).returncode
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"    (command failed to launch: {exc})")
        return 1


def run_arm(arm: dict) -> None:
    name = arm["name"]
    is_live = bool(arm.get("live"))
    state_dir = ROOT / "state" / arm["dir"]
    state_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({k: str(v) for k, v in (arm.get("env") or {}).items()})
    env["KALSHI_WEATHER_STATE_DIR"] = str(state_dir)
    # P0a CRN: paper arms of this cycle share ONE order-book snapshot (KALSHI_WEATHER_CYCLE_ID set in
    # main) so the paired A/B difference cancels cross-arm snapshot noise. The LIVE arm ALWAYS fetches
    # fresh — real orders must not price off a cached/stale book. Global KALSHI_WEATHER_SHARED_BOOK=0
    # disables the whole thing (rollback / A-vs-off measurement).
    env["KALSHI_WEATHER_SHARED_BOOK"] = "0" if is_live else os.environ.get("KALSHI_WEATHER_SHARED_BOOK", "1")

    fv = _resolve_fv(arm.get("fv", "fair-values.json"))
    mode = arm.get("mode", "mm")
    max_orders = str(arm.get("max_orders", 5))

    tag = " ⚠️ LIVE (real orders)" if is_live else ""
    _log(f"\n=== arm '{name}' → state/{arm['dir']} (mode={mode}, fv={Path(fv).name}){tag} ===")

    # 0. LIVE only: sync the real Kalshi book before doing anything (pull actual fills).
    #    The live arm's per-trade realized P&L is captured INSIDE sync_live_positions.py (Kalshi
    #    reports settled positions with realized_pnl_cents), NOT via settle_paper — settle_paper
    #    assumes the paper-fill schema and would crash on / double-count synced live positions.
    #    A FAILED sync means the daily loss-stop would run on stale (last-good) P&L — skip
    #    the live arm this cycle and alert rather than trade blind on a stale balance.
    if is_live:
        _log("  - sync live positions from Kalshi")
        sync_rc = _run([PY, "bin/sync_live_positions.py"], env)
        if sync_rc != 0:
            _log(f"  🛑 live sync FAILED (rc={sync_rc}) — skipping live arm this cycle (stale-P&L guard)")
            try:
                from trader.notify import alert
                alert(f"live sync FAILED (rc={sync_rc}) for arm '{name}' — skipped live trade (stale-P&L guard)",
                      key="live_sync_failed")
            except Exception as e:
                _log(f"    (alert dispatch failed: {e})")
            return

    # 1. Settle finalized positions (queues losses to this arm's loss-queue.jsonl). For the live
    #    arm this is a no-op: settle_paper skips synced_from_kalshi positions (sync captures live
    #    realized P&L), so it neither crashes on the synced schema nor double-counts the kill-switch.
    _log("  - settle")
    _run([PY, "bin/settle_paper.py"], env)

    # 2. Post-mortem: ONLY for arms that opt in (writes the shared LESSONS.md).
    if arm.get("postmortem"):
        _log("  - postmortem (writes LESSONS.md)")
        _run([PY, "bin/postmortem.py"], env)

    # 3. Trade (live arms place REAL orders via --live).
    _log("  - trade" + (" (LIVE)" if is_live else ""))
    trade_cmd = [PY, "bin/paper_trade.py",
                 "--fair-values", fv,
                 "--mode", mode,
                 "--max-orders", max_orders,
                 "--state-dir", str(state_dir)]
    if is_live:
        trade_cmd.append("--live")
    _run(trade_cmd, env)

    # 4. Book report.
    _log("  - report")
    _run([PY, "bin/paper_trade.py", "--report", "--state-dir", str(state_dir)], env)

    # 5. Dashboard data: ONLY for arms that opt in (writes the shared dashboard/data.json).
    if arm.get("dashboard"):
        _log("  - dashboard data")
        _run([PY, "bin/generate_dashboard_data.py"], env)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run A/B variant arms from variants.json")
    ap.add_argument("names", nargs="*", help="Only run these arm names (default: all enabled)")
    ap.add_argument("--config", default=str(ROOT / "variants.json"), help="Path to variants.json")
    ap.add_argument("--list", action="store_true", help="List arms and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print the per-arm commands + env without executing")
    # 2026-07-06 (live-first split): the LIVE arm only needs fair-values.json (climatology,
    # built in run-cycle.sh step 1), NOT the two GEFS/forecast builds (1b/1c, paper-only) nor
    # the 7 paper arms that used to run before it. run-cycle.sh now places the real order via
    # `--only-live` right after step 1, then runs the paper builds + `--exclude-live` for the
    # paper arms — so a slow/torn-down/stalled paper phase can no longer starve the live order
    # ("38/38 built, killed before trading" — the 6/24-6/25 SIGKILL mode). The two are mutually
    # exclusive; both compose with the existing LIVE=1 + halt gate below (a live arm still only
    # trades when LIVE=1 and not halted).
    live_sel = ap.add_mutually_exclusive_group()
    live_sel.add_argument("--only-live", action="store_true",
                          help="Run only the live arm(s) (still gated on LIVE=1 + not halted)")
    live_sel.add_argument("--exclude-live", action="store_true",
                          help="Run only the paper arm(s); never place real orders this call")
    args = ap.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run

    try:
        cfg = json.loads(Path(args.config).read_text())
    except (OSError, json.JSONDecodeError) as e:
        # A malformed variants.json must not crash the whole cron cycle with a bare
        # traceback (audit H3). Fail loudly with a clear message + non-zero exit.
        _log(f"❌ cannot load variants config {args.config}: {e}")
        sys.exit(1)
    variants = cfg.get("variants", [])

    if args.list:
        for v in variants:
            flags = []
            if v.get("live"):
                flags.append("LIVE")
            if v.get("postmortem"):
                flags.append("postmortem")
            if v.get("dashboard"):
                flags.append("dashboard")
            state = "on " if v.get("enabled") else "off"
            _log(f"  [{state}] {v['name']:20s} dir=state/{v['dir']:22s} mode={v.get('mode','mm'):5s} {' '.join(flags)}")
        return 0

    if args.names:
        wanted = set(args.names)
        arms = [v for v in variants if v["name"] in wanted]
        missing = wanted - {v["name"] for v in arms}
        if missing:
            _log(f"⚠️  unknown arm name(s): {', '.join(sorted(missing))}")
    else:
        arms = [v for v in variants if v.get("enabled")]

    # Live-first split (2026-07-06): partition arms by liveness BEFORE the LIVE/halt gate so
    # the gate still applies to whatever remains. --only-live keeps only real-order arms (the
    # gate then still requires LIVE=1 + not halted, so a paper cron :00 makes this a no-op);
    # --exclude-live drops them so a paper-phase call can never place a real order.
    if args.only_live:
        arms = [a for a in arms if a.get("live")]
    elif args.exclude_live:
        arms = [a for a in arms if not a.get("live")]

    # LIVE arms place REAL orders — run them only when env LIVE=1 AND not halted.
    live_on = os.environ.get("LIVE", "0") == "1"
    halt_reason = None
    if live_on:
        halted, halt_reason = is_halted_safe()  # module-level import; fail-safe internally
        if halted:
            live_on = False
    if not live_on:
        skipped = [a["name"] for a in arms if a.get("live")]
        if skipped and halt_reason:
            _log(f"🛑 LIVE HALTED ({halt_reason}) — skipping live arm(s) {', '.join(skipped)}. Resume: bin/halt_live.py --off")
            try:
                from trader.notify import alert
                alert(f"🛑 live arms skipped — LIVE_HALT active: {halt_reason}", key="live_halt_skip", dedup_seconds=3600)
            except Exception:
                pass
        elif skipped:
            _log(f"(skipping live arm(s) {', '.join(skipped)} — set LIVE=1 to enable real orders)")
        arms = [a for a in arms if not a.get("live")]
    elif any(a.get("live") for a in arms):
        _log("⚠️  LIVE=1 — live arm(s) will place REAL orders.")

    if not arms:
        _log("No arms to run.")
        return 0

    # P0a CRN: one cycle id for this run so every PAPER arm reads the SAME per-cycle book cache
    # (state/_shared/book-cache-<id>.json). Set once here; run_arm inherits it via os.environ.copy().
    os.environ["KALSHI_WEATHER_CYCLE_ID"] = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.getpid()}"

    _log(f"=== {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} run_variants: "
         f"{len(arms)} arm(s): {', '.join(a['name'] for a in arms)} ===")
    for arm in arms:
        run_arm(arm)
    _log(f"\n=== run_variants done ({len(arms)} arm(s)) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

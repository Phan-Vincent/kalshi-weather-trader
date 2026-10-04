#!/usr/bin/env python3
"""bin/check_cycle_cadence.py — alert when scheduled trading cycles silently never START.

WHY: the OpenClaw cron agent-turn sometimes reports status=ok WITHOUT ever invoking
run-cycle-detached.sh (5 silent no-starts on 2026-07-01, 1 on 2026-07-02 — all on the
deepseek fallback model). That failure mode is invisible to BOTH existing guards:
cron.failureAlert never fires (the turn said ok), and bin/healthcheck.py runs INSIDE
run-cycle.sh, so it never executes when the cycle never starts. The only scheduled
path independent of the OpenClaw gateway is launchd → bin/sync-live.sh, which calls
this watchdog OUTSIDE its .cycle.lock critical section (we only read logs).

WHAT: compare EXPECTED cycle starts vs ACTUAL over the last N hours (default 3).
  Expected (mirrors the OpenClaw cron schedules; America/Los_Angeles wall clock):
    paper  '0 6-22 * * *'            → hourly on the hour, 06:00-22:00
    LIVE   '20 9,11,13,15,17,19 * *' → :20 past those hours
  Actual: the '=== <UTC-ISO>Z cycle start ===' marker run-cycle.sh tee's into
  logs/cycle-YYYY-MM-DD.log (the FILE is named with the LOCAL date at cycle start;
  the timestamp inside the marker is UTC — `date -u`).
A slot only counts once its start time + grace (default 20 min) has passed; each slot
is matched one-to-one to a start in [slot, slot+grace] so one manual run can't cover
two slots. Shortfall >= min-shortfall (default 2) → operator alert
(key=cycle_cadence_gap, dedup 2h) + exit 1. The overnight window (22:00→06:00 PT)
generates ZERO expected slots, so this can run around the clock with no false alarms.

DIAGNOSIS (QA 2026-07-03): a missed slot has TWO distinct root causes and the alert
must not conflate them. (a) The cron agent-turn never invoked run-cycle-detached.sh
(the gateway/model-fallback failure this watchdog was built for). (b) The cron DID
fire but run-cycle.sh lock-skipped: a long-stalled cycle (e.g. the 7/2 Open-Meteo
aggregate stall, 04:00→06:31Z, 2.5h) holds logs/.cycle.lock and the skipped run
`exit 0`s BEFORE the tee block, so no 'cycle start' marker is ever written. Before
blaming the gateway we probe the lock: held by a live pid → the alert points at the
stalled cycle instead, so the operator isn't sent to the gateway while a live cycle
is wedged holding the lock. The shortfall math is identical either way.

CONSTRAINTS: strictly READ-ONLY apart from the alert (trader.notify appends its own
logs/alerts.jsonl). Never raises out of evaluate(); a broken log line is skipped.
Exit: 0 cadence ok · 1 gap detected (alerted).

Usage:
  python3 bin/check_cycle_cadence.py [--window-hours 3] [--grace-minutes 20]
      [--min-shortfall 2] [--now 2026-07-02T21:00:00Z] [--json] [--no-alert]
Env (same knobs, for the bin/sync-live.sh wiring):
  KALSHI_WEATHER_CADENCE_WINDOW_HOURS, KALSHI_WEATHER_CADENCE_GRACE_MINUTES,
  KALSHI_WEATHER_CADENCE_MIN_SHORTFALL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

# Patched by tests (tests/test_cycle_cadence.py) to point at a synthetic log dir.
LOG_DIR = ROOT / "logs"

TZ = ZoneInfo("America/Los_Angeles")

# Cron schedules MIRRORED here — keep in sync with the OpenClaw cron entries
# (paper '0 6-22 * * *', LIVE '20 9,11,13,15,17,19 * * *'). Both invoke
# run-cycle.sh, which writes the same start marker, so one matcher covers both.
PAPER_SLOT_MINUTE = 0
PAPER_SLOT_HOURS = tuple(range(6, 23))          # 06:00..22:00 inclusive
LIVE_SLOT_MINUTE = 20
LIVE_SLOT_HOURS = (9, 11, 13, 15, 17, 19)

# run-cycle.sh line ~70: echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) cycle start ==="
_START_RE = re.compile(r"^=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z cycle start ===")


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _lock_stall_hint(log_dir: Path | None = None) -> str | None:
    """If logs/.cycle.lock is held by a LIVE process, return a diagnosis string.

    WHY (QA 2026-07-03): run-cycle.sh's lock-skip path exits BEFORE the tee block,
    so a slot whose cron fired but hit the lock writes no 'cycle start' marker and
    counts as missed here. When that happens the right diagnosis is the stalled
    cycle holding the lock, NOT the OpenClaw gateway. Mirrors run-cycle.sh's own
    liveness rules: empty/missing pid file = holder still acquiring (treat as
    alive); a non-empty pid that is dead = stale lock run-cycle.sh will reclaim
    (→ None, fall back to the gateway hypothesis). CONSTRAINTS: strictly read-only
    (stat/read/kill -0 only), never raises — diagnosis garnish must not break the
    watchdog."""
    try:
        lock = (Path(log_dir) if log_dir is not None else LOG_DIR) / ".cycle.lock"
        if not lock.exists():
            return None
        pid_s = ""
        try:
            pid_s = (lock / "pid").read_text().strip()
        except OSError:
            pass
        if pid_s:
            try:
                os.kill(int(pid_s), 0)            # signal 0 = existence probe only
            except (ProcessLookupError, ValueError, OverflowError):
                return None                       # stale lock (dead owner) — not a stall
            except PermissionError:
                pass                              # alive but not ours — still held
        since = datetime.fromtimestamp(lock.stat().st_mtime, tz=timezone.utc
                                       ).astimezone(TZ).strftime("%m-%d %H:%M PT")
        return (f"a cycle (pid {pid_s or 'acquiring'}) has been running/stalled since "
                f"{since} holding logs/.cycle.lock — lock-skipped slots write no start "
                f"marker; check the stalled cycle, not the gateway")
    except Exception:  # noqa: BLE001 — the watchdog must never crash the sync job
        return None


def expected_slots(window_start: datetime, now_utc: datetime,
                   grace: timedelta) -> list[datetime]:
    """Cron slots (as UTC datetimes) that are both IN the window and DUE.

    A slot is due only once slot+grace <= now — a cycle that fired 10 min late is
    healthy, and a slot whose grace hasn't elapsed must not count as missed.
    Slots are generated on the LA wall clock (cron semantics), then converted.
    The extra -1 day of generation is DST/UTC-offset safety margin; the window
    filter discards anything outside [window_start, now]."""
    slots: list[datetime] = []
    d = window_start.astimezone(TZ).date() - timedelta(days=1)
    end = now_utc.astimezone(TZ).date()
    while d <= end:
        for h in PAPER_SLOT_HOURS:
            slots.append(datetime(d.year, d.month, d.day, h, PAPER_SLOT_MINUTE, tzinfo=TZ))
        for h in LIVE_SLOT_HOURS:
            slots.append(datetime(d.year, d.month, d.day, h, LIVE_SLOT_MINUTE, tzinfo=TZ))
        d += timedelta(days=1)
    utc = sorted(s.astimezone(timezone.utc) for s in slots)
    return [s for s in utc if s >= window_start and s + grace <= now_utc]


def actual_starts(window_start: datetime, now_utc: datetime,
                  log_dir: Path | None = None) -> list[datetime]:
    """UTC timestamps of 'cycle start' markers within [window_start, now].

    Reads logs/cycle-<LOCAL-date>.log for every LA-local date the window touches:
    the file is named by the local date at cycle start, so a 22:04 PT start lives
    in that local date's file even though its UTC timestamp is the next day."""
    ld = Path(log_dir) if log_dir is not None else LOG_DIR
    starts: list[datetime] = []
    d = window_start.astimezone(TZ).date()
    end = now_utc.astimezone(TZ).date()
    while d <= end:
        p = ld / f"cycle-{d.isoformat()}.log"
        if p.exists():
            for ln in p.read_text(errors="replace").splitlines():
                m = _START_RE.match(ln)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
                if window_start <= ts <= now_utc:
                    starts.append(ts)
        d += timedelta(days=1)
    return sorted(starts)


def evaluate(now: datetime | None = None, window_hours: float | None = None,
             grace_minutes: float | None = None, min_shortfall: int | None = None,
             do_alert: bool = True, log_dir: Path | None = None) -> dict:
    """Expected-vs-actual cadence check. Returns a dict; severity 1 = gap (alerted)."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if window_hours is None:
        window_hours = _env_num("KALSHI_WEATHER_CADENCE_WINDOW_HOURS", 3)
    if grace_minutes is None:
        grace_minutes = _env_num("KALSHI_WEATHER_CADENCE_GRACE_MINUTES", 20)
    if min_shortfall is None:
        min_shortfall = int(_env_num("KALSHI_WEATHER_CADENCE_MIN_SHORTFALL", 2))
    grace = timedelta(minutes=grace_minutes)
    window_start = now_utc - timedelta(hours=window_hours)

    slots = expected_slots(window_start, now_utc, grace)
    starts = actual_starts(window_start, now_utc, log_dir)

    # One-to-one greedy match: each due slot claims the EARLIEST unclaimed start in
    # [slot, slot+grace]. One-to-one so a single (possibly manual) run can't satisfy
    # two adjacent slots (a :00 paper slot's grace window ends exactly where the :20
    # LIVE slot begins, so a late paper start must not also cover the LIVE slot).
    used: set[int] = set()
    missed: list[datetime] = []
    for slot in slots:
        hit = None
        for i, s in enumerate(starts):
            if i in used:
                continue
            if s > slot + grace:
                break                      # starts are sorted — nothing later matches
            if s >= slot:
                hit = i
                break
        if hit is None:
            missed.append(slot)
        else:
            used.add(hit)

    shortfall = len(missed)
    severity = 1 if shortfall >= min_shortfall else 0
    missed_pt = [m.astimezone(TZ).strftime("%m-%d %H:%M PT") for m in missed]

    if severity and do_alert:
        try:
            from trader.notify import alert as _send
            # Disambiguate the root cause (QA 2026-07-03): a live lock holder means
            # the misses are lock-skips of a stalled cycle; only blame the gateway
            # when nothing is holding logs/.cycle.lock.
            diagnosis = _lock_stall_hint(log_dir) or (
                "the cron agent-turn is likely reporting ok WITHOUT invoking "
                "run-cycle-detached.sh — check the OpenClaw gateway / model fallback")
            _send(f"CYCLE CADENCE GAP: {shortfall}/{len(slots)} scheduled cycle starts "
                  f"missing in the last {window_hours:g}h (missed: {', '.join(missed_pt)}). "
                  f"Likely cause: {diagnosis}.",
                  key="cycle_cadence_gap", dedup_seconds=3600 * 2)
        except Exception:  # noqa: BLE001 — the watchdog must never crash the sync job
            pass

    return {"severity": severity, "now_utc": now_utc.isoformat(),
            "window_hours": window_hours, "grace_minutes": grace_minutes,
            "min_shortfall": min_shortfall,
            "expected": [s.isoformat() for s in slots],
            "actual_starts": [s.isoformat() for s in starts],
            "missed": [m.isoformat() for m in missed], "missed_pt": missed_pt,
            "shortfall": shortfall}


def main() -> int:
    ap = argparse.ArgumentParser(description="Expected-vs-actual cycle-start cadence watchdog.")
    ap.add_argument("--window-hours", type=float, default=None,
                    help="lookback window (default env KALSHI_WEATHER_CADENCE_WINDOW_HOURS or 3)")
    ap.add_argument("--grace-minutes", type=float, default=None,
                    help="late-start allowance per slot (default env ..._GRACE_MINUTES or 20)")
    ap.add_argument("--min-shortfall", type=int, default=None,
                    help="missed slots needed to alert (default env ..._MIN_SHORTFALL or 2)")
    ap.add_argument("--now", default=None, metavar="UTC_ISO",
                    help="evaluate as-of this UTC time, e.g. 2026-07-02T21:00:00Z (testing)")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    res = evaluate(now=now, window_hours=args.window_hours, grace_minutes=args.grace_minutes,
                   min_shortfall=args.min_shortfall, do_alert=not args.no_alert)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"CYCLE CADENCE: {'GAP' if res['severity'] else 'OK'}  "
              f"expected={len(res['expected'])} actual={len(res['actual_starts'])} "
              f"shortfall={res['shortfall']} (window {res['window_hours']:g}h, "
              f"grace {res['grace_minutes']:g}m, alert at >= {res['min_shortfall']})")
        for m in res["missed_pt"]:
            print(f"  MISSED slot: {m}")
    return res["severity"]


if __name__ == "__main__":
    sys.exit(main())

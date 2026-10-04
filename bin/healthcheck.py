#!/usr/bin/env python3
"""bin/healthcheck.py — trading-OUTCOME health (alert-only).

"Agent-turn OK" != "trading OK". This checks what actually matters — did the cycle
run, is fair-values fresh, did live posting fail, is the live book syncing — and
ALERTS on a transition to worse (and on recovery). It NEVER auto-disables live
(that decision is yours: bin/halt_live.py --on, bin/flatten_live.py --execute).

Exit code: 0 OK, 1 DEGRADED, 2 CRITICAL. Importable render() for reviews.

Env (windows): KALSHI_WEATHER_HEALTH_CYCLE_HOURS (6), _FV_MIN (180),
KALSHI_WEATHER_HEALTH_SYNC_HOURS (6), _POST_FAIL_WINDOW_H (4).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

STATE = ROOT / "state"
LOGS = ROOT / "logs"
HEALTH_STATE = STATE / "health-state.json"
ALERTS_LOG = LOGS / "alerts.jsonl"

SEV_NAME = {0: "OK", 1: "DEGRADED", 2: "CRITICAL"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_min(p: Path):
    return (time.time() - p.stat().st_mtime) / 60.0 if p.exists() else None


def _newest_cycle_log_age_min():
    """Age (min) of the most recently-written cycle-*.log, or None if none exist."""
    logs = sorted(LOGS.glob("cycle-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return _age_min(logs[0]) if logs else None


def _cycle_starts(scan_logs: int = 2) -> list:
    """Recent cycle-start timestamps (aware-UTC), chronological, from the `=== <iso-ts> cycle start
    ===` markers in the newest `scan_logs` cycle logs. Two files matter at the first cycle of a new
    local day: the previous cycle's start lives in yesterday's log. Fully exception-safe → []."""
    try:
        logs = sorted(LOGS.glob("cycle-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:scan_logs]
    except Exception:
        return []
    starts = []
    for lg in reversed(logs):  # oldest file first → appended order stays chronological
        try:
            for line in lg.read_text(errors="replace").splitlines():
                s = line.strip()
                if s.startswith("=== ") and s.endswith(" cycle start ==="):
                    ts = s[len("=== "):-len(" cycle start ===")].strip()
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt.tzinfo is not None:
                        starts.append(dt)
        except Exception:
            continue
    return starts


def _openclaw_cron_failures(min_consec: int):
    """Enabled openclaw cron jobs whose name looks kalshi-related and whose consecutiveErrors >=
    min_consec, as [(name, consecutiveErrors)]. Returns None when the cron state can't be read
    (fresh deploy / sandbox) so the caller can distinguish "clean" from "not present". The gateway
    LLM oversight lane (nightly review, loss post-mortems, brier report, handoff-scan, dashboard)
    runs OUTSIDE this repo and nothing here watched it — it silently died on an invalid provider key
    2026-07-11 with failure alerts undelivered (2026-07-13 audit). Fully exception-safe."""
    try:
        cron = Path(os.environ.get("KALSHI_WEATHER_CRON_DIR") or os.path.expanduser("~/.openclaw/cron"))
        st = json.loads((cron / "jobs-state.json").read_text())
        cfg = json.loads((cron / "jobs.json").read_text())
    except Exception:
        return None
    sjobs = st.get("jobs", st) if isinstance(st, dict) else {}
    cjobs = cfg.get("jobs", cfg) if isinstance(cfg, dict) else cfg
    # jobs.json stores jobs as a LIST of objects (each with "id"/"name"); jobs-state.json keys state
    # by that id in a DICT. Build id→name from whichever shape jobs.json uses.
    names = {}
    if isinstance(cjobs, dict):
        for jid, jv in cjobs.items():
            if isinstance(jv, dict):
                names[jid] = jv.get("name") or jv.get("title") or jid
    elif isinstance(cjobs, list):
        for jv in cjobs:
            if isinstance(jv, dict) and jv.get("id"):
                names[jv["id"]] = jv.get("name") or jv.get("title") or jv["id"]
    if not isinstance(sjobs, dict):
        return []
    out = []
    for jid, jv in sjobs.items():
        if not isinstance(jv, dict):
            continue
        state = jv.get("state", jv)
        try:
            ce = int(state.get("consecutiveErrors", 0) or 0)
        except (TypeError, ValueError):
            ce = 0
        enabled = True
        si = jv.get("scheduleIdentity")
        if si:
            try:
                enabled = bool(json.loads(si).get("enabled", True))
            except Exception:
                enabled = True
        name = str(names.get(jid, jid))
        if enabled and ce >= min_consec and "kalshi" in name.lower():
            out.append((name, ce))
    return out


# The `claude -p` CLI emits these flush-left on an Anthropic-API auth failure (the supervisor 401;
# the real line is "Failed to authenticate. API Error: 401 Invalid authentication credentials").
# Matched at the START of a stripped log line so the CLI's own error line trips it, but the same
# phrase quoted mid-sentence inside a normal supervisor report body (e.g. narrating a live-order
# 401/410) does NOT — the report is prose/markdown, never a line beginning with these tokens.
_SUPERVISOR_AUTH_SIGNATURES = ("failed to authenticate", "invalid authentication credentials")


def _supervisor_auth_status(window_h: float):
    """Whether the most-recent supervisor run (within `window_h`) failed Claude Code auth.

    Reads the newest logs/supervisor-*.log and walks its run blocks (each begins
    ``=== <ts> supervisor start ===``). Returns ``(severity, detail)`` or ``None`` when there is
    nothing to assert: no supervisor log yet (fresh deploy), or the newest run is older than the
    window (a stale historical 401 must not ping forever, and the supervisor is intentionally
    quiet outside its 6 daytime :30-PT slots). Fully exception-safe — never raises into _checks().
    """
    try:
        logs = sorted(LOGS.glob("supervisor-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    if not logs:
        return None
    log = logs[0]
    try:
        lines = log.read_text(errors="replace").splitlines()[-400:]
    except Exception:
        return None

    runs = []  # (start_dt|None, had_auth_fail) per run block, in file order
    started, cur_start, cur_fail = False, None, False
    for raw in lines:
        s = raw.strip()
        if s.endswith(" supervisor start ==="):
            if started:
                runs.append((cur_start, cur_fail))
            started, cur_start, cur_fail = True, None, False
            if s.startswith("=== "):
                mid = s[len("=== "):-len(" supervisor start ===")].strip()
                try:
                    dt = datetime.fromisoformat(mid.replace("Z", "+00:00"))
                    # A tz-NAIVE but ISO-parseable ts (format drift / a corrupt or model-echoed
                    # line without the trailing Z) would make `_now() - dt` raise TypeError below
                    # and crash the whole healthcheck. Reject it → fall back to the file mtime.
                    cur_start = dt if dt.tzinfo is not None else None
                except Exception:
                    cur_start = None
            continue
        # Anchor to line start: the CLI error is flush-left; a report body quoting the phrase
        # mid-line (or as a markdown bullet) must not false-trip the check.
        if started and any(s.lower().startswith(sig) for sig in _SUPERVISOR_AUTH_SIGNATURES):
            cur_fail = True
    if started:
        runs.append((cur_start, cur_fail))
    if not runs:
        return None

    last_start, last_fail = runs[-1]
    if last_start is not None:
        age_h = (_now() - last_start).total_seconds() / 3600.0
    else:
        a = _age_min(log)
        age_h = a / 60.0 if a is not None else None
    if age_h is not None and age_h > window_h:
        return None
    if not last_fail:
        return (0, "supervisor `claude -p` auth ok")

    nfail = 0
    for _st, f in reversed(runs):
        if not f:
            break
        nfail += 1
    return (1,
            f"supervisor `claude -p` auth FAILED (401) on last {nfail} run(s) in {log.name} — "
            f"headless Claude Code token invalid; oversight reporter blind (deterministic tripwires "
            f"still run). Fix: `claude setup-token` -> ~/.openclaw/claude-code-oauth-token")


def _count_recent_alerts(key: str, since: datetime) -> int:
    """Count alerts.jsonl rows with this key whose ts >= since (the event occurred)."""
    if not ALERTS_LOG.exists():
        return 0
    n = 0
    try:
        for line in ALERTS_LOG.read_text().splitlines()[-1000:]:
            line = line.strip()
            if not line or key not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("key") or "") != key:
                continue
            try:
                ts = datetime.fromisoformat((r.get("ts") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= since:
                n += 1
    except Exception:
        pass
    return n


def _error_model_saturated(cap: float, min_n: float):
    """Cities whose RAW learned forecast bias exceeds the get_bias ±cap clamp (model/error_tracker.py
    BIAS_CAP_F), so the fair-value correction is silently UNDER-applied (2026-07-13 audit: HOU/PHX run
    ~2.3-3.5°F too warm). Reads state/error-model.json directly (dependency-light + test-isolated).
    Returns [(city, raw_bias_f, n_effective)] worst-first, [] if clean, None if unreadable."""
    try:
        em = json.loads((STATE / "error-model.json").read_text())
    except Exception:
        return None
    cities = em.get("cities", {}) if isinstance(em, dict) else {}
    if not isinstance(cities, dict):
        return []
    out = []
    for c, v in cities.items():
        if not isinstance(v, dict):
            continue
        raw = v.get("bias_f")
        n = v.get("n_effective", 0.0)
        try:
            if raw is not None and float(n or 0) >= min_n and abs(float(raw)) > cap:
                out.append((c, round(float(raw), 2), round(float(n), 1)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: -abs(x[1]))
    return out


def _checks(last_check_utc: datetime) -> list[tuple[str, int, str]]:
    """Return [(check_name, severity, detail)]. severity: 0 OK, 1 DEGRADED, 2 CRITICAL."""
    out = []
    cycle_hours = float(os.environ.get("KALSHI_WEATHER_HEALTH_CYCLE_HOURS", "6"))
    fv_min = float(os.environ.get("KALSHI_WEATHER_HEALTH_FV_MIN", "180"))
    # 16h default: the LIVE cron runs only 3x/day (gaps up to ~14h overnight), so a
    # tighter window would false-alarm nightly. This flags genuinely-stuck sync only.
    sync_hours = float(os.environ.get("KALSHI_WEATHER_HEALTH_SYNC_HOURS", "16"))
    post_window_h = float(os.environ.get("KALSHI_WEATHER_HEALTH_POST_FAIL_WINDOW_H", "4"))

    # 1. Cycle liveness
    age = _newest_cycle_log_age_min()
    if age is None:
        out.append(("cycle_liveness", 2, "no cycle-*.log found"))
    elif age > cycle_hours * 60:
        out.append(("cycle_liveness", 2, f"newest cycle log is {age/60:.1f}h old (> {cycle_hours}h)"))
    else:
        out.append(("cycle_liveness", 0, f"cycle log {age:.0f}m old"))

    # 2. Fair-values freshness. run-cycle.sh evaluates health at cycle START (step 0), BEFORE step 1
    # rebuilds fair-values.json — so at check time FV is always the PREVIOUS cycle's build and its age
    # ≈ the gap since that cycle. That gap is ~1-2h intraday but ~8h across the scheduled overnight idle
    # (cycles run ~06:00-22:00 PT only), which tripped the fixed 180m threshold and paged a false
    # DEGRADED→OK pair EVERY morning (alert fatigue; 2026-07-13 audit). Fix: expect FV to be about as
    # fresh as the previous cycle ran — alarm only when it is materially STALER than that (rebuilds
    # actually failing), never merely because the market was idle overnight. Floor at fv_min so intraday
    # sensitivity is unchanged; fall back to fv_min when the cycle cadence can't be read.
    fv_age = _age_min(ROOT / "fair-values.json")
    fv_margin = float(os.environ.get("KALSHI_WEATHER_HEALTH_FV_MARGIN_MIN", "120"))
    _starts = _cycle_starts()
    prev_gap_min = (_now() - _starts[-2]).total_seconds() / 60.0 if len(_starts) >= 2 else None
    fv_allowed = max(prev_gap_min + fv_margin, fv_min) if prev_gap_min is not None else fv_min
    if fv_age is None:
        out.append(("fair_values_fresh", 1, "fair-values.json missing"))
    elif fv_age > fv_allowed:
        _why = (f"previous cycle {prev_gap_min/60:.1f}h ago + {fv_margin:.0f}m margin"
                if prev_gap_min is not None else f"{fv_min/60:.1f}h")
        out.append(("fair_values_fresh", 1,
                    f"fair-values.json {fv_age/60:.1f}h old (> {_why}) — rebuild failing?"))
    else:
        out.append(("fair_values_fresh", 0, f"fair-values {fv_age:.0f}m old"))

    # 3. Live posting — fresh failures only (bounded window, since last check).
    since = max(last_check_utc, _now() - timedelta(hours=post_window_h))
    fails = _count_recent_alerts("live_order_fail", since)
    if fails > 0:
        out.append(("live_posting", 2, f"{fails} live order failure(s) since {since:%H:%M}Z (410/API)"))
    else:
        out.append(("live_posting", 0, "no fresh live order failures"))

    # 4. Live sync freshness (only if the live arm has ever synced)
    sync = STATE / "live-premium" / "sync-state.json"
    sync_age = _age_min(sync)
    if sync_age is not None and sync_age > sync_hours * 60:
        out.append(("live_sync_fresh", 1, f"live sync {sync_age/60:.1f}h stale (> {sync_hours}h)"))
    elif sync_age is not None:
        out.append(("live_sync_fresh", 0, f"live sync {sync_age:.0f}m old"))

    # 5. GEFS degradation (recent alert)
    if _count_recent_alerts("gefs_fail", _now() - timedelta(hours=24)) > 0:
        out.append(("gefs", 1, "gefs ingest degraded in last 24h"))

    # 6. Idle live arm (ever posted, but none in 24h)
    if os.environ.get("KALSHI_WEATHER_HEALTH_SKIP_IDLE") != "1":
        _lp = None
        try:
            from bin.ab_health import _live_posted_24h as _lp  # type: ignore
        except Exception:
            try:
                sys.path.insert(0, str(ROOT / "bin"))
                from ab_health import _live_posted_24h as _lp  # type: ignore
            except Exception:
                _lp = None
        if _lp is not None:
            try:
                n24, ever = _lp()
                if ever and n24 == 0:
                    out.append(("live_idle", 1, "live arm posted before but 0 orders in 24h"))
            except Exception:
                pass

    # 7. Error-model freshness. The per-city bias/std EWMA (state/error-model.json)
    # must keep updating from settlements — it silently froze Jun 18–Jul 6 2026 because
    # maker positions carried no forecast_f, and nothing watched it. Flag if it hasn't
    # updated within the window while cycles are clearly running.
    em_days = float(os.environ.get("KALSHI_WEATHER_HEALTH_ERRMODEL_DAYS", "3"))
    em_path = STATE / "error-model.json"
    if em_path.exists():
        em_age_days = None
        try:
            em = json.loads(em_path.read_text())
            ts = em.get("updated_utc") or em.get("updated_at_utc")
            if ts:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                em_age_days = (_now() - dt).total_seconds() / 86400.0
        except Exception:
            em_age_days = None
        if em_age_days is None:
            # No parseable timestamp → fall back to file mtime.
            a = _age_min(em_path)
            em_age_days = a / 1440.0 if a is not None else None
        if em_age_days is not None and em_age_days > em_days:
            out.append(("error_model_fresh", 1,
                        f"error-model.json {em_age_days:.1f}d stale (> {em_days:.0f}d) — "
                        f"bias/std EWMA not learning from settlements"))
        elif em_age_days is not None:
            out.append(("error_model_fresh", 0, f"error-model {em_age_days:.1f}d old"))

    # 8. Persistent LIVE halt. A halt blocks NEW orders but leaves resting GTC quotes on
    # the book AND disables the refresh/halt-cancel machinery that manages them, so stale
    # quotes get adversely selected while the operator believes trading is stopped. A halt
    # left on is also the 2026-07-01 'x' incident (~35.5h outage). Surface a halt older than
    # the grace so it can't silently persist. (A brief maintenance halt stays quiet.)
    halt_grace_min = float(os.environ.get("KALSHI_WEATHER_HEALTH_HALT_GRACE_MIN", "90"))
    halt_path = STATE / "LIVE_HALT.json"
    if halt_path.exists():
        reason, age_min = "halted", None
        try:
            h = json.loads(halt_path.read_text())
            reason = str(h.get("reason", "halted"))[:80]
            ts = h.get("tripped_utc")
            if ts:
                age_min = (_now() - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 60.0
        except Exception:
            pass
        if age_min is None or age_min > halt_grace_min:
            hrs = f"{age_min/60:.1f}h" if age_min is not None else "unknown age"
            out.append(("live_halt_persistent", 1,
                        f"LIVE halted {hrs} (reason: {reason}) — resting quotes unmanaged; "
                        f"flatten_live.py --execute or resume with halt_live.py --off"))
        else:
            out.append(("live_halt_persistent", 0, f"live halted {age_min:.0f}m (within grace)"))

    # 9. Operator alert channel. bin/notify_selfcheck.py (run from the gateway-INDEPENDENT
    # sync job at :10/:40) writes state/notify-channel-health.json when the gateway token
    # desyncs or sends start failing — the mode that blinded the operator for ~8h on
    # 2026-07-07. Surfacing it here means the down-state appears in every cycle's HEALTH
    # block + the supervisor read (so the self-check is not a decorative instrument). We map
    # its severity through: severity 2 (sends actively failing) → CRITICAL, so evaluate()'s
    # own alert attempt fails and prints the loud local "UNDELIVERED" trace to the cycle log.
    ch_path = STATE / "notify-channel-health.json"
    ch_age = _age_min(ch_path)
    if ch_age is None:
        pass  # self-check has never written it (fresh deploy) — cycle_liveness/cadence cover this
    elif ch_age > 90:
        # The self-check (bin/notify_selfcheck.py, run from the sync job) has stopped refreshing
        # this file. Do NOT silently drop the check — a frozen 'down' record would otherwise
        # vanish and let health flip falsely to OK. Surface the stall itself as DEGRADED/UNKNOWN.
        out.append(("alert_channel", 1,
                    f"notify self-check stale ({ch_age/60:.1f}h) — channel state UNKNOWN; "
                    f"is the launchd sync job running?"))
    else:
        # Guard the whole read: a corrupt / non-dict / odd-typed health file must never crash
        # _checks() (unlike the pre-fix version whose .get()/int() ran outside the try).
        try:
            ch = json.loads(ch_path.read_text())
            if not isinstance(ch, dict):
                ch = {}
            sev = int(ch.get("severity", 0)) if str(ch.get("status", "ok")) != "ok" else 0
            detail = str(ch.get("detail", "operator alert channel degraded"))[:200]
        except Exception:
            sev, detail = 0, ""
        if sev >= 1:
            out.append(("alert_channel", min(sev, 2), detail))
        else:
            out.append(("alert_channel", 0, "operator alert channel ok"))

    # 10. Nightly git backup. bin/backup_workspace.sh (run from the same gateway-independent
    # sync job, once per local day per repo) replaced the LLM-driven openclaw backup crons after
    # they died silently on provider billing 2026-07-03→10 and a week of state went unbacked. It
    # writes state/backup-health.json on every ATTEMPTED backup — attempts stop for the day after
    # success, so the healthy mtime cadence is ~24h (vs every 30min while failing); 30h ≈ 1.25×
    # budget. Same contract as check 9: a stale or failing record is surfaced, never silently
    # dropped. Capped at WARN — a backup gap is a durability risk, not a trading fault.
    bk_path = STATE / "backup-health.json"
    bk_age = _age_min(bk_path)
    if bk_age is None:
        pass  # backup has never run (fresh deploy) — nothing to assert yet
    elif bk_age > 30 * 60:
        out.append(("backup_fresh", 1,
                    f"backup health stale ({bk_age/60:.1f}h) — nightly git backup not running; "
                    f"is the launchd sync job alive?"))
    else:
        try:
            bk = json.loads(bk_path.read_text())
            if not isinstance(bk, dict):
                bk = {}
            bad = str(bk.get("status", "ok")) != "ok"
            detail = str(bk.get("detail", "git backup failing"))[:200]
        except Exception:
            bad, detail = False, ""
        if bad:
            out.append(("backup_fresh", 1, detail))
        else:
            out.append(("backup_fresh", 0, "nightly git backup ok"))

    # 11. Supervisor auth. The read-only oversight reporter (bin/supervise.sh) runs headless via
    # launchd and authenticates with the machine's Claude Code OAuth LOGIN — NOT the openclaw
    # gateway token. A re-login/reboot rotated that credential 2026-07-11T12:22Z and EVERY
    # supervisor run 401'd from 16:32Z on; for ~2 days (spanning a drawdown) the LLM summary/anomaly
    # read was blind and nothing paged — only the deterministic tripwires kept running. This
    # deterministic check restores the signal: if the newest supervisor run failed Claude Code auth,
    # raise DEGRADED through the normal health alert path (gateway/Telegram + Discord fallback, which
    # is itself independent of the down claude reporter). Capped at WARN — degraded oversight, not a
    # trading fault. Window default 18h spans the ~14h overnight gap between :30-PT slots so the
    # state doesn't flap OK/DEGRADED nightly.
    sup_window_h = float(os.environ.get("KALSHI_WEATHER_HEALTH_SUPERVISOR_WINDOW_H", "18"))
    sup = _supervisor_auth_status(sup_window_h)
    if sup is not None:
        out.append(("supervisor_auth", sup[0], sup[1]))

    # 12. OpenClaw cron oversight lane. The gateway-driven LLM jobs (nightly review, loss post-mortems,
    # brier report, handoff-scan, dashboard-live-data) run OUTSIDE this repo; nothing here watched them,
    # and they silently died on an invalid provider API key 2026-07-11 with failure notifications
    # UNDELIVERED (2026-07-13 audit) — including the nightly review that renders the A/B health used for
    # the quote-refresh decision. Surface an ENABLED kalshi cron job stuck at >= N consecutive errors so
    # a dead oversight lane pages instead of going silent. Capped at WARN — degraded oversight, not a
    # trading fault. Skipped entirely (not a false OK) when the cron state can't be read.
    cron_min = int(os.environ.get("KALSHI_WEATHER_HEALTH_CRON_ERR", "3"))
    cron_fail = _openclaw_cron_failures(cron_min)
    if cron_fail is not None:
        if cron_fail:
            listed = ", ".join(f"{n} ({c}x)" for n, c in sorted(cron_fail, key=lambda x: -x[1])[:6])
            out.append(("cron_oversight", 1,
                        f"{len(cron_fail)} enabled kalshi cron job(s) failing >= {cron_min}x: {listed} — "
                        f"LLM oversight lane down (check gateway provider key/quota/billing)"))
        else:
            out.append(("cron_oversight", 0, "kalshi cron oversight lane ok"))

    # 13. restic secondary (offsite) backup. Same contract as check 10, for the SECOND backup layer.
    # ~/.config/restic/backup-openclaw.sh (launchd, daily 12:30) snapshots ~/openclaw — the untracked
    # state (~1.3 GiB) the git backup does NOT cover — to an offsite sftp repo. It silently died on a
    # PATH bug ("restic: command not found", exit 127) for ~5 weeks with no watcher (2026-07-13 audit);
    # the script now writes state/restic-health.json on every attempt (ok/fail). A daily cadence → a
    # healthy mtime ~24h, so 30h ≈ 1.25x budget flags a missed/failing run (incl. an unreachable repo).
    # Capped at WARN — durability redundancy, not a trading fault. Absent file (never run) → skip.
    rk_path = STATE / "restic-health.json"
    rk_age = _age_min(rk_path)
    if rk_age is None:
        pass  # restic has never written a health record yet — nothing to assert
    elif rk_age > 30 * 60:
        out.append(("restic_backup", 1,
                    f"restic backup health stale ({rk_age/60:.1f}h) — daily offsite backup not running; "
                    f"is the launchd restic job alive / is the sftp repo reachable?"))
    else:
        try:
            rk = json.loads(rk_path.read_text())
            if not isinstance(rk, dict):
                rk = {}
            bad = str(rk.get("status", "ok")) != "ok"
            detail = str(rk.get("detail", "restic backup failing"))[:200]
        except Exception:
            bad, detail = False, ""
        if bad:
            out.append(("restic_backup", 1, detail))
        else:
            out.append(("restic_backup", 0, "restic offsite backup ok"))

    # 14. Error-model bias-cap saturation. get_bias (model/error_tracker.py) clamps each city's learned
    # forecast bias to ±BIAS_CAP_F (2°F) to prevent model divergences; a city whose TRUE bias exceeds
    # that is silently UNDER-corrected, leaving its fair values systematically biased (2026-07-13 audit:
    # HOU/PHX ~2.3-3.5°F too warm — a real hot-city mispricing nothing surfaced). Report well-sampled
    # saturated cities. WARN only — a model-quality signal; widening/adapting the cap is an operator
    # decision (the cap exists to prevent divergences), so this informs, it does not gate trading.
    bias_cap = float(os.environ.get("KALSHI_WEATHER_HEALTH_BIAS_CAP_F", "2.0"))   # == error_tracker.BIAS_CAP_F
    bias_min_n = float(os.environ.get("KALSHI_WEATHER_HEALTH_BIAS_MIN_N", "10"))
    sat = _error_model_saturated(bias_cap, bias_min_n)
    if sat is not None:
        if sat:
            listed = ", ".join(f"{c} raw={b:+.1f}°F(n={n:.0f})" for c, b, n in sat[:6])
            out.append(("error_model_saturation", 1,
                        f"{len(sat)} city bias(es) pinned at the ±{bias_cap:.0f}°F cap — fair values "
                        f"under-corrected: {listed} (model-quality; widening the cap is an operator call)"))
        else:
            out.append(("error_model_saturation", 0, "no city bias pinned at the cap"))

    return out


def _load_state() -> dict:
    try:
        return json.loads(HEALTH_STATE.read_text()) if HEALTH_STATE.exists() else {}
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        HEALTH_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, HEALTH_STATE)
    except Exception:
        pass


def evaluate() -> tuple[int, list[tuple[str, int, str]], dict]:
    """Run checks, update health-state, alert on transition. Returns (severity, checks, prev)."""
    prev = _load_state()
    last_check = prev.get("last_check_utc")
    try:
        last_check_dt = datetime.fromisoformat(last_check) if last_check else (_now() - timedelta(hours=4))
    except Exception:
        last_check_dt = _now() - timedelta(hours=4)

    checks = _checks(last_check_dt)
    severity = max((s for _, s, _ in checks), default=0)
    status = SEV_NAME[severity]
    prev_status = prev.get("status", "OK")

    consec = int(prev.get("consecutive_failures", 0))
    consec = consec + 1 if severity == 2 else 0

    new = {
        "status": status,
        "consecutive_failures": consec,
        "last_ok_utc": _now().isoformat() if severity == 0 else prev.get("last_ok_utc"),
        "last_check_utc": _now().isoformat(),
        "checks": {name: SEV_NAME[s] for name, s, _ in checks},
    }
    _save_state(new)

    # Alert on a transition (worse OR recovery) AND re-alert while CRITICAL persists — a
    # stuck CRITICAL must keep pinging, not go silent after the first cycle.
    transition = status != prev_status
    if transition or severity >= 2:
        bad = [f"{name}: {detail}" for name, s, detail in checks if s >= 1]
        if severity >= 1:
            rec = ("posting broken → `bin/halt_live.py --on` + `bin/flatten_live.py --execute`"
                   if any(n == "live_posting" and s == 2 for n, s, _ in checks)
                   else "investigate; halt with `bin/halt_live.py --on` if needed")
            arrow = f"{prev_status}→{status}" if transition else f"{status} (persists)"
            msg = f"health {arrow}: " + "; ".join(bad)[:240] + f". {rec}"
        else:
            msg = f"health recovered → OK (was {prev_status})"
        # Persistent-CRITICAL re-alerts use a longer dedup so we re-ping without spamming.
        dd = 60 if transition else 1800
        delivered = False
        try:
            from trader.notify import alert
            delivered = bool(alert(msg, key="health_status", dedup_seconds=dd))
        except Exception as e:
            print(f"[healthcheck] alert dispatch raised: {e}", file=sys.stderr)
        if not delivered and severity >= 2:
            # CRITICAL alert not delivered (send failed or deduped) — leave a loud local
            # trace in the cycle log so the failure is never invisible.
            print(f"[healthcheck] ⚠️ CRITICAL ALERT UNDELIVERED: {msg}", file=sys.stderr)

    return severity, checks, prev


def render() -> str:
    severity, checks, _ = evaluate()
    lines = [f"HEALTH: {SEV_NAME[severity]}"]
    for name, s, detail in checks:
        mark = {0: "✓", 1: "▲", 2: "✗"}[s]
        lines.append(f"  {mark} {name}: {detail}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trading-outcome healthcheck (alert-only)")
    ap.add_argument("--quiet", action="store_true", help="print nothing on OK")
    args = ap.parse_args()
    severity, checks, _ = evaluate()
    if not (args.quiet and severity == 0):
        label = {0: "  OK", 1: "WARN", 2: "CRIT"}
        print(f"HEALTH: {SEV_NAME[severity]}")
        for name, s, detail in checks:
            print(f"  [{label[s]}] {name}: {detail}")
    return severity


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        # A crash here would otherwise exit 1 (== DEGRADED) and be masked by run-cycle.sh's
        # `|| true`. Self-report so a broken healthcheck is never silent; exit 3 (distinct).
        import traceback
        traceback.print_exc()
        try:
            from trader.notify import alert
            alert(f"healthcheck CRASHED: {type(e).__name__}: {str(e)[:200]}", key="healthcheck_crash")
        except Exception:
            pass
        sys.exit(3)

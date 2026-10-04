#!/usr/bin/env bash
# Sync-only live-book job — reads Kalshi, books settlements, feeds the $ kill-switch. Places NO orders.
#
# Why this exists: the LIVE trading cron runs only 6×/day (`20 9,11,13,15,17,19` LA), so the live book
# is never synced overnight (~14h). A same-day bin posted in the evening that settles before morning was
# invisible → its settlement was never booked (traced 2026-06-30: KXLOWTSFO-26JUN27-B57.5 lived entirely
# in that gap). This runs via launchd (hourly at :00), INDEPENDENT of OpenClaw's cron/gateway, so the
# book stays reconciled around the clock. Args pass through — use `--report` for a read-only dry run.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/launchd-sync.log"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ── Cycle-cadence watchdog (2026-07-03) ─────────────────────────────────────────────────────────
# The OpenClaw cron agent-turn sometimes reports status=ok WITHOUT ever invoking
# run-cycle-detached.sh (5 silent no-starts on 7/1, 1 on 7/2 — all on the deepseek fallback).
# That mode is invisible to cron.failureAlert (the turn said ok) AND to bin/healthcheck.py (it
# runs INSIDE run-cycle.sh, so it never executes when the cycle never starts). This launchd job
# is the only scheduled path independent of the OpenClaw gateway, so the expected-vs-actual
# cycle-start check lives here. Deliberately OUTSIDE the .cycle.lock critical section below: it
# only READS logs/cycle-*.log (never the book), must not stretch the sync's lock hold, and must
# still run when a trading cycle holds the lock (a held lock = a cycle that STARTED — healthy).
# Overnight (22:00→06:00 PT) it expects zero slots, so the :10/:40 runs stay silent. Alerts via
# trader.notify key=cycle_cadence_gap (2h dedup); `|| true` — the watchdog never blocks the sync.
python3 bin/check_cycle_cadence.py >>"$LOG" 2>&1 || true

# ── Operator-alert-channel watchdog (2026-07-08) ─────────────────────────────────────────────────
# The alert path (trader/notify.py → `openclaw message send`) needs gateway.remote.token to match
# the server token (OPENCLAW_GATEWAY_TOKEN in ~/.openclaw/.env). A cowork/gateway restart rotated
# the server token on 2026-07-07 and left remote.token stale → every alert failed for ~8h with the
# operator blind. This check is gateway-INDEPENDENT (compares the tokens directly + scans
# alerts.jsonl) and escalates out-of-band (state/notify-channel-health.json, read by healthcheck;
# a loud sentinel; osascript/logger). Same rationale as the cadence watchdog: it lives here because
# the launchd sync job is the only scheduled path that does not depend on the gateway. Read-only on
# openclaw.json/.env; `|| true` — never blocks the sync.
python3 bin/notify_selfcheck.py >>"$LOG" 2>&1 || true

# ── Nightly deterministic git backup (2026-07-10) ───────────────────────────────────────────────
# Replaces the LLM-driven "Workspace Nightly Backup" / "Config Nightly Backup" openclaw crons:
# they ran a model turn to do pure git mechanics and died SILENTLY 2026-07-03→10 when every
# fallback provider hit billing/quota — a week of trader state went unbacked. Once per LOCAL day
# PER REPO, retried each :10/:40 slot until that repo pushes (offline ⇒ retry, not skip);
# failures are loud (state/backup-health.json → healthcheck check 10, + trader.notify). Lives
# here for the same reason as the two watchdogs above: launchd is the only scheduled path
# independent of the OpenClaw gateway. BACKGROUNDED — launchd runs this label single-instance, so
# a git push hung on a dead network would otherwise stall THIS slot's book sync and suppress
# every later :10/:40 slot (the exact overnight blindness this job exists to prevent). The script
# self-locks against overlapping slots and time-bounds its pushes; it must stay OUTSIDE the
# .cycle.lock section (a backup snapshot tolerates concurrent state writes).
( bash bin/backup_workspace.sh >>"$LOG" 2>&1 & )

# ── Triangulation snapshot for gateway/sandbox readers (2026-07-12) ──────────────────────────────
# Cowork/sandbox sessions can't run triangulation Legs A/B (no kalshi-cli, no signed HTTP), so
# operator status checks from there always reported ZERO/ERROR — a permanent false alarm that
# buried the real transport-lie signal the tool exists to catch. Each :10/:40 slot snapshots a
# full three-leg read to state/live-premium/triangulation-snapshot.json (atomic);
# bin/triangulate_balance.py auto-falls back to a FRESH (≤75m) snapshot when kalshi-cli is absent
# and exits with the snapshot's severity. BACKGROUNDED like the backup above (relies on the
# plist's AbandonProcessGroup=true, 2026-07-12) so a hung Kalshi read can never stall the book
# sync. Read-only on Kalshi (GETs on the reads host only); stays OUTSIDE the .cycle.lock (reads
# the book file once; tolerates a concurrent atomic state write).
( python3 bin/triangulate_balance.py --dir live-premium --write-snapshot >>"$LOG" 2>&1 & )

# Share run-cycle.sh's lock so a sync never overlaps a trading cycle. Each state FILE is written
# atomically (tmp + os.replace, QA-19), but both processes do read-modify-write on the same book and
# write book/sync-state as a non-transactional PAIR — an overlap can lose updates or tear the pair.
# Scheduled at :00 vs trading at :20 so they don't collide; the lock is the safety net if they ever
# do. A dead owner's stale lock is reclaimed (kill -0 check), like run-cycle.sh.
LOCKDIR="$ROOT/logs/.cycle.lock"
# Max-age reclaim mirrors run-cycle.sh (audit 2026-07-06): without it an empty-pid
# crash-window lock or a hung-but-alive holder wedges BOTH sync and trading forever.
LOCK_MAX_AGE_MIN="${KALSHI_WEATHER_LOCK_MAX_AGE_MIN:-30}"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  _owner="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  _too_old="$(find "$LOCKDIR" -maxdepth 0 -mmin +"${LOCK_MAX_AGE_MIN}" 2>/dev/null || true)"
  if [ -n "${_too_old}" ]; then
    echo "$(ts) sync: reclaiming lock older than ${LOCK_MAX_AGE_MIN}m (pid ${_owner:-unknown}) — prior cycle wedged" >>"$LOG"
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" 2>/dev/null || { echo "$(ts) sync skip: lock race lost" >>"$LOG"; exit 0; }
  elif [ -z "${_owner}" ] || kill -0 "${_owner}" 2>/dev/null; then
    echo "$(ts) sync skip: a cycle holds the lock (pid ${_owner:-acquiring})" >>"$LOG"; exit 0
  else
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" 2>/dev/null || { echo "$(ts) sync skip: lock race lost" >>"$LOG"; exit 0; }
  fi
fi
echo "$$" >"$LOCKDIR/pid"
# Ownership-aware release: don't steal back a lock a later cycle reclaimed from us.
trap '[ "$(cat "$LOCKDIR/pid" 2>/dev/null || true)" = "$$" ] && rm -rf "$LOCKDIR"' EXIT

# Match the premium-live arm's state dir + risk env so the daily/weekly $ kill-switch counters that the
# sync feeds stay consistent with what the trading cycle uses.
export KALSHI_WEATHER_STATE_DIR="$ROOT/state/live-premium"
export KALSHI_WEATHER_MAX_POSITION_DOLLARS=5
export KALSHI_WEATHER_DAILY_MAX_LOSS_DOLLARS=20
export KALSHI_WEATHER_WEEKLY_MAX_LOSS_DOLLARS=60
export KALSHI_RESEARCH_MODE_ALLOW_RED=1

echo "=== $(ts) sync-only start ===" >>"$LOG"
python3 bin/sync_live_positions.py "$@" >>"$LOG" 2>&1
rc=$?
echo "=== $(ts) sync-only end rc=${rc} ===" >>"$LOG"

# ── ROADMAP monthly-rain KILL SCREEN (2026-07-14) ────────────────────────────────────────────────
# Zero-capital forward data logger for the KXRAIN*M markets — the ONLY residual weather probe after the
# catalog scan found the venue picked clean (see ROADMAP.md). --once-daily makes this hourly job write at
# most one frozen snapshot/day; backgrounded + `|| true` so a slow Kalshi/ACIS/Open-Meteo read can never
# stall or fail the book sync. Read-only (no orders). Read out with bin/rain_screen_score.py once months
# settle; expected outcome: KILL in ~12 months. Remove this block to stop the screen.
( python3 bin/rain_screen_snapshot.py --once-daily >>"$LOG" 2>&1 || true & )

exit "${rc}"

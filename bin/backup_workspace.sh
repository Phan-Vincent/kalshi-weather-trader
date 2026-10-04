#!/usr/bin/env bash
# Deterministic nightly git backup for the workspace + config repos — replaces the LLM-driven
# "Workspace Nightly Backup" / "Config Nightly Backup" openclaw crons. Those ran a MODEL turn to
# do pure git mechanics and died SILENTLY 2026-07-03→10 when every fallback provider hit
# billing/quota (moonshot suspended, yunwu-deepseek 403) — a week of trader state went unbacked.
# No agent belongs in this loop.
#
# Contract (hardened per adversarial review 2026-07-10): once per LOCAL day PER REPO (fires just
# after local midnight; a persistently-failing repo keeps retrying each :10/:40 slot while the
# healthy repo stays at one commit/day); the caller backgrounds this script and it self-locks, so
# a hung network can never delay the position sync; every push is time-bounded (low-speed abort);
# staged content passes the content secret-scan (automations/backup/secret-scan.sh — born from
# the real 2026-06 .env leak; filename ignores can't catch pasted tokens) or the backup ABORTS
# loudly; a repo not on its expected branch fails loud instead of silently backing up a side
# branch. Failures surface via state/backup-health.json (healthcheck check 10) + trader.notify
# (6h dedup — the healthcheck WARN persists in every cycle's HEALTH block regardless).
# Secret safety by construction: ~/.openclaw/.gitignore is ALLOWLIST (deny /*, re-include only
# openclaw.json + cron/jobs.json); the workspace repo blocklists *token*/*key*/etc AND gets the
# content scan above.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export GIT_TERMINAL_PROMPT=0
ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
SCAN="$HOME/.openclaw/workspace/automations/backup/secret-scan.sh"
HEALTH="$ROOT/state/backup-health.json"
TODAY="$(date +%Y-%m-%d)"
STAMP_WS="$ROOT/state/.backup-last-success-day.workspace"
STAMP_CFG="$ROOT/state/.backup-last-success-day.config"

# Both repos already succeeded today → nothing to do (and don't touch the health file, so its
# mtime cadence stays "once per successful day / every slot while failing").
[ "$(cat "$STAMP_WS" 2>/dev/null || true)" = "$TODAY" ] \
  && [ "$(cat "$STAMP_CFG" 2>/dev/null || true)" = "$TODAY" ] && exit 0

# Self-lock: the caller backgrounds us, so overlapping :10/:40 slots must not run concurrent git
# ops on the same repos. Stale locks (>30m — far beyond the bounded push) are reclaimed.
LOCK="$ROOT/state/.backup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rm -rf "$LOCK"' EXIT

fail_detail=""

backup_repo() {  # $1=repo path  $2=expected branch  $3=commit message  $4=stamp file
  local repo="$1" branch="$2" msg="$3" stamp="$4" out rc br
  [ "$(cat "$stamp" 2>/dev/null || true)" = "$TODAY" ] && return 0

  br="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [ "$br" != "$branch" ]; then
    fail_detail="${fail_detail}$repo: not on $branch (on '${br:-detached}') — backup skipped; "
    return 1
  fi

  out="$(git -C "$repo" add -A 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && [[ "$out" == *index.lock* ]]; then
    sleep 2  # transient contention with a manual git command — absorb one race
    out="$(git -C "$repo" add -A 2>&1)"; rc=$?
  fi
  [ "$rc" -eq 0 ] || { fail_detail="${fail_detail}add failed ($repo): ${out:0:150}; "; return 1; }

  if ! (cd "$repo" && bash "$SCAN"); then
    git -C "$repo" reset -q
    fail_detail="${fail_detail}$repo: SECRET SCAN flagged staged content — backup blocked; "
    return 1
  fi

  if ! git -C "$repo" diff --cached --quiet; then
    out="$(git -C "$repo" commit -q -m "$msg" 2>&1)" \
      || { fail_detail="${fail_detail}commit failed ($repo): ${out:0:150}; "; return 1; }
  fi
  # Push even with no new commit today — yesterday's commit may still be unpushed (offline).
  # Low-speed abort bounds a stalled transfer; GIT_TERMINAL_PROMPT=0 kills any credential prompt.
  out="$(git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 -C "$repo" push 2>&1)" \
    || { fail_detail="${fail_detail}push failed ($repo): ${out:0:150}; "; return 1; }

  echo "$TODAY" >"$stamp"
  return 0
}

ok=1
backup_repo "$HOME/.openclaw/workspace" main "auto-backup $TODAY" "$STAMP_WS" || ok=0
backup_repo "$HOME/.openclaw" main "config backup $TODAY" "$STAMP_CFG" || ok=0

if [ "$ok" -eq 1 ]; then
  status="ok"; detail="backed up + pushed (workspace, config) $TODAY"
  echo "backup: OK ($TODAY)"
else
  status="fail"; detail="git backup: ${fail_detail%; }"
  echo "backup: FAIL — ${fail_detail%; }"
fi

python3 - "$status" "$detail" "$HEALTH" <<'PY' || true
import datetime, json, os, sys
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rec = {"status": sys.argv[1], "severity": 0 if sys.argv[1] == "ok" else 1,
       "detail": sys.argv[2][:300], "ts_utc": now}
tmp = sys.argv[3] + ".tmp"
with open(tmp, "w") as f:
    json.dump(rec, f)
os.replace(tmp, sys.argv[3])
if sys.argv[1] != "ok":
    sys.path.insert(0, os.path.expanduser("~/.openclaw/workspace/automations/kalshi-weather"))
    from trader.notify import alert
    alert("nightly git backup FAILED: " + sys.argv[2][:200] + " — retrying each :10/:40 slot",
          key="backup_failed", dedup_seconds=21600)
PY

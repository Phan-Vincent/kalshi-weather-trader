#!/usr/bin/env bash
# Kalshi trade-cycle SUPERVISOR (read-only). Fires the headless `claude -p` CLI at :30
# after each :20 LIVE slot to verify the launchd cycle actually traded, report P&L, and
# alert on anomalies via the trader's Telegram notifier.
#
# WHY: the old OpenClaw agent-turn reporter (deepseek/kimi) repeatedly finished
# status=ok having only READ logs (it summarized the :00 PAPER cycle as the LIVE run) —
# 8 silent no-ops. This runs deterministic CLI code (the script ALWAYS runs; a model
# does not choose whether to act) and the reporter is genuinely Claude. Same launchd
# pattern the trader's LIVE executor already uses, so it's gateway/GUI-independent.
#
# READ-ONLY posture is ENFORCED, not just asked for (hardening 2026-07-09): --settings points
# at bin/supervise-settings.json = defaultMode "dontAsk" (deny-by-default in headless) + a scoped
# Bash allow-list (read utils + the specific read-only reporters + notify.py only; halt_live.py
# allowed no-arg = status read, denied with any flag) + an explicit deny-list + a PreToolUse hook
# (bin/supervise-readonly-gate.py) that blocks a file-write redirect or any mutation token hidden
# in $(...)/compound chains. So even a prompt-injection from the logs it reads cannot place a
# trade, halt, flatten, or overwrite state. The prompt (bin/supervise-prompt.md) still forbids
# mutating actions as the first line of defense. LIVE is never set here. It runs at :30 — off the
# :00 paper / :10-:40 sync / :20 live cadence — so it never contends for logs/.cycle.lock.
#
# INSTALL: copy this to <trader>/bin/supervise.sh, chmod +x, then load the plist.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# launchd has a bare PATH; claude lives in ~/.local/bin, kalshi-cli/python3 in homebrew.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$ROOT" || exit 1

# Headless auth for `claude -p`: launchd does NOT inherit the interactive Claude Code login
# (macOS Keychain OAuth), and a re-login/reboot rotated that credential 2026-07-11T12:22Z ->
# every supervisor run 401'd ("Invalid authentication credentials") from 2026-07-11T16:32Z on.
# Fix: source a long-lived automation token (`claude setup-token`) from a 0600 file kept OUT of
# git/backups (same posture as ~/.openclaw/server-token). Guarded: absent/empty file => behavior
# unchanged, never errors under `set -u`. The token itself never appears in this script or in git.
CLAUDE_TOKEN_FILE="$HOME/.openclaw/claude-code-oauth-token"
if [ -s "$CLAUDE_TOKEN_FILE" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CLAUDE_TOKEN_FILE")"
  # $(cat) strips trailing newlines but not a CR (CRLF file) or internal newline — a stray \r in
  # the Authorization header re-triggers the exact 401 this fixes. Strip all CR/LF defensively.
  CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN//$'\r'/}"
  CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN//$'\n'/}"
  export CLAUDE_CODE_OAUTH_TOKEN
fi

LOG="$ROOT/logs/supervisor-$(date +%F).log"
mkdir -p "$ROOT/logs"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor start ==="
  # Durable tripwire (2026-07-10 quote-refresh live flip): alert ONCE the first time the live
  # cancel/DELETE leg fires in prod (first `refresh_cancelled` in the live book), and on any new
  # refresh_list_failed. Deterministic + sentinel-gated; runs regardless of the claude -p reporter
  # below and outside its read-only sandbox, so it can send the notify.py alert. Never fails the run.
  python3 "$ROOT/bin/refresh_deleteleg_watch.py" || true
  claude -p "$(cat "$ROOT/bin/supervise-prompt.md")" \
    --model sonnet \
    --settings "$ROOT/bin/supervise-settings.json" \
    --output-format text
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor end (rc=$?) ==="
} >> "$LOG" 2>&1

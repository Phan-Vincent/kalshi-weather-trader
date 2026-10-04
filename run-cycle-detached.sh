#!/usr/bin/env bash
# run-cycle-detached.sh — decouple the trading cycle from the kimi agentTurn cron.
#
# WHY: the cron jobs (jobs.json: kind=agentTurn, model=moonshot/kimi-k2.6) run an LLM
# turn that invokes run-cycle.sh as a tool call. When a model call times out (run
# records: "execution timed out (last phase: model-call-started)") the agent turn is
# torn down, killing run-cycle.sh mid-flight → the cycle reaches 0 orders and the EXIT
# trap fires a (false) cycle_fail. The 3600s job timeout is NOT the limiter (healthy
# cycles finish in ~5-9 min); the model-call timeout is.
#
# FIX: launch run-cycle.sh in its OWN session so a teardown of the agent's process group
# cannot kill it. macOS has NO `setsid`, so we use Python's start_new_session=True
# (calls setsid(2) in the child) — portable across darwin+linux, and python3 is the
# trader's runtime so it is always present. run-cycle.sh's mkdir lock prevents overlap
# and it tees its own log, so we discard the child's fds here. We then best-effort wait
# so the agent can still report fills; if the agent turn is killed during that wait, the
# detached cycle keeps running to completion regardless. Execution is guaranteed; Slack
# reporting is best-effort. LIVE=1 (and any other env) is inherited via os.environ.
set -uo pipefail

ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
DATE=$(date +%Y-%m-%d)
LOG="$ROOT/logs/cycle-${DATE}.log"
mkdir -p "$ROOT/logs"

# Spawn run-cycle.sh in a new session, detached from this (agent-turn) process group.
child=$(python3 - "$ROOT/run-cycle.sh" <<'PY'
import sys, subprocess
script = sys.argv[1]
p = subprocess.Popen(
    ["/usr/bin/env", "bash", script],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True,   # setsid(2) in the child — survives caller teardown
)
print(p.pid)
PY
)
echo "run-cycle launched detached (pid ${child}, LIVE=${LIVE:-0}) -> ${LOG}"

# Bounded best-effort wait so the agent turn can tail/report fills. If the agent is
# killed here, run-cycle.sh (a separate session) is unaffected and finishes the cycle.
wait_budget="${CYCLE_WAIT_SECS:-900}"
deadline=$(( $(date +%s) + wait_budget ))
while kill -0 "${child}" 2>/dev/null; do
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo "run-cycle still running after ${wait_budget}s wait budget; returning (cycle continues detached)"
    exit 0
  fi
  sleep 5
done

echo "run-cycle finished; last 20 log lines:"
tail -20 "${LOG}" 2>/dev/null || true

# acquire_cycle_lock.sh — acquire the single-cycle lock, or exit the caller if another
# cycle already holds it. MUST BE SOURCED (not executed): it uses the caller's $$ so the
# caller's ownership-aware EXIT trap releases the right lock, and its `exit 0` on a skip
# is meant to exit run-cycle.sh.
#
# Behavior preserved verbatim from the old inline block (run-cycle.sh, pre-2026-07-08):
# max-age reclaim of a wedged holder, empty-pid = still-acquiring = alive, stale (dead
# pid) reclaim. Added 2026-07-08 after root-causing the 09:20 PDT live-slot starvation
# (a stalled :00 paper cycle held logs/.cycle.lock; the :20 launchd-LIVE launch hit the
# "owner alive" branch and exited 0 to stderr->/dev/null — real orders dropped SILENTLY):
#   (1) MODE file: each holder records live|paper so a live cycle can tell them apart.
#   (2) LIVE PRIORITY: a live cycle whose slot is starved by a PAPER holder preempts it
#       (kills its process group; paper state writers are atomic tmp+rename so a mid-write
#       kill cannot corrupt state) and proceeds — so a stalled paper cycle can never again
#       silently starve the live slot. A live cycle NEVER preempts another LIVE holder
#       (two live cycles must not run) — it yields; nor an unknown-mode holder.
#   (3) LOUD ALERT: a lock-skipped LIVE cycle notifies (was a silent stderr exit) — EXCEPT when the
#       holder is a healthy LIVE cycle. That is the benign, expected launchd-live + redundant
#       live-cron (a5405e51) contention on the same :20 slot: the holder is trading the slot, so the
#       skip is a no-op, not a starved slot. Only a paper/unknown/dead holder pages (2026-07-10 fix).
#   (4) MAX-AGE RECLAIM KILLS the wedged holder (2026-07-10, OM-trickle incident): reclaim-without-
#       kill orphaned hung cycles that could wake later and run concurrently with the new owner
#       (two-live-cycles invariant violation). Guarded against pid recycling: only kills a pid whose
#       command matches KALSHI_WEATHER_LOCK_KILL_MATCH (default "run-cycle"); else reclaim-only.
#
# Requires exported by the caller: ROOT, LOCKDIR, LOCK_MAX_AGE_MIN. Reads LIVE (default 0).
# Knobs: KALSHI_WEATHER_LIVE_PREEMPT (default 1), KALSHI_WEATHER_NOTIFY_CMD (test override).

_self_mode="paper"; [ "${LIVE:-0}" = "1" ] && _self_mode="live"

_lock_notify() {   # $1=message $2=dedup-key ; overridable for tests, always fail-soft
  local _cmd="${KALSHI_WEATHER_NOTIFY_CMD:-}"
  if [ -n "$_cmd" ]; then
    $_cmd "$1" --key "$2" >/dev/null 2>&1 || true
  else
    python3 "$ROOT/bin/notify.py" "$1" --key "$2" >/dev/null 2>&1 || true
  fi
}

_kill_owner_group() {   # $1=pid ; TERM its process group (fallback per-pid), escalate to KILL. Fail-soft.
  # Shared by the live-preempt and max-age-reclaim paths. `ps` MUST end in `|| true`: it is a
  # command-substitution assignment under set -euo pipefail, and the owner can exit right after
  # the caller's kill-0 (a slow cycle finishing) → ps fails → pipefail → set -e would abort the
  # sourcing shell BEFORE run-cycle.sh's EXIT trap is installed, silently dropping the slot
  # (2026-07-08 final-QA regression). An empty _pgid falls through to the per-pid kill.
  _pgid="$(ps -o pgid= -p "$1" 2>/dev/null | tr -d ' ' || true)"
  if [ -n "$_pgid" ]; then kill -TERM "-${_pgid}" 2>/dev/null || true; else kill -TERM "$1" 2>/dev/null || true; fi
  _t=0
  while kill -0 "$1" 2>/dev/null && [ "$_t" -lt 5 ]; do sleep 1; _t=$((_t + 1)); done
  if kill -0 "$1" 2>/dev/null; then
    if [ -n "$_pgid" ]; then kill -KILL "-${_pgid}" 2>/dev/null || true; else kill -KILL "$1" 2>/dev/null || true; fi
    sleep 1
  fi
}

_take_over_lock() {   # atomically rebuild $LOCKDIR after a reclaim/preempt decision, serialized so two
  # reclaimers do not both proceed. The old `rm -rf "$LOCKDIR"; mkdir "$LOCKDIR"` was NOT atomic: two
  # interleaved reclaimers (or a slow one that woke after another already rebuilt a fresh lock) both
  # recreated the dir and ran concurrently → two-live-cycles invariant violation → duplicate live
  # orders / correlated-side-cap bypass (both cycles seed the cap from the same pre-run snapshot and
  # never see each other's placements). Election primitive = a single atomic `mkdir` MARKER (only its
  # creator ever removes it — see the marker block below for why no orphan auto-clear); under it we
  # re-check that a competitor hasn't ALREADY (re)acquired the lock — a rebuilt fresh dir OR a fresh dir
  # whose pid is not yet published (destroying either is the very double-run we're preventing) — then
  # destroy+recreate and PUBLISH our pid before releasing the marker so any later reclaimer's freshness
  # re-check sees us and yields. Losing the marker or the final mkdir → skip this cycle (exit 0) — the
  # SAFE direction, identical to the old lost-race exit. Fail-soft throughout to survive `set -euo
  # pipefail` in the sourcing shell.
  # $1 = the owner pid we already handled on this path (killed, or confirmed dead/wedged). The
  # freshness re-check below must NOT treat THAT pid as a live competitor: a just-TERMed holder is a
  # zombie until its parent reaps it, and `kill -0 <zombie>` still succeeds — so re-checking its
  # liveness would wrongly yield the lock we are entitled to take. We yield only to a DIFFERENT pid
  # that has already rebuilt a fresh lock (a competing reclaimer that won just before us).
  local _prev="${1:-}"
  local _m="${LOCKDIR}.reclaiming"
  # Win the reclaim election with a SINGLE atomic `mkdir` marker — the only mutex here. `mkdir` fails if
  # the dir exists, so exactly one concurrent reclaimer wins; the rest yield (the SAFE direction, same as
  # the old lost-race exit). Crucially, the marker is removed ONLY by its own creator (the `rmdir` at the
  # end of this function). Nothing ever removes/renames a marker it does not own — deliberately: every
  # non-owner "clear the orphan" scheme (find-then-rm, rename-capture) has a check-then-act TOCTOU under
  # concurrent reclaimers that can let two both rebuild the lock. So we accept ONE bounded residual: a
  # reclaimer SIGKILLed/power-lost in the ~sub-second window while holding this marker orphans it, and
  # then wedged-lock RECLAIMS pause until it is cleared (`rm -rf ${LOCKDIR}.reclaiming`). That is
  # strictly safe (never a double live cycle), self-limited (normal cycles that need no reclaim are
  # unaffected — the marker only gates reclaim), and detectable (healthcheck cycle_liveness). It trades
  # an unrecoverable-money failure (duplicate live orders) for a recoverable-availability one.
  mkdir "$_m" 2>/dev/null || { echo "run-cycle: another cycle is reclaiming the lock; exiting" >&2; exit 0; }
  # YIELD if $LOCKDIR is now held or being-acquired by someone OTHER than us or the holder we just
  # handled (_prev): a competing reclaimer that already rebuilt a fresh lock, OR a plain-mkdir acquirer
  # mid-publish whose pid file is not written yet (empty _cur on a FRESH dir — the old `-n "$_cur"`
  # guard wrongly clobbered it). A STALE dir ($LOCKDIR older than max age) is the wedged lock we came
  # to reclaim, so it never yields. _prev (the holder we killed/confirmed-dead) may be a zombie whose
  # pid still passes kill -0, so its pid is explicitly excluded rather than treated as a live rival.
  local _cur; _cur="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -d "$LOCKDIR" ] \
     && [ -z "$(find "$LOCKDIR" -maxdepth 0 -mmin +"${LOCK_MAX_AGE_MIN}" 2>/dev/null || true)" ] \
     && [ "$_cur" != "$$" ] \
     && ! { [ -n "$_prev" ] && [ "$_cur" = "$_prev" ]; } \
     && { [ -z "$_cur" ] || kill -0 "$_cur" 2>/dev/null; }; then
    rmdir "$_m" 2>/dev/null || true
    echo "run-cycle: lock already (re)acquired by another cycle (pid ${_cur:-acquiring}); exiting" >&2
    exit 0
  fi
  rm -rf "$LOCKDIR"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    rmdir "$_m" 2>/dev/null || true
    echo "run-cycle: lock race lost; exiting" >&2
    exit 0
  fi
  echo "$$" > "$LOCKDIR/pid"    # publish ownership BEFORE releasing the marker (pid/mode are rewritten
                                 # canonically at the end of acquisition; a later reclaimer's freshness
                                 # re-check keys on this early publish to yield instead of clobbering us)
  rmdir "$_m" 2>/dev/null || true
}

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  _owner="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  _owner_mode="$(cat "$LOCKDIR/mode" 2>/dev/null || true)"
  _too_old="$(find "$LOCKDIR" -maxdepth 0 -mmin +"${LOCK_MAX_AGE_MIN}" 2>/dev/null || true)"
  if [ -n "$_too_old" ]; then
    # Older than the max age → wedged/crashed holder (covers the empty-pid crash window
    # and a hung-but-alive owner). Reclaim regardless of pid state — and KILL a still-alive
    # holder (2026-07-10): reclaim-without-kill left wedged cycles running ORPHANED. The
    # 2026-07-10 Open-Meteo trickle hangs showed why that is dangerous, not just untidy: an
    # orphaned holder can WAKE LATER (the blocked read finally returning) and keep executing
    # its cycle body concurrently with the new lock owner — for a LIVE holder that violates
    # the two-live-cycles invariant outright, and its stale build could trade old signals.
    # GUARD against pid recycling (a >LOCK_MAX_AGE_MIN-old pid may now be an innocent
    # process): only kill when the pid's command still looks like a trading cycle
    # (KALSHI_WEATHER_LOCK_KILL_MATCH, default "run-cycle"); unmatched → reclaim-only (the
    # pre-2026-07-10 behavior). The TERMed holder's own EXIT trap fires (quiet
    # cycle_interrupted key) — same accepted pattern as the live-preempt kill.
    echo "run-cycle: reclaiming lock older than ${LOCK_MAX_AGE_MIN}m (pid ${_owner:-unknown}) — prior cycle wedged" >&2
    _killed=""
    if [ -n "$_owner" ] && [ "$_owner" != "$$" ] && kill -0 "$_owner" 2>/dev/null; then
      _owner_cmd="$(ps -o command= -p "$_owner" 2>/dev/null || true)"
      case "$_owner_cmd" in
        *"${KALSHI_WEATHER_LOCK_KILL_MATCH:-run-cycle}"*)
          _kill_owner_group "$_owner"
          _killed=" (hung holder KILLED — it could otherwise wake later and run concurrently)"
          echo "run-cycle: killed wedged holder pid ${_owner}" >&2
          ;;
        *)
          echo "run-cycle: wedged pid ${_owner} command does not match '${KALSHI_WEATHER_LOCK_KILL_MATCH:-run-cycle}' (pid recycled?) — reclaiming without kill" >&2
          ;;
      esac
    fi
    _lock_notify "run-cycle: reclaimed a lock older than ${LOCK_MAX_AGE_MIN}m (pid ${_owner:-unknown})${_killed} — a prior cycle wedged/crashed holding it" cycle_lock_wedged
    _take_over_lock "$_owner"
  elif [ -z "$_owner" ] || kill -0 "$_owner" 2>/dev/null; then
    # Owner alive (or acquiring: empty pid, bounded by the max-age check above).
    if [ "$_self_mode" = "live" ] && [ "${KALSHI_WEATHER_LIVE_PREEMPT:-1}" = "1" ] \
       && [ "$_owner_mode" = "paper" ] && [ -n "$_owner" ] && [ "$_owner" != "$$" ] \
       && kill -0 "$_owner" 2>/dev/null; then
      # LIVE PRIORITY: a paper cycle is starving this live slot → preempt it.
      echo "run-cycle: LIVE preempting paper holder (pid ${_owner}) that was starving the live slot" >&2
      _kill_owner_group "$_owner"
      _take_over_lock "$_owner"
      # Only NOW (lock actually held) alert — a notify before this can lie ("orders proceed") if the
      # preempt/reclaim then fails, and its network delay is what let the holder die mid-preempt above.
      _lock_notify "run-cycle: LIVE cycle preempted a paper holder (pid ${_owner}) that held the cycle lock at a live slot — live orders proceed" cycle_live_preempt
    else
      # Not preempting → exit. A silent skip is fine for a paper overlap, but a LIVE slot
      # dropping real orders behind a held lock must be LOUD (root cause 2026-07-08) — UNLESS the
      # holder is another healthy LIVE cycle. That case is BENIGN and expected: the launchd -live
      # plist and the redundant OpenClaw live cron (a5405e51, kept enabled as a fallback) both fire
      # the same :20 slot, so one wins the lock and TRADES while the other skips. A live holder means
      # the slot IS being traded — paging cycle_live_lockskip there is a false alarm (2026-07-10: it
      # fired on ~every :20 slot). Stay LOUD only for a paper/unknown/dead holder (the live orders may
      # actually have been dropped). Paper-starves-live is normally caught by the preempt block above;
      # it reaches here only with preempt disabled, and then it MUST page.
      echo "run-cycle: another cycle holds the lock (pid ${_owner:-acquiring}, mode ${_owner_mode:-unknown}); exiting" >&2
      if [ "$_self_mode" = "live" ]; then
        if [ "$_owner_mode" = "live" ] && [ -n "$_owner" ] && kill -0 "$_owner" 2>/dev/null; then
          echo "run-cycle: LIVE slot already held by a healthy live cycle (pid ${_owner}) — redundant :20 trigger, benign no-op (not paged)" >&2
        else
          _lock_notify "run-cycle: LIVE slot LOCK-SKIPPED — held by ${_owner_mode:-unknown} cycle (pid ${_owner:-acquiring}); premium-live placed NO orders this slot. Check for a stalled cycle holding logs/.cycle.lock." cycle_live_lockskip
        fi
      fi
      exit 0
    fi
  else
    echo "run-cycle: reclaiming stale lock (pid ${_owner} not running)" >&2
    _take_over_lock "$_owner"
  fi
fi
# INVARIANT: these two writes are the ONLY writes into $LOCKDIR, and they happen ONCE here at
# acquisition — so the lockdir mtime == cycle-start time. The max-age reclaim above depends on that.
# Do NOT add a heartbeat/touch of $LOCKDIR during the cycle: it would refresh the mtime, defeat the
# 30m reclaim, and (now that a healthy live holder is skipped SILENTLY, 2026-07-10) turn a hung live
# holder into a silent live-slot drop with no backstop. If a heartbeat is ever needed, bound it well
# under LOCK_MAX_AGE_MIN and re-key the skip alert off staleness, not just owner_mode.
echo "$$" > "$LOCKDIR/pid"
printf '%s' "$_self_mode" > "$LOCKDIR/mode"

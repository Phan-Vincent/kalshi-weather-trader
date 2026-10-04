#!/usr/bin/env python3
"""Tests for bin/acquire_cycle_lock.sh — the extracted single-cycle lock acquisition.

Locks the behavior that fixes the 2026-07-08 09:20 PDT live-slot STARVATION (a stalled :00
paper cycle held logs/.cycle.lock; the :20 launchd-LIVE launch hit the "owner alive" branch
and silently exit-0'd → real orders dropped with no alert):
  • a LIVE cycle PREEMPTS a paper holder (kills it, reclaims, acquires);
  • a LIVE cycle NEVER preempts another LIVE holder — it yields SILENTLY (a healthy live holder is
    trading the slot; this is the intended launchd-live + redundant live-cron :20 contention);
  • but a LIVE cycle yielding to an unknown/paper/dead holder DOES page (the slot may be untraded);
  • a paper cycle still skips silently on any overlap (no behavior change, no alert);
  • KALSHI_WEATHER_LIVE_PREEMPT=0 disables preemption but keeps the loud alert (paper holder);
  • max-age reclaim KILLS a wedged-but-alive holder whose command matches "run-cycle"
    (2026-07-10 OM-trickle incident: orphaned holders can wake and run concurrently), but
    NOT an unmatched pid (recycling guard);
plus regression coverage that the PRESERVED reclaim paths (stale dead-pid, max-age) still work.

The helper is SOURCED (`. acquire_cycle_lock.sh`); on a skip it `exit 0`s the sourcing shell,
so "acquired" == the trailing __ACQUIRED__ marker printed. notify is redirected to a log file
(no real alerts); holders run in their OWN session (start_new_session) so the preemption's
process-group kill targets only the holder, never the test runner. Liveness is checked via the
Popen handle (wait/poll) — NOT os.kill(pid,0), which can't tell a reaped-dead pid from a zombie.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACQUIRE = ROOT / "bin" / "acquire_cycle_lock.sh"


@pytest.fixture
def holders():
    procs = []

    def _spawn(argv=None):
        # own session => own process group (pgid == pid); a group-kill can't reach pytest.
        # Default `sleep 120` deliberately does NOT match the max-age kill's command guard
        # (KALSHI_WEATHER_LOCK_KILL_MATCH="run-cycle"); pass argv to spawn a matching holder,
        # e.g. ["bash", "-c", "exec -a run-cycle-wedged sleep 120"] (argv0 rename → ps command
        # contains "run-cycle" like the real holder).
        p = subprocess.Popen(argv or ["sleep", "120"], start_new_session=True)
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def _prime_lock(lockdir: Path, pid: int, mode: str):
    lockdir.mkdir(parents=True, exist_ok=True)
    (lockdir / "pid").write_text(f"{pid}\n")
    (lockdir / "mode").write_text(mode)


def _run(tmp_path, *, live, lockdir, preempt="1", path_prepend=None):
    notify_log = tmp_path / "notify.log"
    fake_notify = tmp_path / "fake_notify.sh"
    fake_notify.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{notify_log}"\n')
    fake_notify.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "ROOT": str(ROOT), "LOCKDIR": str(lockdir), "LOCK_MAX_AGE_MIN": "30",
        "LIVE": "1" if live else "0", "KALSHI_WEATHER_LIVE_PREEMPT": preempt,
        "KALSHI_WEATHER_NOTIFY_CMD": f"bash {fake_notify}",
    })
    if path_prepend:                       # e.g. a stub `ps` that fails, to exercise the guard
        env["PATH"] = f"{path_prepend}:{env['PATH']}"
    # run under the SAME shell options as run-cycle.sh (set -euo pipefail) — that is exactly
    # where an unbound-var / errexit bug in the sourced helper would surface.
    p = subprocess.run(["bash", "-c", f'set -euo pipefail; . "{ACQUIRE}"; echo "__ACQUIRED__:$_self_mode"'],
                       env=env, capture_output=True, text=True, timeout=30)
    acquired = "__ACQUIRED__" in p.stdout
    notify = notify_log.read_text() if notify_log.exists() else ""
    return acquired, notify, p


def _terminated(holder, timeout=8) -> bool:
    """True iff the holder is dead (reaps it via the Popen handle — zombie-safe)."""
    try:
        holder.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


# ── acquisition + mode file ──────────────────────────────────────────

def test_no_contention_acquires_and_writes_mode(tmp_path):
    lockdir = tmp_path / ".cycle.lock"
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir)
    assert acquired
    assert (lockdir / "pid").exists()
    assert (lockdir / "mode").read_text() == "live"
    assert notify == ""


# ── live priority: preempt a paper holder ────────────────────────────

def test_live_preempts_paper_holder(tmp_path, holders):
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "paper")
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir)
    assert acquired, "live cycle must take the lock from a paper holder"
    assert _terminated(holder), "paper holder must be killed"
    assert "cycle_live_preempt" in notify
    assert (lockdir / "mode").read_text() == "live"


def test_live_preempts_even_when_ps_fails_midrace(tmp_path, holders):
    """Regression (final-QA 2026-07-08): the paper holder can exit right after the kill-0 guard
    (a slow cycle finishing) so `ps -o pgid=` fails. That command-substitution runs under
    set -euo pipefail; unguarded it aborted the sourcing shell BEFORE the EXIT trap → silent live
    drop + a false 'orders proceed' alert. The `|| true` guard must fall through to the per-pid kill."""
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "paper")
    stub = tmp_path / "stubbin"
    stub.mkdir()
    (stub / "ps").write_text("#!/usr/bin/env bash\nexit 1\n")   # ps always fails → pgid lookup empties
    (stub / "ps").chmod(0o755)
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir, path_prepend=str(stub))
    assert acquired, "must still acquire when ps fails (fall through to per-pid kill), not abort"
    assert _terminated(holder), "paper holder must still be killed via the per-pid path"
    assert "cycle_live_preempt" in notify


def test_live_yields_to_healthy_live_holder_silently(tmp_path, holders):
    """Two live cycles must never run concurrently, and a skip behind a HEALTHY live holder must be
    a SILENT no-op — not a page. This is the intended launchd -live + redundant OpenClaw live-cron
    (a5405e51) contention on the same :20 slot: the holder is trading the slot, so the skip is benign.
    (2026-07-10: it was falsely paging cycle_live_lockskip on ~every :20 slot.)"""
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "live")
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir)
    assert not acquired, "two live cycles must never run concurrently"
    assert holder.poll() is None, "a live holder must NOT be killed"
    assert "cycle_live_lockskip" not in notify, "a healthy live holder IS trading the slot — do not page"
    assert "cycle_live_preempt" not in notify
    assert notify == "", "benign live-vs-live redundancy must be fully silent"


def test_live_skip_unknown_mode_holder_still_pages(tmp_path, holders):
    """If the holder's mode can't be confirmed as live (missing/unknown mode file), the live slot
    may genuinely have gone untraded — keep the LOUD alert; only a confirmed live holder is silenced."""
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    lockdir.mkdir(parents=True, exist_ok=True)
    (lockdir / "pid").write_text(f"{holder.pid}\n")   # pid only, NO mode file → _owner_mode unknown
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir)
    assert not acquired, "live yields to an unknown-mode holder (never preempts it)"
    assert holder.poll() is None, "an unknown-mode holder must NOT be killed"
    assert "cycle_live_lockskip" in notify, "unknown-mode holder → can't confirm the slot traded → page"
    assert "cycle_live_preempt" not in notify


def test_paper_overlap_skips_silently(tmp_path, holders):
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "paper")
    acquired, notify, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert not acquired
    assert holder.poll() is None, "a paper cycle must not preempt anything"
    assert notify == "", "a benign paper overlap must not alert"


def test_preempt_disabled_live_skips_paper_but_alerts(tmp_path, holders):
    holder = holders()
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "paper")
    acquired, notify, _ = _run(tmp_path, live=True, lockdir=lockdir, preempt="0")
    assert not acquired, "preempt disabled → live yields the lock"
    assert holder.poll() is None, "preempt disabled → holder untouched"
    assert "cycle_live_lockskip" in notify, "loud alert still fires when preempt is off"
    assert "cycle_live_preempt" not in notify


# ── preserved reclaim paths (regression on the extraction) ───────────

def test_stale_dead_pid_is_reclaimed(tmp_path, holders):
    holder = holders()
    dead_pid = holder.pid
    os.killpg(os.getpgid(dead_pid), signal.SIGKILL)
    holder.wait(timeout=5)                    # REAP so it's not a zombie (kill -0 would see a zombie)
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, dead_pid, "paper")
    acquired, _, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert acquired, "a lock owned by a dead pid must be reclaimed"


def test_max_age_reclaim_kills_matching_wedged_holder(tmp_path, holders):
    """2026-07-10 (OM-trickle incident): a wedged-but-alive holder must be KILLED at max-age
    reclaim, not orphaned — an orphan can wake later (its blocked read returning) and run its
    cycle body concurrently with the new lock owner (two-live-cycles invariant violation)."""
    holder = holders(["bash", "-c", "exec -a run-cycle-wedged sleep 120"])  # command matches guard
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "live")
    old = time.time() - 3600
    os.utime(lockdir, (old, old))
    acquired, notify, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert acquired, "a lock older than the max age must be reclaimed even if the owner is alive"
    assert _terminated(holder), "the wedged holder must be killed (it could wake and run concurrently)"
    assert "cycle_lock_wedged" in notify
    assert "KILLED" in notify, "the wedge alert must say the holder was killed"


def test_max_age_reclaim_skips_kill_on_unmatched_command(tmp_path, holders):
    """Pid-recycling guard: a >max-age pid may now belong to an INNOCENT process. If its command
    does not match KALSHI_WEATHER_LOCK_KILL_MATCH ("run-cycle"), reclaim the lock but do NOT
    kill — the pre-2026-07-10 behavior. (The default `sleep 120` holder does not match.)"""
    holder = holders()                        # owner still ALIVE, but the lock is older than max age
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, holder.pid, "paper")
    old = time.time() - 3600
    os.utime(lockdir, (old, old))
    acquired, notify, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert acquired, "a lock older than the max age must be reclaimed even if the owner is alive"
    assert holder.poll() is None, "an unmatched (possibly recycled) pid must NOT be killed"
    assert "cycle_lock_wedged" in notify
    assert "KILLED" not in notify


# ── atomic reclaim: two reclaimers must not BOTH take the lock (2026-07-13) ───

def test_concurrent_reclaim_marker_blocks_second_reclaimer(tmp_path, holders):
    """Two cycles must never BOTH reclaim a wedged/stale lock and then run concurrently — the old
    `rm -rf "$LOCKDIR"; mkdir "$LOCKDIR"` was not atomic and let them. The reclaim is now serialized
    by an atomic mkdir MARKER (${LOCKDIR}.reclaiming): a second reclaimer that finds the marker held
    yields (exit 0) instead of also rebuilding the lock."""
    holder = holders()
    dead_pid = holder.pid
    os.killpg(os.getpgid(dead_pid), signal.SIGKILL)
    holder.wait(timeout=5)                       # reap → stale dead-pid reclaim path
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, dead_pid, "paper")
    marker = tmp_path / ".cycle.lock.reclaiming"
    marker.mkdir()                               # simulate a concurrent reclaimer mid-reclaim
    acquired, _, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert not acquired, "a second reclaimer must yield while another holds the reclaim marker"
    assert marker.exists(), "the yielding cycle must not remove the in-progress reclaimer's marker"


def test_orphaned_reclaim_marker_pauses_reclaim_safely(tmp_path, holders):
    """DELIBERATE tradeoff: the reclaim marker is a pure atomic-mkdir mutex that only its creator
    removes — no non-owner auto-clears it, because every check-then-act orphan-clear (find+rm,
    rename-capture) has a TOCTOU that can let two reclaimers both rebuild the lock (two live cycles).
    The cost is that a marker orphaned by a crash mid-reclaim PAUSES reclaim (a reclaimer yields)
    rather than clobbering it — the SAFE direction (never a double live cycle; normal no-reclaim
    cycles are unaffected; detectable via healthcheck cycle_liveness; cleared with `rm -rf`)."""
    holder = holders()
    dead_pid = holder.pid
    os.killpg(os.getpgid(dead_pid), signal.SIGKILL)
    holder.wait(timeout=5)
    lockdir = tmp_path / ".cycle.lock"
    _prime_lock(lockdir, dead_pid, "paper")
    marker = tmp_path / ".cycle.lock.reclaiming"
    marker.mkdir()
    old = time.time() - 3600                      # orphaned > LOCK_MAX_AGE_MIN (30m), but never auto-cleared
    os.utime(marker, (old, old))
    acquired, _, _ = _run(tmp_path, live=False, lockdir=lockdir)
    assert not acquired, "a reclaim marker (even orphaned) must never be clobbered — reclaim yields safely"

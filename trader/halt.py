"""trader/halt.py — manual GLOBAL live-trading halt (operator kill-switch).

A single sentinel `state/LIVE_HALT.json` at the repo-root state dir. When present,
live arms refuse to open NEW exposure (enforced in run_variants.py + paper_trade.py).
This is the "stop new orders" actuator; bin/flatten_live.py cancels resting orders.

Operator-invoked (bin/halt_live.py) — the healthcheck only RECOMMENDS it, never sets
it. Global on purpose: lives at the repo root state/ (not a per-arm dir) so every
live arm sees the same halt. Best-effort, never raises.

PROVENANCE HARDENING (2026-07-01 'x' incident): on 7/1–7/2 the prod sentinel appeared
with reason 'x' — verbatim the fixture string from tests/test_halt.py — no set-alert
was ever logged, and clear_halt() unlinked the only copy, so the episode (~35.5h live
outage) was unreconstructable. Three defenses now:
  1. set_halt() embeds full process provenance (pid/argv/hostname/cwd/under_pytest).
  2. clear_halt() archives the sentinel + clearer identity to logs/halt-history.jsonl
     BEFORE unlinking, so every halt episode is permanently reconstructable.
  3. Under pytest, set_halt()/clear_halt() REFUSE to touch the real PROD sentinel
     (tests that monkeypatch _HALT_PATH to a tmp dir keep working).
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# Always the repo-root state/ (NOT KALSHI_WEATHER_STATE_DIR, which is per-arm) so the
# halt is global across every live arm.
_ROOT_STATE = Path(os.environ.get(
    "KALSHI_WEATHER_DIR",
    Path.home() / ".openclaw/workspace/automations/kalshi-weather",
)) / "state"
_HALT_PATH = _ROOT_STATE / "LIVE_HALT.json"

# The PRODUCTION sentinel path — the module default with NO KALSHI_WEATHER_DIR
# override. Saved as its own constant (never reassigned by this module) so the pytest
# guard can tell "_HALT_PATH still points at the real prod sentinel" apart from "a test
# monkeypatched _HALT_PATH to a tmp dir" (tests/test_halt.py re-pins _HALT_PATH; the
# guard must not break that).
_PROD_HALT_PATH = (
    Path.home() / ".openclaw/workspace/automations/kalshi-weather" / "state" / "LIVE_HALT.json"
)

# Halt-episode archive (sibling logs/ of the state dir). clear_halt() appends the full
# sentinel content + clearer identity here BEFORE unlinking — the 2026-07-01 unlink
# destroyed the only copy of the mystery 'x' halt.
_HALT_HISTORY = _ROOT_STATE.parent / "logs" / "halt-history.jsonl"


def is_halted() -> Tuple[bool, Optional[str]]:
    """(True, reason) if live trading is halted, else (False, None). Never raises."""
    try:
        if not _HALT_PATH.exists():
            return False, None
        data = json.loads(_HALT_PATH.read_text())
        return True, str(data.get("reason", "halted"))
    except Exception:
        # A present-but-unreadable sentinel is treated as halted (fail safe).
        return _HALT_PATH.exists(), "halted (unreadable sentinel)"


def is_halted_safe() -> Tuple[bool, Optional[str]]:
    """Fail-SAFE halt check for live order paths: like is_halted(), but ANY error →
    (True, reason). is_halted() already swallows its own errors; this also guards a
    broken/refactored call so a failure can NEVER leave live trading enabled."""
    try:
        return is_halted()
    except Exception as e:  # pragma: no cover - defensive
        return True, f"halt check error — treating as halted: {e}"


def _provenance() -> dict:
    """Process identity for the sentinel/archive (2026-07-01 'x' incident: a bare
    {'reason': 'x'} sentinel could not be attributed to any process after the fact).

    CONSTRAINT: every field is individually best-effort — the kill-switch must still
    trip even if e.g. os.getcwd() raises because the cwd was deleted under us (cron
    scratch dirs) or the hostname lookup fails."""
    prov: dict = {}
    for field, fn in (
        ("pid", os.getpid),
        ("argv", lambda: list(sys.argv)),
        ("hostname", socket.gethostname),
        ("cwd", os.getcwd),
        ("under_pytest", lambda: "PYTEST_CURRENT_TEST" in os.environ),
    ):
        try:
            prov[field] = fn()
        except Exception:
            prov[field] = None
    return prov


def _refuse_pytest_prod_write(action: str) -> bool:
    """True → set_halt/clear_halt must REFUSE to touch the sentinel.

    WHY (2026-07-01 incident): a pytest run whose trader.halt import happened WITHOUT
    the test's KALSHI_WEATHER_DIR tmp override left _HALT_PATH at the real prod path,
    wrote reason 'x' (the tests/test_halt.py fixture string) into state/LIVE_HALT.json,
    and later unlinked it — ~35.5h live outage with unrecoverable provenance.
    CONSTRAINT: under pytest ('PYTEST_CURRENT_TEST' set) these functions must never
    touch the real production sentinel. We compare _HALT_PATH against the saved
    _PROD_HALT_PATH constant (not the env), so tests that monkeypatch _HALT_PATH to a
    tmp dir keep working. Never raises."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return False  # not a pytest process — normal operation
    try:
        if _HALT_PATH != _PROD_HALT_PATH and _HALT_PATH.resolve() != _PROD_HALT_PATH.resolve():
            return False  # test monkeypatched the sentinel to its own tmp dir — allowed
    except Exception:
        pass  # under pytest and can't prove it's NOT prod → refuse (fail safe)
    _notify(
        f"⚠️ halt {action} REFUSED: pytest run targeted the PROD sentinel "
        f"({_HALT_PATH}) — see 2026-07-01 'x' incident",
        key="halt_test_guard",
    )
    return True


def set_halt(reason: str, source: str = "manual") -> bool:
    """Trip the halt (idempotent: preserves the original reason/time if already set).

    Payload carries full process provenance (pid/argv/hostname/cwd/under_pytest) so a
    mystery sentinel like the 2026-07-01 'x' incident is attributable. Refuses to write
    the PROD sentinel from inside a pytest run (see _refuse_pytest_prod_write).
    Never raises."""
    try:
        if _refuse_pytest_prod_write("set"):
            return False
        if _HALT_PATH.exists():
            return True  # already halted — keep the original reason/timestamp
        _HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "reason": reason,
            "source": source,
            "tripped_utc": datetime.now(timezone.utc).isoformat(),
            **_provenance(),
        }
        tmp = _HALT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, _HALT_PATH)
        _notify(f"🛑 LIVE HALT set: {reason}", key="live_halt_set")
        return True
    except Exception:
        return False


def _archive_episode() -> None:
    """Append the current sentinel's full content + {cleared_utc, cleared_by} as one
    JSON line to logs/halt-history.jsonl (mkdir parents). Best-effort, NEVER raises —
    losing an archive line is acceptable; blocking a live resume is not."""
    try:
        try:
            content = json.loads(_HALT_PATH.read_text())
            if not isinstance(content, dict):
                content = {"sentinel": content}
        except Exception:
            # Unreadable/corrupt sentinel: still archive whatever raw bytes we can.
            try:
                content = {"sentinel_raw": _HALT_PATH.read_text(errors="replace")}
            except Exception:
                content = {"sentinel_raw": None}
        prov = _provenance()
        rec = dict(content)
        rec["cleared_utc"] = datetime.now(timezone.utc).isoformat()
        rec["cleared_by"] = {
            "pid": prov.get("pid"),
            "argv": prov.get("argv"),
            "hostname": prov.get("hostname"),
        }
        _HALT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with open(_HALT_HISTORY, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def clear_halt() -> bool:
    """Clear the halt (resume live). No-op if not halted.

    Archives the sentinel content + clearer identity to logs/halt-history.jsonl BEFORE
    unlinking (the 2026-07-01 unlink destroyed the only copy of the mystery 'x' halt).
    Refuses to unlink the PROD sentinel from inside a pytest run (see
    _refuse_pytest_prod_write). Never raises."""
    try:
        if _refuse_pytest_prod_write("clear"):
            return False
        if _HALT_PATH.exists():
            _archive_episode()
            _HALT_PATH.unlink()
            _notify("✅ LIVE HALT cleared — live trading may resume", key="live_halt_clear")
        return True
    except Exception:
        return False


def _notify(msg: str, key: str) -> None:
    try:
        from trader.notify import alert
        alert(msg, key=key, dedup_seconds=60)
    except Exception:
        pass

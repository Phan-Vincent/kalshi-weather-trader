#!/usr/bin/env python3
"""bin/notify_selfcheck.py — gateway-INDEPENDENT watchdog for the operator alert channel.

The whole operator-alert path (trader/notify.py) delivers via `openclaw message send`, which
needs the gateway CLIENT token (openclaw.json → gateway.remote.token) to match the gateway
SERVER token (gateway.auth.token if inline, else OPENCLAW_GATEWAY_TOKEN from ~/.openclaw/.env).
A cowork / gateway-supervisor restart can rotate the server token and leave remote.token
stale → EVERY alert fails ("unauthorized: gateway token mismatch") and the operator goes
blind. That happened 2026-07-07 ~17:35 PT → 2026-07-08 ~02:00 UTC (~8h) with no signal.

This check runs from the launchd sync job (bin/sync-live.sh, every :10/:40) which does NOT
depend on the gateway, and detects the outage two ways WITHOUT touching the gateway:
  1. token equality — remote.token vs the effective auth token. BOOLEAN ONLY: token values
     are NEVER printed, logged, or written anywhere.
  2. recent send failures — scans logs/alerts.jsonl for status send_failed/exception whose
     stderr mentions "token mismatch" / "unauthorized" / "gateway", in a recent window.

On a bad read it raises OUT-OF-BAND signals that need no gateway:
  - writes state/notify-channel-health.json — READ by bin/healthcheck.py (check 9), so the
    down-state surfaces in every cycle's HEALTH block + the supervisor read (NOT decorative);
  - appends a loud logs/ALERT-CHANNEL-DOWN.txt breadcrumb;
  - best-effort macOS `osascript` notification + `logger` os_log line.

Read-only w.r.t. openclaw.json / .env. Never raises (returns an exit code = severity).

Env overrides (testing): KALSHI_WEATHER_OPENCLAW_JSON, KALSHI_WEATHER_ENV_FILE,
KALSHI_WEATHER_ALERTS_LOG, KALSHI_WEATHER_CHANNEL_HEALTH, KALSHI_WEATHER_NOTIFY_FAIL_WINDOW_MIN.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
LOGS = ROOT / "logs"

_HOME = Path.home()
OPENCLAW_JSON = Path(os.environ.get("KALSHI_WEATHER_OPENCLAW_JSON", _HOME / ".openclaw/openclaw.json"))
ENV_FILE = Path(os.environ.get("KALSHI_WEATHER_ENV_FILE", _HOME / ".openclaw/.env"))
ALERTS_LOG = Path(os.environ.get("KALSHI_WEATHER_ALERTS_LOG", LOGS / "alerts.jsonl"))
HEALTH_PATH = Path(os.environ.get("KALSHI_WEATHER_CHANNEL_HEALTH", STATE / "notify-channel-health.json"))
SENTINEL = LOGS / "ALERT-CHANNEL-DOWN.txt"
FAIL_WINDOW_MIN = float(os.environ.get("KALSHI_WEATHER_NOTIFY_FAIL_WINDOW_MIN", "45"))

SEV_NAME = {0: "ok", 1: "degraded", 2: "down"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_env_var(env_file: Path, name: str):
    """Return the raw value of `name` from a KEY=VALUE .env file, or None. Never logs it."""
    try:
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != name:
                continue
            v = v.strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]
            return v
    except Exception:
        return None
    return None


def _remote_token(oc):
    try:
        return oc.get("gateway", {}).get("remote", {}).get("token")
    except Exception:
        return None


def _effective_auth_token(oc, env_file: Path):
    """The SERVER token the gateway authenticates with: inline gateway.auth.token if set,
    else OPENCLAW_GATEWAY_TOKEN from the process env, else parsed from the .env file."""
    try:
        t = oc.get("gateway", {}).get("auth", {}).get("token")
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        pass
    t = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if t and t.strip():
        return t
    return _parse_env_var(env_file, "OPENCLAW_GATEWAY_TOKEN")


def _tokens_match(oc):
    """True/False if determinable, else None. Compares values but returns ONLY a boolean —
    token strings never leave this function."""
    if not isinstance(oc, dict):
        return None
    remote = _remote_token(oc)
    auth = _effective_auth_token(oc, ENV_FILE)
    if remote and auth:
        return remote == auth
    return None  # can't locate one side → unknown; do not false-alarm on it


def _recent_send_failures(window_min: float) -> dict:
    """Recent primary-gateway send failures in the window, CLASSIFIED as {"auth", "transport",
    "total"}. 'auth' = token mismatch / unauthorized (needs a token reconcile); 'transport' = a
    gateway error that is NOT an auth failure (e.g. "GatewayTransportError: gateway timeout"). The
    two need different remediation, and blanket-labeling a transient timeout as a token mismatch
    produced a misleading "reconcile the token" page that misled the 2026-07-13 audit."""
    cutoff = _now() - timedelta(minutes=window_min)
    auth = transport = 0
    try:
        lines = ALERTS_LOG.read_text().splitlines()[-60:]
    except Exception:
        return {"auth": 0, "transport": 0, "total": 0}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("status") not in ("send_failed", "exception"):
            continue
        blob = ((r.get("stderr") or "") + " " + (r.get("error") or "")).lower()
        if not any(w in blob for w in ("token mismatch", "unauthorized", "gateway")):
            continue
        is_auth = ("token mismatch" in blob) or ("unauthorized" in blob)
        try:
            ts = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:  # a naive ts must not raise on comparison with tz-aware cutoff
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts >= cutoff:
            if is_auth:
                auth += 1
            else:                        # matched 'gateway' but no auth keyword → transport fault
                transport += 1
    return {"auth": auth, "transport": transport, "total": auth + transport}


def evaluate() -> dict:
    """Return the channel-health record (no token values). Pure read; no side effects."""
    oc = _read_json(OPENCLAW_JSON)
    match = _tokens_match(oc)
    sf = _recent_send_failures(FAIL_WINDOW_MIN)
    fails = sf["total"]

    if sf["auth"] > 0:
        sev = 2
        detail = (f"{sf['auth']} primary gateway AUTH send failure(s) in last {int(FAIL_WINDOW_MIN)}m "
                  f"(token mismatch/unauthorized) — reconcile gateway.remote.token "
                  f"(Discord fallback may be delivering)")
    elif sf["transport"] > 0:
        sev = 2
        detail = (f"{sf['transport']} primary gateway TRANSPORT failure(s) in last {int(FAIL_WINDOW_MIN)}m "
                  f"(gateway timeout/unreachable — NOT an auth problem, no token action) "
                  f"(Discord fallback may be delivering)")
    elif match is False:
        sev = 1
        detail = ("gateway.remote.token != effective auth token (OPENCLAW_GATEWAY_TOKEN) — "
                  "the next alert will fail; reconcile gateway.remote.token")
    else:
        sev = 0
        detail = "alert channel ok" if match else "alert channel ok (token match unverified)"

    return {
        "status": SEV_NAME[sev],
        "severity": sev,
        "tokens_match": match,          # bool or null — NEVER the token itself
        "recent_send_failures": fails,
        "detail": detail,
        "checked_utc": _now().isoformat(),
    }


def _write_health(rec: dict) -> None:
    """Persist the record, preserving `since` across a sustained bad state. Atomic."""
    prior = _read_json(HEALTH_PATH) or {}
    if rec["severity"] >= 1:
        rec["since"] = prior.get("since") if str(prior.get("status", "ok")) != "ok" else rec["checked_utc"]
    else:
        rec["since"] = None
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, HEALTH_PATH)
    except Exception:
        pass


def _sanitize(s: str) -> str:
    return s.replace('"', "'").replace("\n", " ").replace("\\", "")[:180]


def _raise_out_of_band(rec: dict) -> None:
    """Gateway-independent escalation. All best-effort; never raises. `detail` carries NO
    token values so these sinks are safe."""
    detail = _sanitize(rec.get("detail", "alert channel degraded"))
    status = str(rec.get("status", "degraded")).upper()
    # 1. Deterministic breadcrumb (always works).
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(SENTINEL, "a") as f:
            f.write(f"{rec.get('checked_utc')}\t{status}\t{detail}\n")
    except Exception:
        pass
    # 2. macOS notification (bonus — may not render from a headless launchd job).
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{detail}" with title "kalshi-weather ALERT CHANNEL {status}"'],
            capture_output=True, timeout=8,
        )
    except Exception:
        pass
    # 3. Unified log (visible via Console.app / `log show`).
    try:
        subprocess.run(["/usr/bin/logger", "-t", "kalshi-weather",
                        f"ALERT CHANNEL {status}: {detail}"], capture_output=True, timeout=8)
    except Exception:
        pass


def run() -> int:
    rec = evaluate()
    _write_health(rec)
    if rec["severity"] >= 1:
        _raise_out_of_band(rec)
    return rec["severity"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Gateway-independent operator-alert-channel watchdog")
    ap.add_argument("--json", action="store_true", help="print the health record as JSON")
    ap.add_argument("--quiet", action="store_true", help="print nothing on ok")
    a = ap.parse_args()
    rec = evaluate()
    _write_health(rec)
    if rec["severity"] >= 1:
        _raise_out_of_band(rec)
    if a.json:
        print(json.dumps(rec, indent=2))
    elif not (a.quiet and rec["severity"] == 0):
        print(f"alert-channel: {rec['status'].upper()} — {rec['detail']}")
    return rec["severity"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # a watchdog must never crash its host job
        print(f"[notify_selfcheck] non-fatal: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
        sys.exit(0)

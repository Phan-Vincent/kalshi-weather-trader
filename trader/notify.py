"""trader/notify.py — best-effort operator alerts via the openclaw Telegram bot.

Delivery shells out to `openclaw message send --channel telegram --target <id>`,
so NO bot token is handled here (openclaw's gateway owns it). This function
NEVER raises — on any failure (or when disabled) the alert is appended to
logs/alerts.jsonl so nothing is silently lost and a trading path can't crash on it.

Env:
  KALSHI_WEATHER_ALERTS=0           disable delivery (still logs locally)
  KALSHI_WEATHER_ALERT_TARGET       telegram chat id / @username (default paired chat)
  KALSHI_WEATHER_ALERT_CHANNEL      openclaw channel (default telegram)
  OPENCLAW_BIN                      openclaw CLI path (default 'openclaw')

De-dupe: identical `key` alerts within `dedup_seconds` are suppressed so a
failing cron doesn't spam (state in logs/alert-dedup.json).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(os.environ.get(
    "KALSHI_WEATHER_DIR",
    Path.home() / ".openclaw/workspace/automations/kalshi-weather",
))
_ALERTS_LOG = _ROOT / "logs" / "alerts.jsonl"
_DEDUP_STATE = _ROOT / "logs" / "alert-dedup.json"

# The PRODUCTION alerts log — the module default with NO KALSHI_WEATHER_DIR override.
# Saved as its own constant (never reassigned) so the pytest guard below can tell "a
# test left _ALERTS_LOG pointing at the real prod log" apart from "conftest/a test
# monkeypatched _ALERTS_LOG to a tmp dir" (the latter is allowed).
_PROD_ALERTS_LOG = (
    Path.home() / ".openclaw/workspace/automations/kalshi-weather" / "logs" / "alerts.jsonl"
)


def _refuse_pytest_prod_write() -> bool:
    """True → _log_local must REFUSE to append.

    WHY (2026-07-06 audit): the conftest isolation fix only monkeypatches _ALERTS_LOG
    in-process, so a test's __main__ block (tests/test_size_ramp.py) or the legacy
    non-pytest runner can append a synthetic row (e.g. live_order_fail) to the REAL
    prod alerts.jsonl. healthcheck derives CRITICAL 'posting broken -> halt_live +
    flatten_live' severity from row counts in that file — a leaked row can page the
    operator to halt live trading and cancel real orders over a test artifact (a
    spurious halt already cost ~35.5h). Fail safe: under pytest, never touch the real
    prod log unless the caller redirected _ALERTS_LOG to a tmp path. Never raises."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return False  # not a pytest process — normal operation
    try:
        if _ALERTS_LOG != _PROD_ALERTS_LOG and _ALERTS_LOG.resolve() != _PROD_ALERTS_LOG.resolve():
            return False  # redirected to a tmp log — allowed
    except Exception:
        pass  # under pytest and can't prove it's NOT prod → refuse (fail safe)
    return True
# Paired operator chat (see ~/.openclaw/credentials/telegram-pairing.json).
_TARGET = os.environ.get("KALSHI_WEATHER_ALERT_TARGET", "")
_CHANNEL = os.environ.get("KALSHI_WEATHER_ALERT_CHANNEL", "telegram")
_OPENCLAW = os.environ.get("OPENCLAW_BIN", "openclaw")
# Gateway-INDEPENDENT backup channel (2026-07-08). Operator-provisioned Discord webhook, used
# ONLY when the primary gateway send fails — so a gateway/token outage (which blinded the
# operator for ~8h on 2026-07-07) can't take the backup down with it. Deliberately a separate
# secret file, NOT derived from the OpenClaw .env token that desyncs.
_FALLBACK_CREDS = Path(os.environ.get(
    "KALSHI_WEATHER_FALLBACK_CREDS",
    Path.home() / ".openclaw/credentials/kalshi-weather-fallback.json",
))
# In-process backoff so a burst of failing alerts in a single cycle can't each pay the fallback
# timeout when the webhook itself is slow/down (e.g. an API break failing many orders while
# Discord also hangs). Resets per process (i.e. per trading cycle).
_FALLBACK_BACKOFF_S = 30.0
_fallback_last_fail = 0.0


def _log_local(rec: dict) -> None:
    if _refuse_pytest_prod_write():
        return  # under pytest with _ALERTS_LOG still at the prod path — do not pollute it
    try:
        _ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_ALERTS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _dedup_recent(key: str | None, window_s: float) -> bool:
    """True if this key fired within window_s (→ suppress). READ-ONLY: does NOT record the
    firing. QA-08 (2026-07-01): recording is split into _record_dedup(), called only AFTER a
    confirmed delivery, so a FAILED send doesn't burn the dedup window and leave the operator
    blind while the same condition (live_order_fail / budget_stop / stale_settle) keeps firing."""
    if not key:
        return False
    try:
        state = json.load(open(_DEDUP_STATE)) if _DEDUP_STATE.exists() else {}
    except Exception:
        state = {}
    return (time.time() - float(state.get(key, 0))) < window_s


def _record_dedup(key: str | None) -> None:
    """Record `key`'s firing time = now (atomic). Called only after a successful delivery."""
    if not key:
        return
    try:
        state = json.load(open(_DEDUP_STATE)) if _DEDUP_STATE.exists() else {}
    except Exception:
        state = {}
    state[key] = time.time()
    try:
        _DEDUP_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_STATE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _DEDUP_STATE)
    except Exception:
        pass


def build_cmd(text: str) -> list[str]:
    """The openclaw command this alert would run (no side effects)."""
    return [_OPENCLAW, "message", "send", "--channel", _CHANNEL,
            "--target", _TARGET, "--message", text]


def _fallback_deliver(text: str) -> tuple[bool, str]:
    """Gateway-INDEPENDENT backup delivery, tried ONLY after the primary send fails.

    POSTs to a Discord incoming webhook (different platform + transport than the Telegram
    gateway, so a gateway/token outage can't disable it too). URL lives in an operator-provisioned
    trader-local creds file — absent/placeholder → silently skipped. NEVER raises. Returns
    (delivered, detail) where detail is safe to log (no secret)."""
    global _fallback_last_fail
    # Never hit the network from a pytest run against the real (prod) creds path.
    if "PYTEST_CURRENT_TEST" in os.environ:
        try:
            prod = (Path.home() / ".openclaw/credentials/kalshi-weather-fallback.json").resolve()
            if _FALLBACK_CREDS.resolve() == prod:
                return (False, "skipped_pytest")
        except Exception:
            return (False, "skipped_pytest")
    try:
        cfg = json.loads(_FALLBACK_CREDS.read_text())
    except Exception:
        return (False, "not_configured")
    if not isinstance(cfg, dict):  # a bare-string / null / list creds file must not raise
        return (False, "not_configured")
    url = str(cfg.get("discord_webhook_url") or "").strip()
    if not url.startswith("https://") or "REPLACE" in url:
        return (False, "not_configured")
    # Bounded per-process backoff: after a recent fallback failure, skip the network POST so a
    # burst of failing alerts can't add ~timeout each while the webhook is hanging.
    if (time.time() - _fallback_last_fail) < _FALLBACK_BACKOFF_S:
        return (False, "backoff")
    try:
        data = json.dumps({"content": text[:1900]}).encode("utf-8")
        # Discord/Cloudflare blocks the default python-urllib User-Agent with a 403
        # (error 1010); an explicit UA is required (Discord API also requests one).
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "kalshi-weather-notify/1.0"},
            method="POST")
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = int(getattr(resp, "status", None) or resp.getcode())
        if 200 <= code < 300:
            return (True, f"http_{code}")
        _fallback_last_fail = time.time()
        return (False, f"http_{code}")
    except urllib.error.HTTPError as e:
        _fallback_last_fail = time.time()
        return (False, f"http_{e.code}")
    except Exception as e:
        _fallback_last_fail = time.time()
        return (False, f"exc:{type(e).__name__}")


def alert(text: str, key: str | None = None, dedup_seconds: float = 3600,
          dry_run: bool = False) -> bool:
    """Send an operator alert. Best-effort, NEVER raises. Returns True if delivered.

    dry_run=True prints the command and does not invoke openclaw (no outbound)."""
    body = f"🔔 kalshi-weather: {text}"
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "key": key, "text": body}
    if os.environ.get("KALSHI_WEATHER_ALERTS", "1") == "0":
        rec["status"] = "disabled"
        _log_local(rec)
        return False
    if _dedup_recent(key, dedup_seconds):
        rec["status"] = "deduped"
        _log_local(rec)
        return False
    cmd = build_cmd(body)
    if dry_run:
        print(" ".join(cmd))
        rec["status"] = "dry_run"
        _log_local(rec)
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _record_dedup(key)   # QA-08: only burn the dedup window on a CONFIRMED delivery
            rec["status"] = "sent"
            _log_local(rec)
            return True
        rec["status"] = "send_failed"
        rec["stderr"] = (r.stderr or "")[:300]
    except Exception as e:
        rec["status"] = "exception"
        rec["error"] = str(e)[:300]
    # Primary (gateway) send failed → try the gateway-independent fallback so the operator
    # isn't blind during a token-mismatch/gateway outage. The primary-failure row is ALWAYS
    # logged (so notify_selfcheck/healthcheck still see the gateway is broken and nag to fix
    # the token) even when the fallback delivers.
    fb_ok, fb_detail = _fallback_deliver(body)
    rec["fallback"] = fb_detail
    _log_local(rec)
    if fb_ok:
        _record_dedup(key)  # operator received it via the backup → dedup like a real delivery
        # Distinct key so healthcheck._count_recent_alerts (counts by key, any status) does not
        # tally this transport row on top of the primary-failure row for the same event.
        _log_local({"ts": datetime.now(timezone.utc).isoformat(),
                    "key": (f"{key}:fallback" if key else None),
                    "text": body, "status": "fallback_sent", "via": "discord_webhook"})
        return True
    return False

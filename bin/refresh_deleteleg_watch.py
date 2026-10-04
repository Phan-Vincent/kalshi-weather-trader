#!/usr/bin/env python3
"""Durable one-time tripwire for the LIVE quote-refresh cancel/DELETE leg's first prod firing.

Wired into the supervisor (bin/supervise.sh, :30 launchd slots, right after each :20 live cycle),
so it survives any interactive Claude session ending. Deterministic and sentinel-gated:

  1. The FIRST time a live `refresh_cancelled` event appears in
     state/live-premium/maker-lifecycle.jsonl -- the cancel/DELETE leg firing in prod for the first
     time ever (see the 2026-07-10 QUOTE_REFRESH=1 live flip; historically 0 live cancels) -- fire
     ONE Telegram alert via trader.notify.alert, note whether the quote was re-posted, and go quiet.
  2. Any NEW refresh_list_failed alert (LIST-leg error -> feature silently degrades to a no-op) ->
     alert. Baselined to the current count on first run so the 54 historical (6/25-7/3) don't fire.

Read-only except a small sentinel JSON in state/. NEVER raises (the supervisor must not fail on
this). py3.9-clean (no PEP-604 unions) since launchd PATH may resolve either python3.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_LC = ROOT / "state" / "live-premium" / "maker-lifecycle.jsonl"
ALERTS = ROOT / "logs" / "alerts.jsonl"
SENTINEL = ROOT / "state" / ".refresh_deleteleg_seen.json"


def _load_sentinel():
    try:
        return json.loads(SENTINEL.read_text())
    except Exception:
        return {}  # empty -> first-run baselining paths trigger below


def _save_sentinel(s):
    try:
        SENTINEL.write_text(json.dumps(s))
    except Exception as e:
        print("  [refresh-watch] sentinel write failed:", e)


def _read_jsonl(p):
    out = []
    try:
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as e:
        print("  [refresh-watch] read failed", p, e)
    return out


def _alert(text, key):
    try:
        sys.path.insert(0, str(ROOT))
        from trader.notify import alert
        ok = alert(text, key=key)
        print("  [refresh-watch] alert sent (%s): delivered=%s" % (key, ok))
        return ok
    except Exception as e:
        print("  [refresh-watch] notify failed:", e)
        return False


def main():
    rows = _read_jsonl(LIVE_LC)
    cancels = [r for r in rows if r.get("event") == "refresh_cancelled"]
    posts = [r for r in rows if r.get("event") == "posted_live"]
    sent = _load_sentinel()

    # 1. First DELETE-leg firing (0 -> >=1): alert exactly once, ever.
    if cancels and not sent.get("first_cancel_alerted"):
        c = cancels[0]
        tk = c.get("ticker")
        side = (c.get("side") or "").upper()
        px = c.get("limit_price_cents")
        fresh = c.get("fresh_price_cents")
        cts = c.get("ts", "")
        # Re-post correlation from state only: a posted_live for the same ticker/side AFTER the
        # cancel (ISO-8601 same-tz timestamps sort lexicographically).
        reposted = any(
            p.get("ticker") == tk and (p.get("side") or "").upper() == side
            and (p.get("ts", "") > cts)
            for p in posts
        )
        msg = (
            "LIVE quote-refresh DELETE leg fired in prod for the FIRST time: "
            "%s %s %sc cancelled (fresh=%s), %d live cancel(s) this run. Re-post seen in state: %s. "
            "VERIFY on Kalshi: the order actually left the book and there is no phantom double-order "
            "on %s. (2026-07-10 quote-refresh live flip)"
            % (tk, side, px, fresh, len(cancels), "yes" if reposted else "NO -- CHECK NOW", tk)
        )
        _alert(msg, key="refresh_deleteleg_first")
        sent["first_cancel_alerted"] = True
        _save_sentinel(sent)

    # 2. NEW refresh_list_failed (LIST-leg errors). Baseline on first run so historical don't fire.
    alert_rows = _read_jsonl(ALERTS)
    n_listfail = sum(1 for r in alert_rows if "refresh_list_failed" in (r.get("key") or ""))
    if "list_failed_baseline" not in sent:
        sent["list_failed_baseline"] = n_listfail
        _save_sentinel(sent)
    elif n_listfail > int(sent.get("list_failed_baseline", 0)):
        new = n_listfail - int(sent["list_failed_baseline"])
        _alert(
            "live quote-refresh LIST leg failed (refresh_list_failed x%d new) -- feature degraded to "
            "a no-op this cycle; check Kalshi API / list_resting_orders." % new,
            key="refresh_listfail_watch",
        )
        sent["list_failed_baseline"] = n_listfail
        _save_sentinel(sent)

    # Status line for the supervisor log.
    if not cancels:
        print("  [refresh-watch] DELETE leg not yet fired (0 live refresh_cancelled); list_failed baseline=%s"
              % sent.get("list_failed_baseline"))
    else:
        print("  [refresh-watch] %d live refresh_cancelled seen; first-firing alerted=%s"
              % (len(cancels), sent.get("first_cancel_alerted")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("  [refresh-watch] error (non-fatal):", e)
        sys.exit(0)

---
name: kalshi-status
description: Show the current health and trading status of the Kalshi weather bot — is it healthy, are cycles firing on schedule, is live trading halted, and do the balance/feeds reconcile. Use when the user asks for kalshi status, "how's the trader", "is the bot healthy/trading", or "is live halted".
argument-hint: "[--feeds]  (add --feeds for the slower ~30s network feed-canary check)"
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — status

Run from the trader root and summarize plainly. Root:
`/Users/you/.openclaw/workspace/automations/kalshi-weather`

Run these (fast, no network) and report a one-line verdict per check, then a 2–3 line summary:

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/halt_live.py            # halt state (🛑 HALTED / ✅ active)
python3 bin/healthcheck.py          # exit code = severity: 0 OK / 1 DEGRADED / 2 CRITICAL
python3 bin/check_cycle_cadence.py  # exit 1 = a scheduled cycle silently didn't start
python3 bin/triangulate_balance.py --dir live-premium   # 0 agree / 1 diverge / 2 leg read 0
```

Also confirm the most recent cycle actually fired:

```bash
grep 'cycle start' logs/cycle-$(date +%F).log | tail -3
```

Only if the user passes `--feeds` (it hits `kalshi-cli --prod`, ~30s), also run:

```bash
python3 bin/feed_canary.py --dir live-premium --no-alert
```

## Reporting

- Lead with an overall verdict: **HEALTHY / DEGRADED / CRITICAL / HALTED**.
- `healthcheck.py` and `check_cycle_cadence.py` use exit code as severity — a nonzero
  exit is a finding, **not** a script crash. Report the actual finding lines.
- If halted, say so first and note when/why (from the sentinel reason).
- A false "divergence" from `triangulate_balance` right after a non-weather (sports/Iran)
  settlement is a known benign alarm — mention it as a caveat rather than an alert.
- Keep it tight; don't paste full raw output unless the user asks.

---
name: kalshi-halt
description: Check, engage, or clear the Kalshi live-trading kill switch. Use when the user asks to "halt live trading", "stop the bot", "kill switch", "resume trading", "un-halt", or "is live halted". Halting stops NEW orders; it does not cancel resting orders.
argument-hint: "status | on <reason> | off"
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — halt / resume live trading

Root: `/Users/you/.openclaw/workspace/automations/kalshi-weather`

The halt is a global sentinel `state/LIVE_HALT.json`. **Always use `halt_live.py`** —
never hand-edit the sentinel (a pytest fixture writing "x" there once caused a 35.5h
outage). Only ever pass a clear, human reason.

## status (read — no confirm)

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/halt_live.py
```

## on — engage the kill switch (confirm first)

Confirm with the user, then run with their reason:

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/halt_live.py --on "<clear human reason>"
```

🔴 **Halting only stops NEW orders. Resting GTC quotes stay live on the book.** After
halting, tell the user this and offer to cut exposure with flatten (an order action,
so it needs its own explicit yes):

```bash
# only after the user explicitly confirms they want to cancel/exit resting orders:
python3 bin/flatten_live.py --execute      # dry run: omit --execute
```

## off — clear the kill switch (confirm first)

Confirm, then:

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/halt_live.py --off
```

Live arms resume placing real orders on the next `LIVE=1` cycle. Clearing archives the
prior sentinel to `logs/halt-history.jsonl` first.

## Reporting

- Confirm the resulting state by echoing the tool's output (`🛑 HALTED` / `✅ active`).
- When you engage a halt, always end by reminding the user that resting orders are
  still working unless they flatten.

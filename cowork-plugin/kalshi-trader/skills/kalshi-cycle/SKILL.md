---
name: kalshi-cycle
description: Trigger a Kalshi trading cycle on demand — PAPER by default (safe), or LIVE (places real orders now) only with explicit confirmation. Use when the user asks to "run a cycle", "kick a kalshi cycle", "trade now", or "fire a paper/live cycle".
argument-hint: "[paper|live]   (default: paper)"
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — trigger a cycle

Root: `/Users/you/.openclaw/workspace/automations/kalshi-weather`

Default to **paper** unless the user clearly said "live". The two are the *same*
pipeline; the only difference is the `LIVE=1` env var.

## Paper cycle (safe — default)

The live arm self-skips without `LIVE`, so no real orders are possible.

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
bash run-cycle.sh
```

## Live cycle (REAL MONEY — confirm first, every time)

🚨 **`LIVE=1` places real orders immediately — there is no time-of-day guard.** Firing
this off-slot opens real exposure *now*. This is a deliberate "trade live now" action,
not a test.

**Before running, you MUST:**
1. State plainly: "This will place REAL orders on the live account right now." Show the
   exact command. **Get an explicit 'yes' from the user in this chat.** Do not proceed
   on ambiguity.
2. Check it isn't halted first (`python3 bin/halt_live.py`) — if halted, tell the user;
   a live cycle won't trade while halted.

Only after an explicit yes:

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
LIVE=1 bash run-cycle-detached.sh
```

## Always: confirm it actually traded

A trigger can silently no-op if launchd already holds `logs/.cycle.lock`. **Never report
success from the exit code alone.** Verify a *fresh* marker:

```bash
grep 'cycle start' logs/cycle-$(date +%F).log | tail -3
```

If there's no fresh `cycle start` in the last few minutes, report that the cycle did
**not** run (another holder had the lock) rather than claiming it traded.

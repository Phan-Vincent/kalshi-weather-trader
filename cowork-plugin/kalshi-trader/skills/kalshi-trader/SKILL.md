---
name: kalshi-trader
description: This skill should be used whenever the user asks about or wants to operate the Kalshi weather trading bot (a LIVE, real-money trading system at ~/.openclaw/workspace/automations/kalshi-weather). Triggers include "kalshi status", "how's the trader/bot doing", "what's my kalshi P&L", "did the trader trade/fill", "is live trading halted", "halt live trading", "resume trading", "run a cycle", "show the trading dashboard", "kalshi review", or any monitoring or control of the Kalshi weather trader. Provides the exact read-only diagnostics, the two safe controls (trigger a cycle, halt/resume), and the live-money safety rules that MUST be followed.
version: 0.1.0
---

# Kalshi Weather Trader — operations playbook

This skill lets you monitor and safely operate a **live, real-money** Kalshi weather
trading bot. The trader itself is deterministic Python fired by launchd; you are the
**operator console** on top of it.

**Trader root (always `cd` here first):**
`/Users/you/.openclaw/workspace/automations/kalshi-weather`

Scope of this plugin = **monitor + safe controls only**: read everything, trigger a
cycle, halt/resume. **No ad-hoc order placement.** Placing/canceling/flattening
individual orders is out of scope unless the user explicitly and unambiguously asks,
and even then you confirm first (see Safety rules).

The full command reference with flags and output notes is in
[references/commands.md](references/commands.md) — load it when you need exact flags.

## ⚠️ Live-money safety rules (read before any control action)

1. **`LIVE=1` places REAL orders immediately, at any time of day.** There is **no**
   time-of-day guard in the code — the only reason `:20` is a "trading slot" is that
   launchd fires it then. Firing a live cycle off-slot opens real exposure *now*.
   Treat any live cycle as a deliberate "trade live now" action, never a test.
   **To test plumbing, use the PAPER cycle** (`bash run-cycle.sh`, no `LIVE`).
2. **Halting only stops NEW orders.** `halt_live.py --on` leaves resting GTC quotes
   working on the book. To actually cut exposure the user must ALSO run
   `bin/flatten_live.py --execute` — surface this every time you halt.
3. **`flatten_live.py --execute` cancels/exits real orders** → out of the default
   tier. Only run it on an explicit, confirmed "yes, flatten" from the user.
4. **Confirm before every control** (run-cycle-live, halt on/off, flatten). Read
   actions never need confirmation; run them freely.
5. **A manual trigger can silently no-op.** `logs/.cycle.lock` (atomic mkdir) means if
   launchd already holds the lock, your trigger exits cleanly without trading (and
   vice-versa). **Never report "traded" from a zero exit code** — confirm a *fresh*
   `cycle start` marker in `logs/cycle-<date>.log`.
6. **Never touch the halt from anything test-shaped.** Use `halt_live.py --on/--off`
   only, with a clear human reason. Never hand-edit `state/LIVE_HALT.json` (a pytest
   fixture writing "x" there once caused a 35.5h live outage).
7. **Notifications go to Telegram** via the trader's `bin/notify.py`
   (`openclaw message send --channel telegram`) — **not Slack**. Don't wire a new
   channel. Every alert also lands in `logs/alerts.jsonl`.
8. **Never set** `LIVE_LEGACY=1` (double-trades) or override `KALSHI_WEATHER_DIR`
   (points the kill-switch at the wrong path).

## How to respond to common asks

Pick the tightest command(s), run them from the trader root, then give a **concise
plain-language summary** — not raw dumps. Prefer the dedicated `/kalshi-*` skills
when the user types a slash command; otherwise run the commands directly.

| User intent | Do this |
|---|---|
| "status", "how's the bot", "is it healthy/trading?" | `/kalshi-status` set: `healthcheck.py`, `check_cycle_cadence.py`, `feed_canary.py --dir live-premium`, `halt_live.py` (halt state), `triangulate_balance.py --dir live-premium` |
| "P&L", "how much did we make", "fill quality", "edge" | `/kalshi-pnl` set: `fill_edge_breakdown.py --live-dir state/live-premium`, `live_paper_gap.py`, `live_fill_quality.py --live-dir state/live-premium`, `check_fill_milestone.py --live-dir state/live-premium`, `brier_report.py` |
| "nightly review", "summarize today" | `/kalshi-review`: `bash bin/nightly-review.sh` |
| "dashboard", "show me the charts" | `/kalshi-dashboard`: refresh data + point at the already-running dashboard on `http://localhost:8774` |
| "run a cycle", "trade now", "kick a cycle" | `/kalshi-cycle` — **paper by default**; live requires explicit confirm |
| "halt", "stop trading", "resume", "is it halted" | `/kalshi-halt` — `halt_live.py --on "reason"` / `--off`, always mention flatten |
| "did it trade at :20?", "did the last cycle fire?" | `grep 'cycle start' logs/cycle-$(date +%F).log` and read the tail of that block |

## Reporting style

- Lead with the answer (OK / DEGRADED / the number), then 1–3 supporting lines.
- Exit codes are severity for several tools (`healthcheck.py`: 0 OK / 1 DEGRADED /
  2 CRITICAL) — don't treat a nonzero exit as a script crash.
- Paper P&L is a ~6×-inflated proxy for live edge — never quote paper as if it were
  live. The live book is `state/live-premium/`; the paper book is `state/paper/`.
- The account also holds non-weather positions (sports, an Iran contract). Trust the
  weather trader's **realized** P&L reporters, not raw account equity.

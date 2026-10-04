# kalshi-trader (Cowork plugin)

Operate and monitor the **Kalshi weather trading bot**
(`~/.openclaw/workspace/automations/kalshi-weather`) from a Claude Cowork / Claude Code
chat. Tier: **monitor + safe controls** — read everything, trigger a cycle, halt/resume.
No ad-hoc order placement.

## Skills

| Skill | Type | What it does |
|---|---|---|
| `kalshi-trader` | model-invoked | Always-on playbook + safety rules; routes plain-language asks ("how's the bot?", "halt live") to the right command. |
| `/kalshi-status` | read | Health, cycle cadence, halt state, balance reconciliation. `--feeds` for the slower feed check. |
| `/kalshi-pnl` | read | Realized live P&L, fill quality, edge breakdown, live-vs-paper gap, milestone. `--calibration` for Brier. |
| `/kalshi-review` | read | Runs the nightly review and summarizes it. |
| `/kalshi-dashboard` | read | Refreshes dashboard data; points at http://localhost:8774. |
| `/kalshi-cycle` | **control** | Triggers a cycle — paper by default; live requires explicit confirmation. |
| `/kalshi-halt` | **control** | Check / engage / clear the live kill switch (+ flatten reminder). |

## Safety model

- `LIVE=1` places **real orders immediately** — there is no time-of-day guard. Live
  cycles are always confirmed in chat first.
- Halting stops **new** orders only; resting quotes need `flatten_live.py --execute`
  (out of the default tier, separately confirmed).
- Alerts go to **Telegram** via the trader's `bin/notify.py`, never Slack.
- A manual trigger can silently no-op behind `logs/.cycle.lock`; success is confirmed
  by a fresh `cycle start` marker, never by exit code.

## Install

This is a standard Claude Code / Cowork plugin directory (identical format).

- **Cowork:** import the packaged `kalshi-trader.plugin` (a zip of this directory) via
  the Cowork plugin importer.
- **Claude Code:** add this directory as a local plugin (e.g. via a local marketplace
  entry pointing at `./cowork-plugin/kalshi-trader`, or `claude plugin` in an
  interactive session), then enable `kalshi-trader`.

The skills invoke the trader by absolute path, so the plugin works wherever it's
installed as long as the trader stays at
`~/.openclaw/workspace/automations/kalshi-weather`.

## Companion: the supervisor

A separate, always-on **launchd supervisor** (`bin/supervise.sh` +
`deploy/com.vincentphan.kalshi-weather-supervisor.plist` in the trader repo) runs
`claude -p` at `:30` after each live slot to verify the cycle traded, report P&L, and
flag anomalies to Telegram — replacing the flaky deepseek/kimi agent-turn reporter.
See the trader repo for install steps.

# Kalshi trader — full command reference

Run everything from the trader root:
`cd /Users/you/.openclaw/workspace/automations/kalshi-weather`

`[read]` = safe, no side effects (may send at most one dedup'd Telegram alert on a real
problem). `[control]` = changes trading state — **confirm with the user first**.

## Read — status & health

| Command | What it tells you |
|---|---|
| `python3 bin/healthcheck.py` | Trading-outcome health. Prints `HEALTH: OK\|DEGRADED\|CRITICAL` + per-check lines. **Exit code = severity** (0/1/2; 3 = healthcheck crashed). Add `--quiet` to print only when not OK. |
| `python3 bin/check_cycle_cadence.py` | Did cycles actually *start* on schedule? Prints `CYCLE CADENCE: OK\|GAP` with missed slots (PT). Exit 1 = gap. Flags: `--window-hours 3`, `--json`, `--no-alert`. |
| `python3 bin/feed_canary.py --dir live-premium` | Field-level "reads real today, 0/null tomorrow" watch on the load-bearing Kalshi feeds. Prints `FEED CANARY: OK\|DEGRADED`. Hits `kalshi-cli --prod` (~30s). Add `--no-alert` for a pure read, `--json`. |
| `python3 bin/halt_live.py` | (no args) Current halt state: `✅ active (not halted)` or `🛑 HALTED — <reason>`. File check: `test -f state/LIVE_HALT.json`. |
| `python3 bin/triangulate_balance.py --dir live-premium` | Cross-checks live balance/positions across 3 independent read paths. Exit 0 = agree, 1 = diverge, 2 = a leg read 0/null. Strictly read-only (GETs only). |

## Read — P&L & performance

| Command | What it tells you |
|---|---|
| `python3 bin/fill_edge_breakdown.py --live-dir state/live-premium` | Realized edge + maker fill-rate sliced by side/city/type/price-band/lead. Overall EV-after-fee/ct + realized $. `--json`, `--write`. Use `--live-dir state/paper` for the paper book. |
| `python3 bin/live_paper_gap.py` | Paired paper-vs-live per-contract gap on shared markets, split into execution/markout vs selection. Needs ≥5 matched events. `--write` to persist. |
| `python3 bin/live_fill_quality.py --live-dir state/live-premium` | Live fill quality early-warning: EV/ct, Wilson win-rate, 2–8h markout. Prints `OK\|ACCUMULATING\|WATCH\|ADVERSE`. Always exit 0. `--json`. |
| `python3 bin/check_fill_milestone.py --live-dir state/live-premium` | Current live fill count vs 30/100 thresholds. |
| `python3 bin/brier_report.py` | Brier + calibration for the **paper** book: Brier Skill Score vs market (positive = we beat market), calibration table. `--json`, `--min-trades N`. |
| `python3 bin/model_vs_market.py` | Model vs market Brier on settled markets, by edge bucket. **Header warns magnitude is UNRELIABLE** — treat as directional only. |
| `python3 bin/paper_trade.py --report --state-dir state/paper` | Paper book report (positions/realized P&L on the paper book). |

## Read — narrative & dashboard

| Command | What it does |
|---|---|
| `bash bin/nightly-review.sh` | Runs the full nightly review (wraps `bin/nightly-review.py`) and produces the review write-up. |
| `python3 bin/generate_live_data.py` | Regenerate the live dashboard JSON. |
| `python3 bin/generate_dashboard_data.py` | Regenerate the paper dashboard JSON. |
| `python3 bin/build_dashboard.py` | Rebuild the static dashboard HTML. |
| dashboard URL | A launchd `kalshi-dashboard` job already serves it on **http://localhost:8774** — point the user there after refreshing data. (Ad-hoc: `python3 -m http.server 8000 --directory dashboard`.) |

## Control — confirm first

| Command | Effect |
|---|---|
| `bash run-cycle.sh` | **PAPER** cycle (no `LIVE`). Safe: the live arm self-skips. Use to test plumbing. |
| `LIVE=1 bash run-cycle-detached.sh` | **LIVE** cycle — places REAL orders immediately, any time of day. Deliberate "trade now". launchd equivalent: `launchctl start com.vincentphan.kalshi-weather-live`. |
| `python3 bin/halt_live.py --on "reason"` | Writes global sentinel `state/LIVE_HALT.json`; live arms stop placing **new** orders next cycle. Idempotent. **Does NOT cancel resting quotes.** |
| `python3 bin/halt_live.py --off` | Archives to `logs/halt-history.jsonl`, removes the sentinel, live resumes next `LIVE=1` cycle. |
| `python3 bin/flatten_live.py --execute` | **Cancels/exits real orders** to cut exposure. Out of the default tier — only on explicit confirmed request. Dry run: omit `--execute`. |

## Confirming a trigger actually traded

A manual cycle can silently no-op if launchd holds `logs/.cycle.lock`. Always verify:

```bash
grep 'cycle start' logs/cycle-$(date +%F).log | tail -3
```

Look for a *fresh* marker (its timestamp inside the log is **UTC**, the filename is PT).
No fresh marker → your trigger did not run the cycle; something else held the lock.

## Notifications

`python3 bin/notify.py "message" --key some_dedupe_key` → Telegram via
`openclaw message send` (target $KALSHI_WEATHER_ALERT_TARGET), deduped 1h per key, always appended to
`logs/alerts.jsonl`. Add `--print` for a dry run (prints the command, sends nothing).

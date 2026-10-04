# Cron decouple — APPLIED 2026-06-29 (historical)

> **STATUS (2026-07-08): APPLIED — do NOT re-apply.** The `~/.openclaw/cron/jobs.json` edit
> below was made on 2026-06-29. Current reality: LIVE cron `a5405e51` (`enabled=true`) forces
> `LIVE=1 …/run-cycle-detached.sh` as its mandatory first tool call; PAPER cron `687ee3be`
> uses the wrapper but is `enabled=false` (the launchd `-paper` plist is the real PAPER
> trigger). The launchd `-live`/`-paper` plists are the independent PRIMARY executors. The
> "The change to apply" section below is retained for incident-debugging history only.
>
> **UPDATE (2026-07-10): LIVE cron `a5405e51` DISABLED (`enabled=false`).** It was a *redundant*
> live trigger — both it and the launchd `-live` plist fired every `:20` slot; the shared cycle
> lock (`bin/acquire_cycle_lock.sh`) serialized them (one traded, one skipped), but the skipper
> falsely paged `cycle_live_lockskip` on ~every slot. Two things resolved keeping it: (1) the false
> page is fixed at the source (commit adc28e1 — a live cycle now skips a healthy live holder
> SILENTLY); (2) launchd `-live` reboot-persistence was verified (durably `enabled` in
> `launchctl print-disabled`, reloads at login) and the cron shared the same gateway login-dependency,
> so it was never a reboot hedge. Now launchd `-live` is the SOLE live executor. Disabled via
> `openclaw cron disable a5405e51-0462-4e02-b1d4-a95270767d5e` (gateway hot-reloads jobs.json; verified
> in-memory via `openclaw cron get`); `jobs.json` backed up to
> `jobs.json.bak.2026-07-10-1154-predisable-a5405e51`. REVERT: `openclaw cron enable a5405e51-…`.
> Note `~/.openclaw/cron/jobs.json` is NOT in this repo — this doc is the version-controlled record.

**Problem (verified 2026-06-29):** the two trading crons are `kind: agentTurn`, `model:
moonshot/kimi-k2.6`. The LLM turn runs `run-cycle.sh` as a tool call. When a model call
times out (run records: 144× "execution timed out (last phase: model-call-started)"),
the agent turn is torn down and kills the in-flight `run-cycle.sh`, so the cycle reaches
**0 orders** and a false `cycle_fail` fires. Job `timeoutSeconds=3600` is NOT the limiter
(healthy cycles finish ~5–9 min); the model-call timeout is. So raising the timeout does
nothing. The cure is to run the script **detached** from the agent turn.

**What's already done (safe, in repo):**
- `run-cycle-detached.sh` — launches `run-cycle.sh` in its own session via Python
  `start_new_session=True` (macOS has no `setsid`; Python is the trader's runtime), so it
  survives agent-turn teardown, then best-effort-waits so the agent can still tail/report
  fills. Execution guaranteed; Slack reporting best-effort. Env (LIVE=1) is inherited.
- `run-cycle.sh` EXIT trap now reports the true rc + caught signal and routes external
  teardown to a quieter `cycle_interrupted` key instead of false `cycle_fail`.

## The change to apply (edit `~/.openclaw/cron/jobs.json`)

Point each job's `payload.message` at the wrapper. Only the script path changes.

**LIVE job `a5405e51-0462-4e02-b1d4-a95270767d5e`:**
- FROM: `Run: LIVE=1 ~/.openclaw/workspace/automations/kalshi-weather/run-cycle.sh. Then tail -20 the cycle log. If fills: post ticker/side/qty/price to #trade-log (2 lines max). If no fills: silent. If errors: post to #error-log.`
- TO:   `Run: LIVE=1 ~/.openclaw/workspace/automations/kalshi-weather/run-cycle-detached.sh. It prints the last 20 log lines when the cycle finishes (or returns early if still running — that is normal). If fills: post ticker/side/qty/price to #trade-log (2 lines max). If no fills: silent. If errors: post to #error-log.`

**Paper job `687ee3be-da6e-42d4-8d9d-1f02786a7043`:**
- FROM: `Run ~/.openclaw/workspace/automations/kalshi-weather/run-cycle.sh. Tail -5 cycle log. If new fills: post 1-line summary. Otherwise: silent. Do NOT modify code.`
- TO:   `Run ~/.openclaw/workspace/automations/kalshi-weather/run-cycle-detached.sh. It prints recent log lines on completion. If new fills: post 1-line summary. Otherwise: silent. Do NOT modify code.`

Back up first: `cp ~/.openclaw/cron/jobs.json ~/.openclaw/cron/jobs.json.bak.predecouple`
(`jobs.json` is hot-reloaded by the cron daemon; no restart needed — verify behavior.)

## Alternative (bigger, not chosen here)
Convert the payload from `agentTurn` to a direct shell/exec kind (no LLM in the trading
path at all), with a separate light agent cron a few minutes later to read the log and
post fills. Removes the model-call-timeout failure mode entirely but needs the openclaw
cron exec-payload schema confirmed and splits the Slack reporting into its own job.

## Rollback
Revert the two `payload.message` strings (or restore the backup). The wrapper and trap
changes are inert once the cron points back at `run-cycle.sh`.

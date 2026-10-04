#!/usr/bin/env bash
# Full paper-trading cycle: build fair values, sweep pending makers, post new quotes, settle finalized.
# Designed to be called from cron. Logs to logs/cycle-YYYY-MM-DD.log.
set -euo pipefail

ROOT="$HOME/.openclaw/workspace/automations/kalshi-weather"
cd "$ROOT"

DATE=$(date +%Y-%m-%d)
LOG="$ROOT/logs/cycle-${DATE}.log"
mkdir -p "$ROOT/logs"

# ── Single-cycle lock (portable; mkdir is atomic on POSIX, works on macOS+Linux) ──
# The Python state writers use atomic tmp+rename, which prevents file *corruption*
# but NOT lost updates if two cycles overlap (cron interval < cycle duration). This
# lock makes an overlapping cycle exit cleanly. A stale lock (owner PID dead after a
# kill -9) is reclaimed automatically.
LOCKDIR="$ROOT/logs/.cycle.lock"
# A cycle takes ~5-9 min; a lock older than this is a wedged or crash-window holder.
# FIX 2026-07-06 (audit): the pid-liveness check alone could wedge trading FOREVER —
# an empty-pid lock (holder mkdir'd then crashed before writing its pid, at line
# "echo $$ > pid") was treated as ALIVE and never reclaimed, and a genuinely-hung but
# still-alive holder had no time bound. A max-age reclaim covers both.
LOCK_MAX_AGE_MIN="${KALSHI_WEATHER_LOCK_MAX_AGE_MIN:-30}"
# Acquisition (+ max-age/stale reclaim, live-priority preemption of a paper holder, and a
# LOUD alert when a live slot is lock-skipped) lives in a sourced, unit-tested helper.
# SOURCED not exec'd: $$ stays this cycle's pid for the ownership-aware release below, and
# a skip `exit 0`s this script. It writes $LOCKDIR/pid and $LOCKDIR/mode on success.
# Root cause for the live-priority/alert additions: 2026-07-08 09:20 PDT live-slot starvation.
. "$ROOT/bin/acquire_cycle_lock.sh"
# Release the lock on exit. Distinguish a genuine in-script failure from EXTERNAL
# teardown: when the kimi agentTurn cron is killed on a model-call timeout it tears
# down our process group, which used to surface as a misleading "early abort, exit 1"
# (the bare `exit $?` actually reported the `[ ]` test's status, not the real cause).
# Now we snapshot the true rc + any caught signal and only page cycle_fail for real
# failures; external interrupts go to the quieter cycle_interrupted key. (_cycle_ok is
# set to 1 only after the main cycle completes; robust against the `{ } | tee` subshell.)
_cycle_ok=0
_trapsig=""
_on_sig() { _trapsig="$1"; }
trap '_on_sig TERM' TERM
trap '_on_sig INT'  INT
trap '_on_sig HUP'  HUP
trap '_on_sig PIPE' PIPE
_on_exit() {
  local rc=$?
  # Ownership-aware release: only remove the lock if WE still own it. If our lock was
  # reclaimed by a later cycle (we wedged past LOCK_MAX_AGE_MIN), that cycle now owns a
  # fresh lock — a blind `rm -rf` here would steal it back and re-open the overlap race.
  if [ "$(cat "$LOCKDIR/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$LOCKDIR"
  fi
  if [ "${_cycle_ok}" = 1 ]; then return 0; fi
  if [ -n "${_trapsig}" ]; then
    python3 "$ROOT/bin/notify.py" "run-cycle.sh interrupted by SIG${_trapsig} (external teardown; rc=${rc}) — cycle may be incomplete, not a script bug" --key cycle_interrupted >/dev/null 2>&1 || true
  else
    python3 "$ROOT/bin/notify.py" "run-cycle.sh did NOT complete (early abort, rc=${rc})" --key cycle_fail >/dev/null 2>&1 || true
  fi
}
trap _on_exit EXIT

SERIES_DEFAULT="KXHIGHAUS,KXHIGHCHI,KXHIGHDEN,KXHIGHLAX,KXHIGHMIA,KXHIGHNY,KXHIGHPHIL,KXHIGHTATL,KXHIGHTBOS,KXHIGHTDAL,KXHIGHTDC,KXHIGHTHOU,KXHIGHTLV,KXHIGHTMIN,KXHIGHTNOLA,KXHIGHTOKC,KXHIGHTPHX,KXHIGHTSATX,KXHIGHTSEA,KXHIGHTSFO,KXLOWTATL,KXLOWTBOS,KXLOWTCHI,KXLOWTDAL,KXLOWTDC,KXLOWTDEN,KXLOWTHOU,KXLOWTLAX,KXLOWTLV,KXLOWTMIA,KXLOWTMIN,KXLOWTNOLA,KXLOWTNYC,KXLOWTPHIL,KXLOWTPHX,KXLOWTSATX,KXLOWTSEA,KXLOWTSFO"
SERIES="${KALSHI_WEATHER_SERIES:-$SERIES_DEFAULT}"

# FIX 2026-06-18: Let LESSONS.md control side_bias (currently "prefer_yes"
# after calibration showed YES=86% acc vs NO=46%). Only override if env var set.
if [ -n "${KALSHI_WEATHER_SIDE_BIAS:-}" ]; then
  echo "KALSHI_WEATHER_SIDE_BIAS=$KALSHI_WEATHER_SIDE_BIAS (env override)"
else
  echo "KALSHI_WEATHER_SIDE_BIAS=unset (LESSONS.md controls)"
fi

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) cycle start ==="
  echo "Series: $SERIES"

  echo
  echo "--- 0. Healthcheck (trading-outcome; alert-only, evaluates prior cycle) ---"
  python3 bin/healthcheck.py 2>&1 || true

  echo
  echo "--- 1. Build fair values (LIVE: climatology-only — forecast model gated off)"
  # 2026-06-25 (audit live-invariance): the Gaussian-ensemble fallback in fair_value.py used to
  # price live markets off the forecast ensemble (GEFS / error_std #7 / dressed members) whenever
  # climatology was unavailable — a leak past the intended live/forecast split. It is now gated
  # behind USE_FORECAST, so this LIVE build (no env) prices on climatology+persistence ALONE;
  # climatology-unavailable markets (e.g. NYC) are untradeable live until Stage 2 ships the
  # forecast deliberately. Forecast knobs (KDE_SHRINK etc.) live in the PAPER build below.
  # GEFS_SKIP_CLIMO=1 (2026-07-06, human sign-off): this climatology build (USE_FORECAST off)
  # DISCARDS the GEFS ensemble at fair_value.py:636 — proven byte-for-byte price-invariant to GEFS
  # (tests/test_gefs_climo_invariance.py). Skipping the (once-per-hour, cfgrib) fetch here trims the
  # pre-live critical path; the paper USE_FORECAST=1 builds (1b/1c) below still fetch + warm the
  # hour cache. Command-scoped so it never leaks to step 1a or the paper builds. Revert: drop the env.
  KALSHI_WEATHER_GEFS_SKIP_CLIMO=1 \
    python3 bin/build_fair_values.py --series "$SERIES" --out fair-values.json 2>&1 || echo "(build_fair_values failed, continuing with stale file)"

  # ── 1a. LIVE arm trades FIRST — right after the ONE build it needs ──────────────────
  # 2026-07-06 (live-first split): premium-live prices on fair-values.json (climatology,
  # just built above) and fetches its OWN fresh book (SHARED_BOOK=0) — it does NOT depend on
  # the two GEFS/forecast builds (1b/1c, paper-only) or the 7 paper arms below. Those used to
  # run BEFORE the live order, so any teardown/stall in that long tail killed the cycle with
  # "38/38 built, killed before trading phase" and ZERO live orders (the 6/24-6/25 SIGKILL
  # mode the bot keeps flagging). Placing the real order here collapses the pre-trade
  # kill-surface from [build#1 + build#1b + build#1c + 7×(settle+trade+report)] to [build#1].
  # `--only-live` self-gates: LIVE unset (paper :00 cron) or halted → "No arms to run", a
  # cheap no-op. The paper run below uses `--exclude-live`, so the live arm never double-trades.
  echo "--- 1a. LIVE arm (real orders) — placed before paper builds/arms so a slow paper phase can't starve it"
  python3 bin/run_variants.py --only-live 2>&1 || true

  # 2026-06-20: SECOND build for PAPER ONLY with the forecast-informed fix
  # (KALSHI_WEATHER_USE_FORECAST=1). The fix corrects a bug where the model
  # discarded its NBM/GEFS/ensemble/KDE forecast and priced on climatology alone.
  # bin/validate_forecast_skill.py measures forecast-vs-climatology skill; it was
  # REWRITTEN 2026-07-03 (weather-model QA) to score the CURRENT model leak-free,
  # one-forecast-per-event — the old "~63%" headline was the retired Gaussian arm on
  # pseudo-replicated, same-day-contaminated data. Provisional re-baseline is ~+50%
  # (event-clustered CI excludes 0); the strict post-LEAK-1 number accrues as the
  # Open-Meteo archive catches up (~1wk). LIVE keeps the legacy fair-values.json above
  # until the forecast-informed paper P&L proves out. Forecast cache makes this cheap.
  echo "--- 1b. Build fair values (PAPER: forecast-informed fix + tuned bandwidth, validation)"
  # KDE_SHRINK 1.0→0.4 (Phase C Stage 0, 2026-06-25): bin/deprecated/backtest_real.py --shrink-sweep (QUARANTINED; see bin/deprecated/README.md) over
  # 7,938 real thresholds (21d) — KDE over-smooths; CRPS-optimal shrink ≈0.4 (CRPS 1.555→1.159;
  # 0.3 is NO better at 1.160, so 0.4 is the true optimum, not over-sharpening). This is the
  # PRODUCTION sharpness lever — the model is KDE-dominated under USE_FORECAST (SCALE_MULT, the
  # Gaussian-path knob crps_report names, barely moved built probs, so it was dropped). Only
  # active with USE_FORECAST=1. Paper only — LIVE keeps legacy pricing. Revert: set KDE_SHRINK=1.0.
  # PROB_FLOOR 0.12→0.03: bin/deprecated/backtest_real.py floor-sweep (QUARANTINED) shows the 12% floor only
  #   hurts the good model's Brier (clamps accurate tails); trading floor is already
  #   enforced by risk min_price_cents=20. MARKET_BLEND 0.30/0.50→0.15/0.25: the heavy
  #   market blend was a band-aid for the broken (Brier 0.308) model; trust the good
  #   model more (forward-validated by bin/shadow_score.py). Paper only; live = defaults.
  KALSHI_WEATHER_USE_FORECAST=1 \
    KALSHI_WEATHER_KDE_SHRINK="${KALSHI_WEATHER_KDE_SHRINK:-0.4}" \
    KALSHI_WEATHER_PROB_FLOOR="${KALSHI_WEATHER_PROB_FLOOR:-0.03}" \
    KALSHI_WEATHER_MARKET_BLEND_BASE="${KALSHI_WEATHER_MARKET_BLEND_BASE:-0.15}" \
    KALSHI_WEATHER_MARKET_BLEND_MAX="${KALSHI_WEATHER_MARKET_BLEND_MAX:-0.25}" \
    python3 bin/build_fair_values.py --series "$SERIES" --out fair-values-forecast.json 2>&1 || echo "(forecast-build failed; paper will fall back to legacy)"

  # 2026-06-29 (Stage-1 gate rebuild): THIRD build — forecast at LIVE knobs →
  # fair-values-forecast-live.json. IDENTICAL to the LIVE climatology build (line ~83)
  # EXCEPT KALSHI_WEATHER_USE_FORECAST=1 — no paper KDE/floor/blend overrides — so the gate's
  # forecast-edge arm (this file) vs climo-edge (fair-values.json) isolates EXACTLY the
  # USE_FORECAST flip we'd ship to live, with NO knob confound (the paper-tuned
  # fair-values-forecast.json above mixes forecast with the KDE/floor/blend tuning, so it
  # can't measure the model variable alone). Feeds bin/validate_forecast_gate.py. GEFS is
  # hour-cached (cache/gefs-<hr>.json) so this build reuses the fetch and is cheap.
  echo "--- 1c. Build fair values (forecast at LIVE knobs — Stage-1 gate isolation)"
  KALSHI_WEATHER_USE_FORECAST=1 \
    python3 bin/build_fair_values.py --series "$SERIES" --out fair-values-forecast-live.json 2>&1 || echo "(forecast-live build failed; gate forecast arm uses stale file)"

  echo
  echo "--- 2-5. Run all enabled PAPER A/B variant arms (live arm already traded in step 1a)"
  # Each arm = a parallel paper book (state/<dir>) trading the SAME live order books
  # with its own env/argv. Arms are declared in variants.json; the runner does, per arm:
  #   settle -> [postmortem] -> trade -> report -> [dashboard]
  # Only the 'main' arm opts into postmortem (writes the shared LESSONS.md) and
  # dashboard (writes dashboard/data.json), so behaviour matches the old hand-written
  # blocks. Compare arms with: python3 bin/compare_variants.py
  # --exclude-live: the live arm already placed its real order in step 1a (live-first split,
  # 2026-07-06); excluding it here prevents a second real order this cycle. Paper arms unchanged.
  python3 bin/run_variants.py --exclude-live 2>&1 || true

  echo
  echo "--- 6. Generate aggregate dashboard"
  python3 bin/generate_live_data.py 2>&1 || true
  python3 bin/build_dashboard.py 2>&1 || true

  echo
  echo "--- 6b. A/B comparison (bootstrap CIs + Brier skill significance + pairwise-vs-baseline)"
  # --baseline premium adds the rigorous matched-market 'is arm X significantly better than the
  # premium baseline' test (shared outcome noise cancels) + flags within-noise, so a flip decision
  # is never made on a point estimate (2026-06-28).
  python3 bin/compare_variants.py --baseline premium 2>&1 || true

  echo
  echo "--- 6b2. Refresh GLOBAL model calibration (model-calibration.json; distinct from per-arm traded BSS)"
  python3 bin/model_brier.py --save 2>&1 | tail -2 || true

  echo
  echo "--- 6b3. Out-of-sample isotonic calibration gain (in-sample vs held-out; flags overfit optimism)"
  python3 bin/calibration_oos.py 2>&1 | tail -5 || true

  echo
  echo "--- 6c. Premium execution/fill quality (fill rate, adverse selection)"
  python3 bin/premium_fill_report.py --state-dir state/paper-premium 2>&1 || true

  echo
  echo "--- 6d. Fill-model calibration vs live (recommends ADVERSE_MARGIN/DEPTH_FRAC once live fills accrue)"
  python3 bin/reconcile_fills.py 2>&1 || true

  echo
  echo "--- 6d2. Live fill QUALITY watchdog (early adverse-selection / negative-edge alert)"
  python3 bin/live_fill_quality.py 2>&1 | tail -3 || true

  echo
  echo "--- 6d3. Live fill-edge breakdown (realized edge + fill-rate sliced by side/city/type/lead → fill-edge-breakdown.json)"
  # Standing instrument: turns the manual 2026-06-25 YES/NO dig into a per-cycle snapshot the
  # dashboard + the YES-haircut A/B read. Read-only (no orders); --write persists the JSON.
  # tail -64 (was -28): the report is ~57 lines and tail cuts the TOP — which held the OVERALL
  # line and the new NOT-DECISION-GRADE header (the whole point of the 2026-07-09 labeling).
  python3 bin/fill_edge_breakdown.py --write 2>&1 | tail -64 || true

  echo
  echo "--- 6e. Live fill milestone (alerts once at >=30 live fills → reconcile is ready)"
  python3 bin/check_fill_milestone.py 2>&1 || true

  echo
  echo "--- 6e2. Pre-registered futility checkpoints (premium-live edge @ n_events 200/400; report+alert only)"
  python3 bin/check_futility_checkpoint.py 2>&1 || true

  echo
  echo "--- 6e2b. YES live-skip lever decision (pre-registered gate; report+alert only, NEVER trades)"
  # Re-runnable reproduction of the frozen 2026-07-05 packet (whose scratchpad script was deleted).
  # Contract-weighted event-clustered gate; alerts ONLY when the decision becomes decidable
  # (≥40 post-fix YES events) or the gate fires — not every cycle. Report-only; the lever flip
  # (KALSHI_WEATHER_PREMIUM_YES_SIZE_MULT) stays an operator decision. → state/live-premium/yes-lever-state.json
  python3 bin/yes_lever_decision.py --write 2>&1 | tail -26 || true

  echo
  echo "--- 6e2c. Pocket-lever decision (pre-registered 13-pocket family; WY FWER; report+alert only)"
  # 2026-07-09: the governed replacement for "selection tightening off fill_edge_breakdown slices"
  # (per-contract, multiplicity-uncorrected — the trap that produced the stale 'same-day −4c' brief).
  # Every pocket is clamped to HOLD until the futility n=200 verdict exists; a fire is an operator
  # decision and requires a --fix-cutoff regime reset. Fingerprint-guarded: full bootstrap only
  # when new settlements landed. → state/live-premium/pocket-lever-state.json
  python3 bin/pocket_lever_decision.py --write 2>&1 | tail -40 || true

  echo
  echo "--- 6e2d. Quote-refresh flip panel (2026-07-10 flip; >10c-drop question; REPORT ONLY, no revert)"
  # Splits the live settled EV at the flip and evaluates the pre-registered >10c-drop trigger the
  # MATCHED (comparable-length pre-window) way — it does NOT fire, the flip is KEPT — with the
  # all-time drop shown only as confounded context and the A/B SIDE-SPLIT as the real tell (loss on
  # the correlated NO side, not a refresh-execution effect). Never reverts. → refresh-flip-report.json
  python3 bin/refresh_flip_report.py --write 2>&1 || true

  echo
  echo "--- 6e3. Markout kill-test (adverse selection vs captured half-spread; read-only verdict)"
  python3 bin/markout_kill_test.py 2>&1 || true

  echo
  echo "--- 6e4. Paper-vs-live gap tracker + decomposition (execution/markout vs fill-selection → live-paper-gap.json)"
  python3 bin/live_paper_gap.py --write 2>&1 | tail -12 || true

  echo
  echo "--- 6e5. Spread-cushion calibration trigger (fires PREMIUM_MIN_SPREAD review once data confidently supports it)"
  python3 bin/spread_cushion_calibration.py 2>&1 | tail -10 || true

  echo
  echo "--- 6e6. Feed-degradation canary (field-level 'real today, 0 tomorrow' watch → state/feed-canary.jsonl)"
  # Watches the load-bearing Kalshi feed scalars (balance, positions, settlements rows,
  # resting orders, fair-values) for the known silent-degradation mode where a field reads
  # real today and 0/null tomorrow (VALIDATION-2026-07-01: settlements count/cost are ALREADY
  # uniformly 0 — the canary deliberately monitors rows/nonzero_revenue_rows instead).
  # Strictly read-only except appending its own state/feed-canary.jsonl; alerts itself via
  # trader.notify only when a previously non-zero field stays 0/null for >=2 consecutive
  # cycles (--min-streak default 2), so one transient empty window never fires.
  python3 bin/feed_canary.py --dir live-premium 2>&1 || true

  echo
  echo "--- 6f. Forward edge: model-vs-market shadow score → state/shadow-score.jsonl (per-city time series)"
  python3 bin/shadow_score.py --log --no-fetch 2>&1 | tail -4 || true

  echo
  echo "--- 6f2. Leak-free skill recompute (post-guard window + same-day-excluded, event-clustered; cache-only)"
  python3 bin/leakfree_skill.py --no-fetch 2>&1 | tail -12 || true

  echo
  echo "--- 6f3. Persistence same-day-leak probe (exposure + guard verification + skill-inflation; read-only)"
  python3 bin/persistence_leak_probe.py 2>&1 | tail -10 || true

  echo
  echo "--- 6f4. Price-aware P&L replay (model-edge -> dollars, leak-free vs contaminated, event-clustered; read-only)"
  python3 bin/pnl_replay.py 2>&1 | tail -8 || true

  echo
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) cycle end ==="
} | tee -a "$LOG"

# Main cycle completed — disarm the "early abort" alert in the EXIT trap.
_cycle_ok=1

# Legacy live block (no-edge climatology model, $510 book at state/live).
# 2026-06-21: LIVE=1 now trades PREMIUM only — the premium-live arm runs inside
# bin/run_variants.py above (gated on LIVE=1). This legacy block is OPT-IN via
# LIVE_LEGACY=1 so it no longer double-trades alongside premium. Reversible.
if [ "${LIVE_LEGACY:-0}" = "1" ]; then
  LIVE_STATE="$ROOT/state/live"
  echo "=== Syncing live book from Kalshi ===" | tee -a "$LOG"
  KALSHI_WEATHER_STATE_DIR="$LIVE_STATE" python3 bin/sync_live_positions.py 2>&1 | tee -a "$LOG" || true
  echo "=== LIVE MODE: placing real orders (\$510 Kalshi, 25¢ YES / 20¢ NO edge, \$63/day stop) ===" | tee -a "$LOG"
  KALSHI_WEATHER_STATE_DIR="$LIVE_STATE" \
    KALSHI_WEATHER_MIN_EDGE_CENTS=25 \
    KALSHI_WEATHER_NO_EDGE_MULTIPLIER=0.8 \
    KALSHI_WEATHER_DAILY_MAX_LOSS_DOLLARS=63 \
    KALSHI_WEATHER_MIN_BID_QTY=1 \
    KALSHI_RESEARCH_MODE_ALLOW_RED=1 \
    python3 bin/paper_trade.py --fair-values fair-values.json --mode mm --max-orders 2 --live --state-dir "$LIVE_STATE" 2>&1 | tee -a "$LOG" || true
fi

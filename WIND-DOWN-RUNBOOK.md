# Live wind-down runbook (STOP-FOR-FUTILITY)

Prepared 2026-07-13. Use when the pre-registered futility checkpoint returns **STOP** — expected at
n_events=200, ~2026-07-19/20 (currently n=146, ~8.4 events/day, live edge −0.01¢/ct [−4.59, +4.52]).

STOP is the rule working, not a failure: ~$470 and ~150 events of live testing did not demonstrate a
positive premium-capture edge. Winding down live does **not** stop research — the paper A/B arms
(quote-refresh, pockets, gate arms) keep running.

## Decision trigger
`state/live-premium/futility-checkpoint-state.json` shows the n=200 checkpoint with `verdict = STOP`
(CI lower bound ≤ 0), surfaced in the cycle log at step 6e2 and alerted. Confirm the verdict is the
n=200 one (not an interim print) before acting.

## Procedure

1. **Halt new exposure** (blocks live arms from opening anything):
   ```
   python3 bin/halt_live.py --on "STOP-FOR-FUTILITY n=200 <date>"
   ```
   Writes `state/LIVE_HALT.json`. After this, healthcheck check 8 (`live_halt_persistent`) will WARN
   that live is halted — that is **expected and correct** here, not an incident.

2. **Cancel resting orders** (dry-run first, then execute):
   ```
   python3 bin/flatten_live.py            # review what would be cancelled
   python3 bin/flatten_live.py --execute  # REAL: cancel every resting live order
   ```

3. **Open positions** — there is NO automated close (`flatten_live.py --flatten-positions` is not
   implemented). Two options:
   - **Let them settle (default).** These are daily weather bins; the open book settles out naturally
     within ~1–2 days. With new orders halted (step 1) and resting orders cancelled (step 2), exposure
     only decreases from here. This is the low-effort, low-slippage path.
   - **Close immediately (only if you want exposure to zero now).** There is no bot path — close each
     position by hand on the Kalshi web account (sell into the book). Reconcile the list first with
     `python3 bin/triangulate_balance.py --dir live-premium` (Leg A/B/C) and the Kalshi web account
     (Leg D). Expect to cross the spread on exit.

4. **Verify wound down:**
   - `python3 bin/halt_live.py` (no args) → `🛑 HALTED`.
   - Next live slot (:20) logs `premium-live placed NO orders` / halt-skip — no new fills.
   - `python3 bin/triangulate_balance.py --dir live-premium` → legs agree; positions shrinking toward 0.

5. **Keep research alive:** do nothing to the paper arms. The quote-refresh flip stays as-is (report
   panel 6e2d), pocket/yes levers keep reporting. Live being halted does not touch paper.

## After STOP
- Write the live postmortem (`postmortems/`): final realized P&L, the correlated same-side variance
  story, and what the same-side cap changed.
- **Before any re-test**, decide whether to first address the open model-quality finding: the hot-city
  forecast bias (HOU/PHX/MIA run ~2–3.5°F too warm) is under-corrected by the ±2°F `get_bias` cap
  (model/error_tracker.py:169). Not the proven cause of the drawdown (variance dominates at this n),
  but a real mispricing that would compound at scale — see the 2026-07-13 audit follow-up.
- To resume later: `python3 bin/halt_live.py --off` (live resumes next cycle). Only do this with a
  concrete changed hypothesis, not to "try again" on the same instrument the gate just failed.

# bin/deprecated/ — quarantined backtests (DO NOT RUN)

Quarantined 2026-07-02 by quant-review experiment #10. These scripts are dead, look-ahead, or
non-representative of the LIVE code path. **Each refuses to run** — a `sys.exit()` guard fires
before any import (moving them out of `bin/` is not enough: the `automations/kalshi_weather ->
kalshi-weather` symlink keeps `kalshi_weather.*` importable, and two of them still *ran* before the
guard was added).

**Their output is NOT validation.** For any forward, leak-free measurement use the tools that score
the model's *actual logged predictions* against realized outcomes:

- `bin/shadow_score.py`          — forward model-vs-market Brier (no look-ahead)
- `bin/leakfree_skill.py`        — leak-free skill (post-guard window, same-day-excluded, event-clustered)
- `bin/persistence_leak_probe.py`— persistence same-day-leak instrumentation
- `bin/pnl_replay.py`            — price-aware model-edge → dollars, leak-free vs contaminated
- `bin/calibration_oos.py`       — out-of-sample (held-out) calibration gain
- `bin/model_brier.py`           — global model Brier / calibration refit

## Why each is here

- **backtest_v3.py** — **LOOK-AHEAD.** Its synthetic "forecasts" are noise centered on the realized
  truth (the docstring admits it "cheats by centering on the truth"), and it tests against AR(1)
  climatology-sampled "history," not real weather. It also crashes on import today (a broken
  `kalshi_weather.*` path — left broken on purpose; the guard makes it moot). Never trust its numbers.
- **calibrate_backtest.py** — calibrates v3's synthetic-noise model. Dead with v3; also crashes on import.
- **backtest_v2.py** — older climatology/persistence backtest, superseded by the forward tools above.
  It still *ran* before quarantine — exactly why the in-file guard (not just the move) is required.
- **backtest_real.py** — **NOT look-ahead** (it uses real archived Open-Meteo forecasts, ~12-18h lead,
  ~1.3°F error). It is quarantined only because it scores the `USE_FORECAST=1` KDE-shifted path, while
  **LIVE prices on climatology only (`USE_FORECAST=0`)** (see `run-cycle.sh` step 1). So its Brier/skill
  describe the PAPER build, not what live trades — and per the live-arm findings the live book prices
  off the *book*, not the model, so even a `USE_FORECAST=0` rebuild of it would not describe live
  trading. The paper param choices it once informed (`KDE_SHRINK=0.4`, `PROB_FLOOR=0.03`, `MARKET_BLEND`)
  are paper-only and documented at their call sites.

## Reversing / re-tuning

The quarantine is fully reversible: `git mv bin/deprecated/<file> bin/<file>` and delete the guard
block. If you genuinely need to re-sweep a PAPER param, do that on a branch — but prefer building a
new, cron-wired forward tool over resurrecting a backward backtest.

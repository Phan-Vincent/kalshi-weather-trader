# Kalshi Weather Trader — Profitability Roadmap

_Last updated: 2026-07-13. Evidence-gated. Every item has a hypothesis, an honest impact estimate, and
a pre-registered success/kill criterion. Numbers are event-clustered (city+date) with BCa CIs unless
noted. This document is deliberately skeptical: its job is to point effort at a real edge, not to
flatter the current book._

## Bottom line (read this first)

**The current live strategy has no edge — a measured, event-clustered zero.** Premium-capture
(market-making) is at **−0.01 ¢/ct, 95% CI [−4.59, +4.52], over 146 events**, realized **−$20.26 on
~$470**. Every positive number this system has ever shown — the +13 to +29 ¢/ct "edges" on the paper
arms — is a **fill-model artifact**: paper books ~37 contracts/market on good-side maker quotes the
live book (~4.3 contracts) never gets. `bin/live_paper_gap.py` decomposes the +12.98 ¢/ct paper-vs-live
gap into **~+11.55 ¢/ct "selection residual"** (P&L on fills that don't exist) and only ~1–1.5 ¢/ct
execution. **You cannot execute or size your way out of a zero. Do not treat paper P&L as real.**

**The one edge candidate — the model's same-day forecast skill — is an outcome leak, not alpha.** The
model beats the market on Brier (BSS +8.8%, n=453), but 100% of that edge lives in same-day markets and
is a clean zero one day out. That lead-time signature is exactly what a leak looks like. The P0-1 leak
audit confirms it directly on BOTH the shadow forecast log (`bin/leak_audit.py`) AND the actual quoted
bins (`bin/leak_audit_traded.py`) — independently reproduced and adversarially verified across 4 lenses
(confidence: high). Stratifying every settled prediction by hours-before the day's extreme,

  - **`legacy` (climatology — what LIVE trades): LEAK-DOMINATED.** Δbrier is **−0.0135 [−0.0175, −0.0091]
    with ≥6h lead** (model *worse* than the market when real forecast uncertainty remains) and only flips
    positive **<2h before / after the extreme** (+0.0123 [+0.0050, +0.0193]) — i.e. skill appears only
    once the temperature has been observed. Robust to ±2h shifts in the assumed extreme hour.
  - **`forecast` (the NWP/GEFS/NBM/dressing arm): NO GENUINE EDGE.** Leak-free Δbrier is a well-powered
    null, **−0.0001 [−0.0033, +0.0034] over 532 events.** And `bin/validate_forecast_gate.py` shows the
    forecast layer does **not** beat plain climatology (pairwise diff **+0.14 ¢/ct [−1.78, +4.06]**).
  - **Traded bins (what we actually quoted): null-to-negative, no edge.** Leak-free Δbrier −0.0266
    [−0.0456, −0.0079] over 215 events (model *worse* than market with real lead). The +0.023/+0.041
    headline reproduces only when scored against our own fill price and is 100% concentrated <2h before
    the extreme — a market-reference artifact stacked on a same-day leak. See P0-1 below.

**Even if a real edge is found, the ceiling is small.** These are among the thinnest markets on Kalshi:
flow-capped at ~200–600 ct/day and ~$1–2k working capital, per-event SD ~38 ¢/ct. Realistic profit
ceiling is **$150–700/month at full build-out, ~$1k/month only at unreachable throughput, and $0 until a
>4 ¢/ct edge is created and confirmed over ~5+ months** (~1,390 events to detect +4 ¢/ct). Plan around a
**side-income annuity, not a scalable business.** If that ceiling does not justify the operator's time,
that is the most important line in this document.

The whole roadmap hinges on **Phase 0: prove a leak-free, decision-time edge exists.** As of this
writing the evidence says it does not. Keep the book running minimal-cost as a plumbing benchmark, and
escalate to Phase 1 only if P0-1 turns up genuine ≥6h-lead skill.

> **STATUS 2026-07-14 — the edge search is exhausted within the daily-bin product.** P0-1 CLOSED with a
> LEAK verdict (shadow + traded). M3 — the last orthogonal lane (multi-day taker) — is **structurally
> dead**: Kalshi lists weather markets only ~0–1.7 days out, so there is nothing to trade at 2–4d. Every
> edge lane inside Kalshi daily high/low temp bins is now either a confirmed zero, a proven leak, or a
> non-existent market. **No untested edge lane remains in this product.** Live `premium-live` has been
> contained to **$2/market** (from $5) as a token benchmark. The passive P0-2/P0-3 durability reads still
> run to close the book out honestly, but they cannot manufacture an edge the product doesn't have.
>
> **CATALOG SCAN 2026-07-14 — the rest of Kalshi weather is picked clean too.** Enumerated all 288
> Kalshi "Climate and Weather" series → 36 tradeable non-daily products with real OI + two-sided quotes
> (monthly city rain, hurricane/seasonal counts, climate records/anomalies, drought, tornado). A 5-agent
> analysis (web-grounded forecast-skill + market-efficiency + testability, adversarially synthesized)
> found **every longer-horizon family fails the same structural triad**: (1) resolution value accumulates
> observably over the period → the *same leak* as daily temp, and it flows to all participants at once
> (SIG + quant MMs read the same NWS/NHC/Copernicus feeds) so it's priced, not exploitable; (2) the
> leak-free residual sits at period-start where forecast skill is weakest, and that skill is *published
> free* (CSU/NOAA hurricane outlooks, Berkeley/Copernicus climate odds, CPC precip) → the book anchors on
> it; (3) settlement is slow → a powered leak-free read takes 2 years (monthly rain) to ~150 years
> (annual records/hurricane). **Verdict: the longer-horizon venue is picked clean.** The *only* residual
> action with any value is a **single 12-month zero-capital kill screen on monthly city rain (KXRAIN*M)**
> — the one family that powers in ~2-3 yr AND whose leak is cleanly instrumentable (published banked
> month-to-date lets you decompose skill(full) − skill(banked-only)). Expected outcome: death. It can't
> CONFIRM an edge in <2-3 yr (past the worthlessness line) but it can KILL in ~12 months, converting
> "probably picked clean" into "proven picked clean" cheaply — the daily-bin discipline applied forward.
> Honest ceiling if any edge somehow survived: ≤ the daily-bin $150–700/mo, most likely ~$0 after spread.
>
> **KILL SCREEN BUILT + RUNNING (2026-07-14).** `bin/rain_screen_snapshot.py` (logger) +
> `bin/rain_screen_score.py` (read-out), tests `tests/test_rain_screen_snapshot.py` /
> `tests/test_rain_screen_score.py`. Wired into `bin/sync-live.sh` (`--once-daily`, `|| true`) so it
> freezes one daily snapshot per KXRAIN*M bin — Kalshi mid + three probs: MARKET, model FULL (bias-
> corrected GEFS ensemble for the remaining month), model BANKED-ONLY (gauge climatology). CRITICAL data
> discipline: banked + climatology come from the **ACIS gauge** the market settles on (CLIMDW, CLIHOU…),
> NOT gridded model precip (which carried a multi-inch basis — a bug caught and fixed during the build).
> The leak-free metric is the **month-open** snapshot only; the scorer runs the firewall
> skill(FULL)−skill(BANKED-ONLY) to prove any edge is forecast, not banked observation. Clock started;
> first scorable leak-free month settles after it closes. Expected read-out: KILL in ~12 months.
>
> **FIREWALL BACKTEST — timeline shortened for the KILL case (2026-07-14).** `bin/rain_firewall_backtest.py`
> (tests `tests/test_rain_firewall_backtest.py`). The model must beat climatology at month-open lead before
> the market comparison matters — a model-vs-climatology question needing NO market prices, so backtestable
> now. (The obvious quick path — Open-Meteo historical-forecast — was verified INVALID: it stitches
> ~1-day-lead forecasts with 30–50% grid-vs-gauge basis, faking skill.) The valid leak-free test, over
> **26 yr × 11 cities = 3,213 city-months / 17,993 live bins**, asks whether the month-open info beyond
> climatology (persistence + ENSO) improves the KXRAIN bins, scored on the ACIS gauge: **firewall −0.0164
> [−0.0194, −0.0135]** (conditioning is *worse* than climatology), **OOS anomaly R² −0.158**. There is NO
> leak-free month-open predictability in monthly rain. Combined with the published near-zero NWP month-open
> monthly-precip skill and the 30–50% grid-gauge basis, the model side is **very unlikely to clear the
> firewall — a KILL basis now.** CAVEAT: this does not test within-month synoptic skill (days 1–2 wk),
> which needs a GEFSv12 reforecast (rigorous path) — small for a monthly total and near-zero at month-open
> lead, so not worth the build unless the passive forward screen surprises. Net: treat the model side as
> dead on priors; the forward screen runs on as free confirmation.
>
> **GEFSv12 REFORECAST — escalation path SCAFFOLDED (2026-07-14).** `bin/gefsv12_reforecast.py` (tests
> `tests/test_gefsv12_reforecast.py`). Pulls the GEFSv12 reforecast APCP (2000–2019) from the public
> `noaa-gefs-retrospective` S3 bucket via byte-range GRIB2 (mirroring `data/gefs_ingester.py`), to close
> the one gap the persistence+ENSO firewall can't test — within-month synoptic skill at a genuine
> month-open init. Fetch validated live end-to-end (init 2005-07-01, HOU c00: 64 six-hour buckets =
> 40 Days:1-10 + 24 Days:10-16, day-1..16 total 2.905 in, ~15s). De-accumulation is a *filter* (keep
> disjoint 6h buckets, drop the interleaved 3h partials). CAVEATS baked in: a day-1 init reaches only 16d
> (Wednesdays reach 35d/11 members); grid→gauge bias needs the LOO multiplicative correction; the ~20GB
> 2000-2019 batch (`build_reforecast_month_open_table`) and the sibling rescorer (`rain_firewall_reforecast.py`
> that swaps the conditioned leg for the real ensemble) are documented TODOs — the scaffold is the fetch +
> de-accumulation + point-extract primitives, not the full batch. Expected outcome still a KILL; this
> exists to close the gap rigorously, not to resurrect the screen.
>
> **STRUCTURAL ARBITRAGE — the last non-forecast door, CLOSED (2026-07-14).** `bin/ladder_arb_check.py`
> (tests `tests/test_ladder_arb_check.py`) checks the one edge needing NO forecast skill: dutch-book locks
> within an event's own order book — monotonicity of the ">N" ladders (rain + temp thresholds) and the
> temp between+tail partitions summing to $1, netted against taker fees and capped by fillable top-of-book
> size. Live scan of **70 weather series: ZERO fillable, fee-covering arbitrage.** The closest cases (7
> low-temp events) are sell-all at **−2.00¢/ct net with ≈0 fillable size** — priced to the fee boundary
> with no crumbs (arb bots eat these in ms, as expected). So even the forecast-FREE path is dead: the
> venue is fully picked clean across forecast edge, market-making, AND structural arbitrage.

## 🛑 STOP / DE-PRIORITIZE (these cost money or attention for zero return)

1. **All premium-capture execution tuning** — quote-refresh, offset, band-width, skip-city, inside/
   passive/tightband. Every one polishes a confirmed-zero, adversely-selected maker book (fill-quality
   ADVERSE; markout flat +0.22 ¢). No requoting recipe recovers positive edge from a structural null.
   The deferred quote-refresh A/B → **read out and kill on the 2026-07-15 schedule, do not extend a
   third time** (its interim read already fails FWER).
2. **The data-mined paper pockets** (premium-skipsfo +29 ¢, premium-forecast +21 ¢, climo-edge +20 ¢) —
   post-hoc city/band slices on top of a fill model that overstates edge ~6×. Noise dressed as alpha;
   `variants.json` already annotates skipsfo/tightband "WITHIN NOISE".
3. **Multi-day (1-day+) forecast skill as a resting-quote edge** — a dead null vs the market (+0.003 /
   +0.009, CIs cover 0). The market already prices multi-day weather at least as well as the model.
   (Caveat: 2–4d *taker* trades are a separate live hypothesis — see M3 — do not conflate.)
4. **The isotonic calibration layer** — measurably overfit (in-sample +1.3%, 5-fold CV −0.0%,
   time-forward +0.2%). It overstates calibration and feeds false confidence into live pricing. Freeze
   or bypass (see M4).
5. **Adding NWP data sources** (more GEFS/NBM/ensemble members) until P0-2 shows the forecast layer
   beats climatology at all — right now it does **not**.

---

## 🚪 PHASE 0 — Prove there is an edge before building anything (THE GATE)

Nothing downstream is worth building until these resolve. Each is a pre-registered, falsifiable
experiment; run in shadow or at tiny size.

### P0-1 — Separate same-day forecast skill from outcome leak — **CLOSED (shadow + traded), verdict: LEAK** ⭐
- **Tools:** `bin/leak_audit.py` (shadow forecast-log) and `bin/leak_audit_traded.py` (the actual quoted
  bins) — tests `tests/test_leak_audit.py`, `tests/test_leak_audit_traded.py`. Both stratify settled
  predictions by hours-before the day's extreme and read model-vs-market Δbrier per stratum with
  event-clustered BCa CIs. Read-only. The traded tool joins the true decision-time market from
  forecast-log `market_mid` at the snapshot nearest `opened_utc` (median gap 0.4 min, 94% before open).
- **Hypothesis:** part of the same-day Brier edge is a legitimate faster-nowcast advantage (pricing
  intraday-observable temperature before the market adjusts), not leakage.
- **Success criterion:** leak-free (≥6h-before-extreme) Δbrier **excludes 0 positive at n≥150 events.**
- **Result (2026-07-14) — FAILS the gate in every well-powered configuration:**
  - **Shadow log:** `legacy` (climatology, what LIVE trades) = **LEAK-DOMINATED** (leak-free −0.0135
    [−0.0175, −0.0091], model *worse* than market with real lead; flips positive only <2h before/after
    the extreme). `forecast` = leak-free null **−0.0001 [−0.0033, +0.0034] over 532 events** (well-powered).
  - **Traded bins** (`paper`, mode=mm): leak-free Δbrier null-to-negative everywhere — all-history
    **−0.0266 [−0.0456, −0.0079], 215 ev** (well-powered, *excludes 0 negative*); `paper-caledge`
    **−0.0173 [−0.0364, +0.0036], 155 ev**; post-fix-only −0.0230 [−0.0488, +0.0032] (97 ev, consistent
    but underpowered on its own). No city/side/type/lead slice hides a robust positive leak-free pocket.
  - **Reconciliation of the +0.023/+0.041 headline:** it reproduces **only** when the model is scored
    against our own `entry`/fill price (a spread-discounted maker price ~19pp below the true mid), and
    even then it is **100% concentrated <2h before the extreme** (a same-day leak). Against the honest
    order-book mid the edge is null-to-negative; the 30–50% market blend baked into `fair_prob` actually
    *flatters* the model (pure-forecast deficit is larger, ~−0.046). So the headline = **a market-reference
    artifact stacked on a same-day outcome leak**; adverse selection fills us on the bins where the market
    was right. (Independently reproduced + adversarially verified across 4 lenses, confidence: high.)
- **Kill criterion — MET.** The same-day "edge" is an outcome leak, not forecast alpha. **There is no
  leak-free forecast engine to build. Do not green-light Phase 1.**
- **Remaining P0-1 work:** re-run across a regime change (see P0-3) as a durability check; the sign is
  not expected to reverse.

### P0-2 — Settle forecast-vs-climatology at live knobs (the fork in the road)
- **Tool:** `bin/validate_forecast_gate.py` (the `USE_FORECAST` A/B; same books, same-day excluded).
- **Current evidence:** forecast-edge BSS **−1.0% [−0.03, +0.03]** vs climo-edge **+12.0%**; pairwise
  matched diff **+0.14 ¢/ct [−1.78, +4.06]**. The edge that exists is carried by **climatology +
  distribution-sharpening, not the NWP stack.**
- **Success criterion:** forecast-edge BSS − climo-edge BSS **> 0, CI excludes 0, n≥150/arm** (currently
  n=15 matched — far short).
- **Kill criterion:** if forecast ≤ climo at n≥150, **freeze the NWP stack, ship climatology + sharpening
  as production, stop adding data sources.**

### P0-3 — Confirm the climatology edge isn't just summer
- **Test:** rolling OOS climo-edge BSS monitor; hold judgment until it spans ≥2 weather regimes. Track,
  don't retune on it. The +12% climo BSS is 74 events over ~3 summer weeks and may be seasonal.
- **Kill criterion:** if it decays to 0 outside July, the edge was seasonal — treat the annuity as
  seasonal and de-risk sizing.

**Phase 0 exit gate:** proceed to Phase 1 **only if P0-1 shows a decision-time, leak-free edge that
excludes 0.** If P0-1 stays LEAK-DOMINATED, the honest conclusion is *there is no proven durable edge* —
run the book minimal-cost as a benchmark, cap live premium-capture to contain the bleed, and revisit
only if a new information advantage appears.

---

## 🔧 PHASE 1 — Build the proven edge (start only after P0-1 passes)

Ranked by EV × P(real) × feasibility. Sizing is already correct — the work is (a) sharpen the model
where it demonstrably helps, (b) fix the calibration that turns Brier skill into P&L, (c) find the one
place the edge is fillable.

- **M1 — Ship + validate the over-smoothing (sharpening) fix.** `resolve_scale`/KDE over-smooth ~55–70%;
  CRPS-optimal bandwidth ×0.3–0.4. Best-replicated lever; plausibly the difference between forecast BSS
  ~0 and ~+0.09. Low effort (env knob `KALSHI_WEATHER_KDE_SHRINK`/`SCALE_FLOOR`). **Success:** sharpened
  arm beats **sharpened-climatology** (not just climo) by a CI-excludes-0 margin at n≥150. **Kill:** if
  it only beats raw climo, the value is in sharpening — apply it to the climo model, drop the forecast
  layer.
- **M2 — Fix tail calibration** (bias-cap + station corrections + overconfidence). Overconfidence ratio
  **3.72**; the model loses exactly on threshold markets (live HIGH/T −14.99 ¢/ct, LOW/B −11.99 ¢/ct)
  and under-corrects HOU/PHX/MIA (BIAS_CAP=2°F, ~19% of bins, currently *negative* forecast Brier).
  Recovers ~+2 to +4 ¢/ct of leaked P&L — this is what makes a real Brier edge survive settlement.
  **Success:** OOS overconfidence <1.5, HOU/PHX/MIA Brier no longer negative, threshold-market live P&L
  improves at n≥40. **Kill:** if OOS calibration gain ≈ 0 (like the current isotonic layer), stop —
  you're overfitting the fix.
- **M3 — 2–4 day *takeable* thin markets — ✗ STRUCTURALLY DEAD (2026-07-14).** Was the one orthogonal,
  leak-proof, taker-not-maker lane left. Probed with `bin/m3_multiday_taker.py` (tests
  `tests/test_m3_multiday_taker.py`): **the forecast log has 0 snapshots beyond 1 day out over 404k
  predictions**, and Kalshi's live API confirms daily high/low weather markets list **only ~0–1.7 days
  out** (12 open markets/city, none >2d). There is nothing to trade at 2–4d lead — the market doesn't
  exist. This closes the last edge lane within the daily-bin product. The only way to revive a
  multi-day thesis is a **different, longer-horizon weather/climate product** (a new-strategy project,
  not a tweak) — worth a catalog scan of Kalshi's longer-dated weather series if edge-hunting continues.
- **M4 — Replace the overfit isotonic layer with identity/per-city calibration.** ~0 direct edge; an
  honesty/simplification fix that removes a false confidence signal and live mispricing. **Success:**
  identity (or per-city) matches/beats isotonic on time-forward Brier; default to identity on ties.
- **M5 (lower) — Regime/tail handling for PHX monsoon + MIA.** Variance reduction, not edge: removes the
  largest single-event losers (a −15°F miss on a max-size tail bin is a correlated blow-up). **Kill:** if
  regime detection doesn't cut tail loss OOS, just abstain on PHX/MIA extremes.

---

## 📈 PHASE 2 — Scale within hard capacity limits (only if Phase 1 confirms a live edge)

**The sizing stack does not need rebuilding.** Quarter-Kelly + calibrated down-shrink + per-event/
correlation caps (`trader/orders.py`, `risk.py`, `scanner.py`) is already correct and conservative. The
scaling problem is not sizing — capacity is flow-capped and bankroll-independent.

- **L1 — Accept the capacity ceiling and build to it.** ~200–600 ct/day absorbable (4–10× current 57),
  hard working-capital ceiling ~$1–2k. Fills are gated by passive sell-flow crossing your bid, **not by
  capital** — a real edge here is a **linear $/day annuity, not Kelly compounding** (200 ct/day × +4 ¢ ≈
  $8/day). Deploy incrementally, measuring fill-quality at each step; do not add capital past ~$1–2k.
- **L2 — Make risk controls bankroll-proportional before scaling.** The 8-events/side correlated cap
  (armed live 2026-07-13) and the $20/$60 stops were set at a $469 bank and will halt a scaled book
  constantly. Auto-scale stops and per-event caps with bankroll; keep the correlated same-side cap armed.
  The cap that controls correlated variance *directly fights throughput* — that trade-off is fundamental.
- **L3 — Honest expectations + review cadence.** Ceiling **$150–700/month** realistic, **~$1k/month**
  only at unreachable throughput, **$0** until a >4 ¢/ct edge is confirmed. Honor the 200- and 400-event
  futility checkpoints in `futility-checkpoint-state.json` (GO only if CI-low > 0, else STOP).

---

## Priority ranking (EV × P-real × feasibility)

| Rank | Item | Why | If it fails |
|---|---|---|---|
| **1** | **P0-1** Separate same-day skill from leak (`bin/leak_audit.py` + `bin/leak_audit_traded.py`) — **CLOSED: LEAK** | The whole thesis; everything waits on it | ✅ done — verdict LEAK → no engine, run book as benchmark |
| **2** | **P0-2** Forecast vs climatology at live knobs | Decides whether to keep the NWP stack | Freeze NWP, ship climo+sharpening |
| **3** | **M1** Ship + validate sharpening | Best-replicated lever; ~0→+0.09 BSS; low effort | Sharpening applies to climo instead |
| **4** | **M2** Fix tail calibration | Converts Brier skill into surviving P&L | Stop; you're overfitting the fix |
| **5** | **P0-3** Climo-edge durability | Guards against a summer-only mirage | Treat annuity as seasonal |
| **~~6~~** | ~~**M3** 2–4d takeable thin markets~~ **✗ STRUCTURALLY DEAD** | Was the last edge lane | Kalshi lists no weather markets >1.7d out — nothing to trade |
| **7** | **M4** Kill overfit isotonic | Honesty/simplification | N/A — strict cleanup |
| **8** | **M5** PHX/MIA regime handling | Variance reduction on blow-ups | Just abstain on extremes |
| **—** | **STOP** premium tuning, mined pockets, multi-day resting quotes | Zero or negative return | — |

**One sentence:** you have a *possible* same-day forecast edge worth maybe a few ¢/ct *if it survives the
leak test* — and as of 2026-07-13 the leak test says it does not — in markets that cap you at a few
hundred dollars a month, so spend effort proving the edge is real (Phase 0), not polishing the execution
of a strategy that has none.

---

## Beyond weather — repurpose the machinery (2026-07-14)

Kalshi weather is picked clean across all three edge types (forecast, market-making, structural arb). The
only edge that ever made money was a MANUAL call (the Iran NO bet). So the honest next use of this
infra is to point it at discretionary trades — not to generate an edge (it can't), but to SIZE and
above all MEASURE whether the operator's own calls have one, with the same rigor that killed the weather
model. Starter: **`bin/discretionary.py`** (`log`/`list`/`score`, tests `tests/test_discretionary.py`).
It reuses `trader/orders.kelly_qty` (sizing), `_kalshi_taker_fee_cents` (fees), and
`compare_variants.cluster_bootstrap_ci` (event-clustered CI). `log` records a decision-time P(win) +
thesis and suggests a quarter-Kelly size (does NOT place orders); `score` grades settled calls on Brier
skill vs the market + calibration + a pre-registered futility gate (≥20 settled calls before any edge
ruling). Live-validated: logging the Iran NO at P=0.85 correctly returned **edge −0.07, size 0** — the
NO side is now 93¢, so adding is negative-edge. This is a measurement/discipline tool, not a money
machine: it tells the operator the truth about their own edge, cheaply, before they bet big — and I
build the machinery, I do not pick trades.

---

_Provenance: synthesized from a 5-agent read-only analysis (edge-inventory, forecast-quality,
strategy-execution, capacity-sizing, synthesis) plus direct runs of `bin/leak_audit.py`,
`bin/leak_audit_traded.py`, `bin/validate_forecast_gate.py`, `bin/live_paper_gap.py`, and
`bin/compare_variants.py`. The P0-1 traded-bin closure was independently reproduced and adversarially
verified by a second 5-agent workflow (reproduce + adverse-selection / market-reference / stats lenses +
synthesis; verdict HOLDS, confidence high, 2026-07-14). See `memory/kalshi-profitability-roadmap.md`._

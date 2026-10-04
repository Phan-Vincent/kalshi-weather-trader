---
name: kalshi-pnl
description: Report the Kalshi weather bot's realized P&L, live fill quality, edge breakdown, live-vs-paper gap, and fill milestone. Use when the user asks about kalshi P&L, "how much did we make/lose", "fill quality", "are we making money", "edge", or "how many fills".
argument-hint: "[--calibration]  (add --calibration to also include the Brier/skill report)"
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — P&L & performance

Run from the trader root and give a plain-language readout. Root:
`/Users/you/.openclaw/workspace/automations/kalshi-weather`

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/fill_edge_breakdown.py --live-dir state/live-premium   # overall realized EV/ct + realized $, sliced
python3 bin/check_fill_milestone.py --live-dir state/live-premium  # live fill count vs 30/100
python3 bin/live_fill_quality.py --live-dir state/live-premium     # OK/ACCUMULATING/WATCH/ADVERSE
python3 bin/live_paper_gap.py                                      # paired live-vs-paper gap (needs >=5 matched events)
```

Only if the user passes `--calibration`, also run:

```bash
python3 bin/brier_report.py       # Brier Skill Score vs market on the PAPER book (positive = beat market)
```

## Reporting

- **Lead with realized LIVE numbers**: overall EV-after-fee/ct, realized $, fill count.
  The live book is `state/live-premium/`.
- **Do not quote paper P&L as if it were live** — paper is a ~6×-inflated proxy for
  live edge. If you cite paper (e.g. from `brier_report`/`live_paper_gap`), label it.
- The account also holds non-weather positions (sports, an Iran contract), so raw
  account equity ≠ weather-trader performance. Use the reporters' realized figures.
- Flag `WATCH`/`ADVERSE` fill quality or a negative live EV explicitly; note the
  sample size (edge on this book is still statistically thin — CIs often span 0).
- `model_vs_market.py`'s magnitude is self-flagged UNRELIABLE — only cite it directionally.

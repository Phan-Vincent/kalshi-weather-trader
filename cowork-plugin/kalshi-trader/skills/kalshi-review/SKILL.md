---
name: kalshi-review
description: Run the Kalshi weather bot's nightly review and summarize it. Use when the user asks for the "kalshi review", "nightly review", "summarize today's trading", or "end of day report".
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — nightly review

Run the trader's own nightly review, then summarize the key takeaways for the user.

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
bash bin/nightly-review.sh
```

This wraps `bin/nightly-review.py` and produces the review write-up (check `reviews/`
and `postmortems/` for any file it emits, and the stdout it prints).

## Reporting

- Summarize: today's realized P&L / fills, calibration/skill signal, any anomalies or
  alerts, and any open decisions the review surfaces.
- If the review writes a file under `reviews/`, tell the user the path so they can open it.
- Read-only — this does not change any trading state.

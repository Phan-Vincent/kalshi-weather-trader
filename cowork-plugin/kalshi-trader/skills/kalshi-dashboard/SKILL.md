---
name: kalshi-dashboard
description: Refresh the Kalshi weather bot's dashboard data and give the user the dashboard URL. Use when the user asks to "show the trading dashboard", "open the kalshi dashboard", "refresh the charts", or "see the trader dashboard".
allowed-tools: [Bash, Read, Grep, Glob]
---

# Kalshi trader — dashboard

A launchd job (`com.vincentphan.kalshi-dashboard`) already serves the dashboard at
**http://localhost:8774**. Refresh its underlying data, confirm the server is up, then
hand the user the URL.

```bash
cd /Users/you/.openclaw/workspace/automations/kalshi-weather
python3 bin/generate_live_data.py        # refresh live dashboard JSON
python3 bin/generate_dashboard_data.py   # refresh paper dashboard JSON
# (only if the static HTML itself needs rebuilding): python3 bin/build_dashboard.py

# confirm the server is up:
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8774 || echo "dashboard server not responding"
```

## Reporting

- Give the user the clickable URL: **http://localhost:8774**.
- If the server isn't responding, say so and note it's the `com.vincentphan.kalshi-dashboard`
  launchd job (check `launchctl list | grep kalshi-dashboard`); as a fallback the user can
  run `python3 -m http.server 8000 --directory dashboard` from the trader root.
- Read-only — refreshing data does not touch trading.

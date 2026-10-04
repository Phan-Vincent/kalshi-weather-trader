# Kalshi Trader Dashboard — Modern

A comprehensive, modern local dashboard for monitoring Kalshi prediction market trading performance.

## Features

### 📊 Dashboard
- **Live P&L** with period switching (Live/Daily/Weekly/Monthly/Yearly)
- **Win Rate** with win/loss breakdown
- **Total Trades** count with open positions
- **Balance & Portfolio Value** from live Kalshi API

### 📈 Charts
- **Cumulative P&L** over time (line chart)
- **Win/Loss Distribution** (doughnut chart)
- **Drawdown** analysis (peak-to-trough)
- **Daily P&L** bars (last 30 days)
- **Weekly & Monthly** performance bars
- **Price Bucket Analysis** (performance by entry price)
- **Hourly Performance** (best trading hours)

### 📋 Positions
- **Live positions** from Kalshi API (auto-refresh)
- Position cards with ticker, side, qty, entry, unrealized P&L
- Resting orders count and fees paid

### 🎯 Performance Analytics
- Average win/loss
- Profit factor
- Expectancy
- Sharpe-like ratio
- Max drawdown

### 🧠 Model Statistics
- **Brier Skill Score** vs market baseline
- Rolling Brier score (50-trade window)
- Calibration curve (predicted vs actual)
- Settled/open trade counts

### 🛡️ Risk Monitor
- Max drawdown with visual bar
- Consecutive losses with circuit breaker threshold
- Sharpe-like ratio
- City risk breakdown table
- Streak tracking (current, max win, max loss)
- Halted cities count

### 🌐 Markets
- Available Kalshi markets listing

### 📚 Documents
- Quick access to README, Lessons, Risk Notes, Audit Report

### 📝 Logs
- Recent settlement log entries

## Setup

### 1. Install Dependencies

```bash
# Ensure kalshi-cli is installed and authenticated
kalshi-cli auth status

# Python 3.8+ required
python3 --version
```

### 2. Generate Data

```bash
cd ~/.openclaw/workspace/automations/kalshi-weather/dashboard-modern

# Generate dashboard data from state files + live API
python3 generate_data.py
```

### 3. Start Dashboard

```bash
# Start the server
python3 serve.py

# Or specify a port
python3 serve.py 9000
```

### 4. Open in Browser

Navigate to: http://localhost:8080

## Auto-Refresh

The dashboard auto-refreshes data every 60 seconds. Click the **Refresh** button for manual updates.

## Data Files

| File | Description |
|------|-------------|
| `data/data.json` | Historical analytics (settlements, brier, risk, etc.) |
| `data/data-live.json` | Live positions and balance from Kalshi API |

## Cron Job

Add to your crontab for automatic data updates:

```bash
# Update dashboard data every 5 minutes
*/5 * * * * cd ~/.openclaw/workspace/automations/kalshi-weather/dashboard-modern && python3 generate_data.py >> /tmp/kalshi-dashboard.log 2>&1
```

## Architecture

```
dashboard-modern/
├── index.html          # Main dashboard UI (single-page app)
├── generate_data.py    # Data generator (state + live API)
├── serve.py            # Simple HTTP server
├── data/
│   ├── data.json       # Historical analytics
│   └── data-live.json  # Live API data
└── README.md
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| 1-6 | Switch pages (Dashboard, Positions, Performance, Model, Risk, Markets) |
| R | Refresh data |

## Customization

Edit `index.html` to customize:
- Color scheme (CSS variables in `:root`)
- Chart colors and styling
- Additional metrics or panels
- Refresh interval (default: 60s)

## Troubleshooting

### "No live data" message
- Run `python3 generate_data.py` to populate data files
- Ensure `kalshi-cli` is authenticated: `kalshi-cli auth status`

### Charts not loading
- Check browser console for errors
- Ensure Chart.js CDN is accessible

### Data not updating
- Check that state files exist in `../state/`
- Verify `kalshi-cli --prod portfolio balance` works

## Security Notes

- Dashboard is served locally only (localhost)
- No data leaves your machine
- Kalshi API credentials are handled by `kalshi-cli` (secure keyring storage)

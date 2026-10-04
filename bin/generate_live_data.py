#!/usr/bin/env python3
"""Generate dashboard/data-live.json from live Kalshi prod API (v5 - enhanced)."""
import json, subprocess, os, sys, time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(ROOT, "dashboard")
# The OpenClaw canvas serves a COPY of the dashboard (viewable from other devices, so it
# can't just redirect to the localhost:8774 server). It went stale 2026-06-19 because nothing
# republished it; this script now mirrors the public bundle there every run (see _publish_canvas).
CANVAS_DIR = os.path.expanduser("~/.openclaw/canvas/kalshi-weather")
CANVAS_FILES = ("index.html", "data.json", "data-live.json", "live-history.json", "ab-data.json")
_DEFAULT_INITIAL_CENTS = 100000  # Synthetic public example; private state overrides it.

# Single source of truth for weather-vs-personal: the SAME anchored regex the live book uses
# (sync_live_positions.py), so the displayed positions/portfolio and the ledger-derived P&L agree.
# A raw substring test would leak any personal bet whose ticker merely contains HIGH/LOW/TEMP
# (KXFEDLOWER, KXALLTIMEHIGH, KXHIGHESTGROSSING, KXHIGHCOURT, ...).
sys.path.insert(0, ROOT)
from data.weather_data import is_weather_ticker

def _atomic_write_json(path, obj, indent=2):
    """QA-18 (2026-07-01): tmp + os.replace so a concurrent reader — or the */15 dashboard cron
    colliding with run-cycle's step-6 call at :00 — never sees a torn/half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent)
    os.replace(tmp, path)


def _publish_canvas():
    """Mirror the dashboard's public files into the OpenClaw canvas dir (whitelist only —
    never .bak/check.py clutter). Copies via tmp + os.replace so canvas readers never see a
    torn file; skips files already current (copy2 preserves mtime). Fail-soft: publishing is
    presentation-only and must never break the data generation this cron exists for."""
    if not os.path.isdir(CANVAS_DIR):
        return
    import shutil
    for name in CANVAS_FILES:
        src = os.path.join(DASHBOARD_DIR, name)
        dst = os.path.join(CANVAS_DIR, name)
        try:
            if not os.path.exists(src):
                continue
            s = os.stat(src)
            try:
                d = os.stat(dst)
                if int(d.st_mtime) == int(s.st_mtime) and d.st_size == s.st_size:
                    continue
            except OSError:
                pass
            tmp = f"{dst}.tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except Exception as e:
            print(f"[live-data] canvas publish of {name} failed: {e}", file=sys.stderr)


def compute_weather_equity(initial_cents, realized_cents, open_cost_cents, weather_portfolio_cents):
    """Compute weather equity from its ledger, excluding unrelated account activity.

    Available = starting bank + weather realized P&L - open weather cost.
    Total = available + weather market value; P&L = total - starting bank.
    Returns (available_cents, total_cents, pnl_dollars, pnl_pct).
    """
    available = initial_cents + realized_cents - open_cost_cents
    total = available + weather_portfolio_cents
    pnl = round((total - initial_cents) / 100, 2)
    pnl_pct = round((total - initial_cents) / initial_cents * 100, 2) if initial_cents > 0 else 0
    return available, total, pnl, pnl_pct


def _weather_realized_cents(root):
    """Canonical weather realized P&L (cents), from the live book's settlement-reconciled ledger —
    the same +775¢ the rest of the system trusts. Falls back to summing the settlement log, then 0.
    NEVER reads book.cash_cents (that is the contaminated account-global available)."""
    book_path = os.path.join(root, "state", "live-premium", "paper-book.json")
    try:
        with open(book_path) as f:
            return int(json.load(f).get("realized_pnl_cents", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    log_path = os.path.join(root, "state", "live-premium", "settlement-log.jsonl")
    try:
        total = 0
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    total += int(json.loads(line).get("pnl_cents", 0))
        return total
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _parse_kalshi(stdout):
    raw = (stdout or "").strip()
    if raw:
        try: return json.loads(raw)
        except json.JSONDecodeError: pass
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try: return json.loads(line)
            except json.JSONDecodeError: continue
    return None

def run_kalshi(*args):
    last = None
    for attempt in range(3):
        try:
            r = subprocess.run(["kalshi-cli", "--prod", *args],
                               capture_output=True, text=True, timeout=30)
            parsed = _parse_kalshi(r.stdout)
            if parsed is not None:
                return parsed
            last = f"no JSON in output (rc={r.returncode})"
        except (subprocess.TimeoutExpired, OSError) as e:
            last = str(e)
        if attempt < 2:
            print(f"[live-data] kalshi-cli {' '.join(args)} attempt {attempt+1}/3 failed: {last} — retrying", file=sys.stderr)
            time.sleep(2 ** attempt)
    print(f"[live-data] kalshi-cli {' '.join(args)} failed after 3 attempts: {last}", file=sys.stderr)
    return {}

def _yes_mark_cents(ticker):
    """YES-side mark price in cents for `ticker`, or None if the book is empty/unusable.

    Modeled on sync_live_positions._fetch_mid_cents (tight timeout, fail-soft import so a
    broken data/ module can never take the whole dashboard feed down) but corrects a
    one-sided-book bug that path has: derive_book encodes a MISSING side as the sentinel 0
    (real Kalshi quotes are 1-99), and its `(yb or ya)` gate lets a one-sided book through
    as mid = real_side/2 — a fabricated price that can sit BELOW the standing bid and
    SIGN-FLIP a near-settled position's P&L (a NO holder in a market bid YES 99¢ is nearly
    worthless, not worth 50¢).

      • two-sided book  -> standard mid (yes_bid+yes_ask)/2, matching the rest of the system
      • only YES bids   -> yes_bid   (market near-resolved YES; that bid is the real floor)
      • only NO bids     -> yes_ask   (= 100 - no_bid; market near-resolved NO)
      • empty book       -> None -> renders '—'

    The one-sided case is the honest liquidation value, and near-settled markets are exactly
    where the unrealized number matters most — so we mark them rather than blanking them."""
    try:
        from data.kalshi_data import fetch_orderbook, derive_book, yes_mark_from_book
        ob = fetch_orderbook(ticker, timeout=4, retries=1)
        if not ob or not ob.get("orderbook_fp"):
            return None
        b = derive_book(ob["orderbook_fp"])
        return yes_mark_from_book(b.get("yes_bid"), b.get("yes_ask"))   # shared one-sided-safe valuation
    except Exception:
        return None


# Get positions and balance
positions_raw = run_kalshi("portfolio", "positions", "--json")
balance_raw = run_kalshi("portfolio", "balance", "--json")

poses = positions_raw.get("market_positions", []) if isinstance(positions_raw, dict) else []
bal = balance_raw.get("balance", 0)
portfolio = balance_raw.get("portfolio_value", 0)

# Build position list
pos_list = []
non_weather_cost = 0.0
non_weather_value = 0.0
for p in poses:
    ticker = p.get("ticker", "")
    if not is_weather_ticker(ticker):
        non_weather_cost += float(p.get("total_traded_dollars", 0))
        continue
    fp = float(p.get("position_fp", 0))
    if abs(fp) < 0.01:
        continue
    side = "yes" if fp > 0 else "no"
    qty = abs(round(fp))                  # round, not truncate, so a sub-1 holding survives (bug 2)
    if qty == 0 and abs(fp) >= 0.01:
        qty = 1
    cost = float(p.get("total_traded_dollars", 0))
    pos_list.append({
        "ticker": p.get("ticker", ""),
        "side": side,
        "qty": qty,
        "position_fp": fp,
        "total_cost_dollars": cost,
        "entry_price_cents": int(round(cost / qty * 100)) if qty > 0 else 0,
        "unreal_pnl_dollars": None,   # marked-to-market in the enrichment loop below
        "resting_orders": int(p.get("resting_orders_count", 0)),
        "fees_paid": float(p.get("fees_paid_dollars", 0)),
    })

# Enrich with close_time + mark-to-market unrealized P&L.
# WHY (2026-07-11): since the fp/dollars migration neither portfolio positions nor
# markets-get carries a usable mark (market_exposure_dollars just mirrors cost and the
# markets-get quote fields read 0), and the old realized_pnl_dollars mapping is 0 for
# every open position — so the LIVE panel showed $0 unrealized on every row. Mark from
# the orderbook instead (same fetch_orderbook → derive_book path the live book's sync
# trusts), via _yes_mark_cents: mid for a two-sided book, standing liquidation bid for a
# one-sided (near-settled) book. A closed market or empty book leaves the mark None
# (renders "—"), never a fake $0.
_now_utc = datetime.now(timezone.utc)
for _pos in pos_list:
    try:
        _m = run_kalshi("markets", "get", _pos["ticker"], "--json")
        _mk = _m.get("market", _m) if isinstance(_m, dict) else {}
        _pos["close_time"] = _mk.get("close_time")
    except Exception:
        _pos["close_time"] = None
    # A closed market has no live book — skip the guaranteed-None orderbook fetch
    # (positions linger here for hours between close and settlement).
    _closed = False
    if _pos["close_time"]:
        try:
            _closed = datetime.fromisoformat(_pos["close_time"].replace("Z", "+00:00")) < _now_utc
        except ValueError:
            pass
    yes_mark = None if _closed else _yes_mark_cents(_pos["ticker"])
    if yes_mark is not None:
        cur = yes_mark if _pos["side"] == "yes" else 100.0 - yes_mark
        _pos["cur_price_cents"] = round(cur, 1)
        # value leg uses the true fractional size, NOT the display qty — qty is
        # abs(round(fp)) floored to 1, which for a sub-1 holding would price a full
        # contract against a fractional contract's cost (phantom profit).
        _pos["unreal_pnl_dollars"] = round(abs(_pos["position_fp"]) * cur / 100.0 - _pos["total_cost_dollars"], 2)

# Track initial balance
INITIAL_STATE = os.path.join(ROOT, "state", "live-initial.json")
if os.path.exists(INITIAL_STATE):
    with open(INITIAL_STATE) as f:
        initial = json.load(f)
    initial_balance_cents = initial.get("initial_balance_cents", _DEFAULT_INITIAL_CENTS)
else:
    initial_balance_cents = _DEFAULT_INITIAL_CENTS
    os.makedirs(os.path.dirname(INITIAL_STATE), exist_ok=True)
    _atomic_write_json(INITIAL_STATE, {"initial_balance_cents": initial_balance_cents, "started_at": datetime.now(timezone.utc).isoformat()}, indent=None)

# True account figures (kept in the JSON for reference; NOT displayed — they include the
# non-weather sports cash + Iran position).
account_avail = round(bal / 100, 2)

# --- Weather-only equity (the displayed LIVE panel) ---
# Realized weather P&L from the settlement ledger (clean); cash tied up in the already
# weather-filtered open positions (pos_list, built with the line-63 weather filter); and the
# weather share of the account portfolio market value (Iran market value already netted out).
weather_realized_cents = _weather_realized_cents(ROOT)
weather_open_cost_cents = int(round(sum(p["total_cost_dollars"] for p in pos_list) * 100))
weather_portfolio = max(0, portfolio - int(round(non_weather_cost * 100)))

weather_available_cents, weather_total, weather_pnl, weather_pnl_pct = compute_weather_equity(
    initial_balance_cents, weather_realized_cents, weather_open_cost_cents, weather_portfolio)

# Weather-scoped Available / Deployed for the hero cards + history snapshot + stdout.
avail = round(weather_available_cents / 100, 2)
deployed = round(weather_open_cost_cents / 100, 2)
weather_deployed = deployed

# Position breakdown by side
yes_positions = [p for p in pos_list if p['side'] == 'yes']
no_positions = [p for p in pos_list if p['side'] == 'no']
yes_exposure = sum(p['total_cost_dollars'] for p in yes_positions)
no_exposure = sum(p['total_cost_dollars'] for p in no_positions)

# Average entry
avg_entry = round(sum(p['entry_price_cents'] for p in pos_list) / len(pos_list), 1) if pos_list else 0

# Upcoming expirations (next 24h)
now = datetime.now(timezone.utc)
upcoming = []
for p in pos_list:
    ct = p.get('close_time')
    if ct:
        try:
            ct_dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            hours_until = (ct_dt - now).total_seconds() / 3600
            if 0 < hours_until <= 24:
                upcoming.append({'ticker': p['ticker'], 'hours': round(hours_until, 1), 'side': p['side'], 'qty': p['qty']})
        except:
            pass

# Load existing history for analytics
hist_path = os.path.join(DASHBOARD_DIR, "live-history.json")
history = []
try:
    with open(hist_path) as f:
        history = json.load(f)
except (OSError, json.JSONDecodeError):
    pass

# Compute live streak from history
live_streak_type = None
live_streak_count = 0
if history:
    # Use PnL changes to determine streak
    recent = history[-50:]  # last 50 snapshots
    for i in range(len(recent) - 1, 0, -1):
        pnl_change = recent[i]['pnl'] - recent[i-1]['pnl']
        if pnl_change > 0.01:
            if live_streak_type is None:
                live_streak_type = 'win'
                live_streak_count = 1
            elif live_streak_type == 'win':
                live_streak_count += 1
            else:
                break
        elif pnl_change < -0.01:
            if live_streak_type is None:
                live_streak_type = 'loss'
                live_streak_count = 1
            elif live_streak_type == 'loss':
                live_streak_count += 1
            else:
                break

# Daily PnL from history (group by date)
daily_live = {}
for h in history:
    d = h['timestamp'][:10]
    if d not in daily_live:
        daily_live[d] = {'pnl': h['pnl'], 'positions': h['positions']}

# Compute max drawdown from history
live_peak = initial_balance_cents / 100
live_max_dd = 0
live_max_dd_pct = 0
for h in history:
    total = h['total']
    live_peak = max(live_peak, total)
    dd = live_peak - total
    live_max_dd = max(live_max_dd, dd)
    dd_pct = (dd / live_peak * 100) if live_peak > 0 else 0
    live_max_dd_pct = max(live_max_dd_pct, dd_pct)

# Today's PnL
today_str = now.strftime('%Y-%m-%d')
today_start_pnl = None
for h in history:
    if h['timestamp'][:10] == today_str:
        today_start_pnl = h['pnl']
        break
if today_start_pnl is None:  # no snapshot from today yet → fall back to latest prior snapshot (or 0)
    today_start_pnl = history[-1]['pnl'] if history else 0
live_today_pnl = round(weather_pnl - today_start_pnl, 2)

data = {
    # TRUE account-global figures — kept for reference, NOT displayed. These include the
    # non-weather sports cash + Iran position; the LIVE panel renders the weather_* keys below.
    "balance_cents": int(bal),
    "balance_dollars": account_avail,
    "portfolio_value_cents": int(portfolio),
    "portfolio_value_dollars": round(portfolio / 100, 2),
    "total_balance_dollars": round((bal + portfolio) / 100, 2),
    "deployed_dollars": round(initial_balance_cents / 100 - account_avail, 2),
    "unreal_pnl_dollars": round(portfolio / 100, 2),
    "non_weather_cost_dollars": round(non_weather_cost, 2),
    "initial_balance_dollars": round(initial_balance_cents / 100, 2),
    "open_positions": pos_list,
    "open_order_count": len(pos_list),
    # Weather-only figures — what the dashboard displays (exclude sports/Iran).
    "weather_available_dollars": avail,
    "weather_deployed_dollars": round(weather_deployed, 2),
    "weather_portfolio_dollars": round(weather_portfolio / 100, 2),
    "weather_total_dollars": round(weather_total / 100, 2),
    "weather_pnl_dollars": weather_pnl,
    "weather_pnl_pct": weather_pnl_pct,
    
    # Enhanced metrics
    "yes_positions": len(yes_positions),
    "no_positions": len(no_positions),
    "yes_exposure_dollars": round(yes_exposure, 2),
    "no_exposure_dollars": round(no_exposure, 2),
    "avg_entry_cents": avg_entry,
    "upcoming_expirations": upcoming,
    "upcoming_count": len(upcoming),
    
    # Streaks
    "live_streak_type": live_streak_type,
    "live_streak_count": live_streak_count,
    "live_max_drawdown": round(live_max_dd, 2),
    "live_max_drawdown_pct": round(live_max_dd_pct, 2),
    
    # Today
    "today_pnl": live_today_pnl,
    
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}

os.makedirs(DASHBOARD_DIR, exist_ok=True)
path = os.path.join(DASHBOARD_DIR, "data-live.json")
_atomic_write_json(path, data)

# Append PnL history snapshot
history.append({
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "available": avail,
    "portfolio": round(weather_portfolio / 100, 2),
    "total": round(weather_total / 100, 2),
    "pnl": weather_pnl,
    "positions": len(pos_list),
})
if len(history) > 336:
    history = history[-336:]
_atomic_write_json(hist_path, history)

_publish_canvas()

_marked = sum(1 for p in pos_list if p["unreal_pnl_dollars"] is not None)
if pos_list and _marked == 0:
    print("[live-data] WARNING: 0 positions marked — orderbook path may be broken "
          "(empty books on every ticker is unlikely; check data/kalshi_data imports)", file=sys.stderr)
print(f"Live data (weather-only): {len(pos_list)} positions ({_marked} marked) | ${avail} avail | ${deployed} deployed | total=${round(weather_total/100,2)} | PnL=${weather_pnl} ({weather_pnl_pct}%) | history={len(history)} snapshots")

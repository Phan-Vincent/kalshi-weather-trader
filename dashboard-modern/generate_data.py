#!/usr/bin/env python3
"""Generate comprehensive dashboard data for the modern Kalshi Trader dashboard.

This script:
1. Fetches live data from Kalshi API (production)
2. Reads existing state files (settlements, brier, risk, etc.)
3. Computes all metrics (PnL, WR, drawdown, Brier, etc.)
4. Outputs data.json and data-live.json for the dashboard
"""

import json
import subprocess
import os
import sys
import time
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Paths
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(ROOT, "dashboard-modern")
DATA_DIR = os.path.join(DASHBOARD_DIR, "data")
STATE_DIR = os.path.join(ROOT, "state")

os.makedirs(DATA_DIR, exist_ok=True)

# Default initial balance (cents)
_DEFAULT_INITIAL_CENTS = 100000


def _parse_kalshi(stdout):
    """Parse JSON from kalshi-cli output, handling mixed output."""
    raw = (stdout or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def run_kalshi(*args):
    """Run kalshi-cli command with retries."""
    last = None
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["kalshi-cli", "--prod", *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
            parsed = _parse_kalshi(r.stdout)
            if parsed is not None:
                return parsed
            last = f"no JSON in output (rc={r.returncode})"
        except (subprocess.TimeoutExpired, OSError) as e:
            last = str(e)
        if attempt < 2:
            print(
                f"[dashboard] kalshi-cli {' '.join(args)} attempt {attempt+1}/3 failed: {last} — retrying",
                file=sys.stderr,
            )
            time.sleep(2 ** attempt)
    print(
        f"[dashboard] kalshi-cli {' '.join(args)} failed after 3 attempts: {last}",
        file=sys.stderr,
    )
    return {}


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default if default is not None else {}


def load_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def generate_dashboard_data():
    """Generate the main dashboard data file."""
    print("[dashboard] Generating dashboard data...")

    # Load state files
    settlements = []
    for r in load_jsonl(os.path.join(STATE_DIR, "settlement-log.jsonl")):
        r["settled_at"] = r.get("settled_at_utc", "")
        r["date"] = r["settled_at"][:10] if r["settled_at"] else "unknown"
        settlements.append(r)

    brier_records = load_jsonl(os.path.join(STATE_DIR, "brier-log.jsonl"))
    brier_settled = [r for r in brier_records if r.get("status") == "settled"]
    brier_open = [r for r in brier_records if r.get("status") == "open"]

    book = load_json(os.path.join(STATE_DIR, "paper-book.json"), {})
    risk = load_json(os.path.join(STATE_DIR, "risk-state.json"), {})
    model_cal = load_json(os.path.join(STATE_DIR, "model-calibration.json"), {})

    if not isinstance(book, dict):
        book = {}
    for _k, _v in (
        ("cash_cents", 0),
        ("starting_bank_cents", _DEFAULT_INITIAL_CENTS),
        ("open", []),
        ("filled_orders", []),
    ):
        book.setdefault(_k, _v)
    if not isinstance(risk, dict):
        risk = {}

    # Core metrics
    cash = book["cash_cents"] / 100
    start_bank = book["starting_bank_cents"] / 100
    total_pnl_cents = sum(s.get("pnl_cents", 0) for s in settlements)
    total_pnl = total_pnl_cents / 100
    wins = [s for s in settlements if s.get("pnl_cents", 0) > 0]
    losses_s = [s for s in settlements if s.get("pnl_cents", 0) < 0]
    win_rate = len(wins) / len(settlements) * 100 if settlements else 0
    avg_win = sum(s["pnl_cents"] for s in wins) / 100 / len(wins) if wins else 0
    avg_loss = abs(sum(s["pnl_cents"] for s in losses_s)) / 100 / len(losses_s) if losses_s else 0
    profit_factor = (
        (sum(s["pnl_cents"] for s in wins) / 100)
        / (abs(sum(s["pnl_cents"] for s in losses_s)) / 100)
        if losses_s
        else 99
    )

    # Time-based aggregation
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily = defaultdict(
        lambda: {
            "pnl": 0,
            "wins": 0,
            "trades": 0,
            "losses": 0,
            "gross_profit": 0,
            "gross_loss": 0,
        }
    )
    for s in settlements:
        d = s["date"]
        pnl = s.get("pnl_cents", 0)
        daily[d]["pnl"] += pnl
        daily[d]["trades"] += 1
        if pnl > 0:
            daily[d]["wins"] += 1
            daily[d]["gross_profit"] += pnl
        elif pnl < 0:
            daily[d]["losses"] += 1
            daily[d]["gross_loss"] += abs(pnl)

    weekly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
    for s in settlements:
        try:
            dt = datetime.strptime(s["date"], "%Y-%m-%d")
            wk = dt.isocalendar()
            key = f"{wk[0]}-W{wk[1]:02d}"
            pnl = s.get("pnl_cents", 0)
            weekly[key]["pnl"] += pnl
            weekly[key]["trades"] += 1
            if pnl > 0:
                weekly[key]["wins"] += 1
            elif pnl < 0:
                weekly[key]["losses"] += 1
        except Exception:
            continue

    monthly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
    for s in settlements:
        mo = s["date"][:7]
        pnl = s.get("pnl_cents", 0)
        monthly[mo]["pnl"] += pnl
        monthly[mo]["trades"] += 1
        if pnl > 0:
            monthly[mo]["wins"] += 1
        elif pnl < 0:
            monthly[mo]["losses"] += 1

    yearly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
    for s in settlements:
        yr = s["date"][:4]
        pnl = s.get("pnl_cents", 0)
        yearly[yr]["pnl"] += pnl
        yearly[yr]["trades"] += 1
        if pnl > 0:
            yearly[yr]["wins"] += 1
        elif pnl < 0:
            yearly[yr]["losses"] += 1

    # Streaks
    current_streak_type = None
    current_streak_count = 0
    max_win_streak = 0
    max_loss_streak = 0
    temp_win = 0
    temp_loss = 0

    for s in sorted(settlements, key=lambda x: x.get("settled_at_utc", "")):
        pnl = s.get("pnl_cents", 0)
        if pnl > 0:
            if current_streak_type == "loss":
                temp_loss = 0
            current_streak_type = "win"
            temp_win += 1
            temp_loss = 0
            max_win_streak = max(max_win_streak, temp_win)
        elif pnl < 0:
            if current_streak_type == "win":
                temp_win = 0
            current_streak_type = "loss"
            temp_loss += 1
            temp_win = 0
            max_loss_streak = max(max_loss_streak, temp_loss)

    # Current streak from most recent
    if settlements:
        recent = sorted(settlements, key=lambda x: x.get("settled_at_utc", ""), reverse=True)
        current_streak_type = None
        current_streak_count = 0
        for s in recent:
            pnl = s.get("pnl_cents", 0)
            if pnl > 0:
                if current_streak_type is None:
                    current_streak_type = "win"
                    current_streak_count = 1
                elif current_streak_type == "win":
                    current_streak_count += 1
                else:
                    break
            elif pnl < 0:
                if current_streak_type is None:
                    current_streak_type = "loss"
                    current_streak_count = 1
                elif current_streak_type == "loss":
                    current_streak_count += 1
                else:
                    break
            else:
                break
    else:
        current_streak_type = None
        current_streak_count = 0

    # Drawdown
    cumulative = 0
    peak = 0
    max_drawdown = 0
    max_drawdown_pct = 0
    daily_cum = []
    for d_key in sorted(daily.keys()):
        cumulative += daily[d_key]["pnl"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_drawdown = max(max_drawdown, dd)
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        max_drawdown_pct = max(max_drawdown_pct, dd_pct)
        daily_cum.append(
            {
                "date": d_key,
                "cum_pnl": round(cumulative / 100, 2),
                "peak": round(peak / 100, 2),
                "drawdown": round(dd / 100, 2),
            }
        )

    # Sharpe-like ratio
    daily_returns = [daily[d]["pnl"] / 100 for d in sorted(daily.keys())]
    if len(daily_returns) > 1:
        avg_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - avg_return) ** 2 for r in daily_returns) / len(daily_returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0
        sharpe_like = (avg_return / std_dev * math.sqrt(252)) if std_dev > 0 else 0
    else:
        sharpe_like = 0

    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss) if settlements else 0

    # Price bucket analysis
    price_buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for s in settlements:
        entry = s.get("entry_cents", 50)
        bucket = min(90, max(10, (entry // 10) * 10))
        pnl = s.get("pnl_cents", 0)
        price_buckets[bucket]["trades"] += 1
        price_buckets[bucket]["pnl"] += pnl
        if pnl > 0:
            price_buckets[bucket]["wins"] += 1

    # Hourly performance
    hourly_perf = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for s in settlements:
        ts = s.get("settled_at_utc", "")
        if ts:
            try:
                hour = int(ts[11:13]) if len(ts) >= 13 else 12
                pnl = s.get("pnl_cents", 0)
                hourly_perf[hour]["trades"] += 1
                hourly_perf[hour]["pnl"] += pnl
                if pnl > 0:
                    hourly_perf[hour]["wins"] += 1
            except Exception:
                pass

    # City data
    city_pnl = risk.get("city_pnl_cents", {})
    city_cons = risk.get("consecutive_losses_by_city", {})

    # Brier / Model
    all_brier = [r.get("our_brier", 0) for r in brier_settled]
    all_mkt = [r.get("market_brier", 0) for r in brier_settled]
    brier_avg = sum(all_brier) / len(all_brier) if all_brier else 0
    mkt_avg = sum(all_mkt) / len(all_mkt) if all_mkt else 0
    bss_all = 1 - (brier_avg / mkt_avg) if mkt_avg > 0 else 0

    # Rolling Brier
    brier_rolling = []
    for i, r in enumerate(brier_settled):
        window = brier_settled[max(0, i - 49) : i + 1]
        o = sum(x.get("our_brier", 0) for x in window) / len(window)
        m = sum(x.get("market_brier", 0) for x in window) / len(window)
        brier_rolling.append(
            {"idx": i, "our": o, "mkt": m, "bss": 1 - (o / m) if m > 0 else 0}
        )

    # Calibration
    cbins = defaultdict(list)
    for r in brier_settled:
        prob = r.get("our_prob_for_outcome", 0.5)
        outcome = r.get("actual", 0)
        bk = round(prob * 10) / 10
        cbins[bk].append(outcome)

    # Today / Week / Month
    td = daily.get(today, {})
    current_week_pnl = (
        sum(weekly[w]["pnl"] for w in sorted(weekly)[-1:]) / 100 if weekly else 0
    )
    current_month_pnl = (
        sum(monthly[m]["pnl"] for m in sorted(monthly)[-1:]) / 100 if monthly else 0
    )

    # Last 7 days
    last_7_days = []
    for i in range(6, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in daily:
            last_7_days.append(
                {
                    "date": d,
                    "pnl": round(daily[d]["pnl"] / 100, 2),
                    "trades": daily[d]["trades"],
                    "wins": daily[d]["wins"],
                    "win_rate": (
                        round(daily[d]["wins"] / daily[d]["trades"] * 100, 1)
                        if daily[d]["trades"]
                        else 0
                    ),
                }
            )
        else:
            last_7_days.append(
                {"date": d, "pnl": 0, "trades": 0, "wins": 0, "win_rate": 0}
            )

    # Last 4 weeks
    last_4_weeks = []
    for w in sorted(weekly)[-4:]:
        wr = (
            round(weekly[w]["wins"] / weekly[w]["trades"] * 100, 1)
            if weekly[w]["trades"]
            else 0
        )
        last_4_weeks.append(
            {
                "week": w,
                "pnl": round(weekly[w]["pnl"] / 100, 2),
                "trades": weekly[w]["trades"],
                "wins": weekly[w]["wins"],
                "win_rate": wr,
            }
        )

    # Build data object
    data = {
        # Core
        "cash": round(cash, 2),
        "start_bank": round(start_bank, 2),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": len(settlements),
        "wins": len(wins),
        "losses": len(losses_s),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        # Advanced
        "expectancy": round(expectancy, 2),
        "sharpe_like": round(sharpe_like, 2),
        "max_drawdown": round(max_drawdown / 100, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 1),
        "current_streak_type": current_streak_type,
        "current_streak_count": current_streak_count,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        # Positions
        "open_positions": len(book["open"]),
        "filled_orders": len(book["filled_orders"]),
        "positions": load_json(os.path.join(STATE_DIR, "positions-live.json"), []),
        # Time-based
        "today_pnl": round(td.get("pnl", 0) / 100, 2),
        "today_trades": td.get("trades", 0),
        "today_wins": td.get("wins", 0),
        "current_week_pnl": round(current_week_pnl, 2),
        "current_month_pnl": round(current_month_pnl, 2),
        # Brier
        "brier_settled": len(brier_settled),
        "brier_open": len(brier_open),
        "brier_avg": round(brier_avg, 4),
        "brier_mkt_avg": round(mkt_avg, 4),
        "bss_all": round(bss_all, 4),
        "model_global_bss": model_cal.get("brier_skill_score"),
        "model_global_n": model_cal.get("n_trades"),
        "model_calibration_updated": model_cal.get("updated_utc"),
        # Time series
        "daily": [
            {
                "date": d,
                "pnl": round(v["pnl"] / 100, 2),
                "trades": v["trades"],
                "wins": v["wins"],
                "losses": v["losses"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
                "gross_profit": round(v["gross_profit"] / 100, 2),
                "gross_loss": round(v["gross_loss"] / 100, 2),
            }
            for d, v in sorted(daily.items())
        ],
        "weekly": [
            {
                "week": w,
                "pnl": round(v["pnl"] / 100, 2),
                "trades": v["trades"],
                "wins": v["wins"],
                "losses": v["losses"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
            }
            for w, v in sorted(weekly.items())
        ],
        "monthly": [
            {
                "month": m,
                "pnl": round(v["pnl"] / 100, 2),
                "trades": v["trades"],
                "wins": v["wins"],
                "losses": v["losses"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
            }
            for m, v in sorted(monthly.items())
        ],
        "yearly": [
            {
                "year": y,
                "pnl": round(v["pnl"] / 100, 2),
                "trades": v["trades"],
                "wins": v["wins"],
                "losses": v["losses"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
            }
            for y, v in sorted(yearly.items())
        ],
        # Last N
        "last_7_days": last_7_days,
        "last_4_weeks": last_4_weeks,
        # Drawdown
        "drawdown_series": daily_cum,
        # Cities
        "cities": [
            {
                "code": c,
                "city": c,
                "pnl": round(p / 100, 2),
                "consec_losses": city_cons.get(c, 0),
            }
            for c, p in sorted(city_pnl.items(), key=lambda x: -abs(x[1]))
        ],
        # Brier
        "brier_rolling": [
            {
                "idx": r["idx"],
                "our": round(r["our"], 4),
                "mkt": round(r["mkt"], 4),
                "bss": round(r["bss"], 4),
            }
            for r in brier_rolling
        ],
        "calibration": [
            {"pred": k * 100, "actual": round(sum(v) / len(v) * 100, 1), "count": len(v)}
            for k, v in sorted(cbins.items())
        ],
        "last_50_bss": round(brier_rolling[-1]["bss"], 4) if brier_rolling else 0,
        # Price buckets
        "price_buckets": [
            {
                "bucket": b,
                "trades": v["trades"],
                "wins": v["wins"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
                "pnl": round(v["pnl"] / 100, 2),
            }
            for b, v in sorted(price_buckets.items())
        ],
        # Hourly
        "hourly_perf": [
            {
                "hour": h,
                "trades": v["trades"],
                "wins": v["wins"],
                "win_rate": (
                    round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
                ),
                "pnl": round(v["pnl"] / 100, 2),
            }
            for h, v in sorted(hourly_perf.items())
        ],
        # Risk
        "risk": {
            k: risk.get(k)
            for k in [
                "today_pnl_cents",
                "week_pnl_cents",
                "consecutive_losses",
                "halted_cities",
                "city_loss_circuit_breaker_threshold",
            ]
        },
        # Settlements
        "settlements": [
            {
                "ticker": s.get("ticker", ""),
                "side": s.get("side", ""),
                "pnl_cents": s.get("pnl_cents", 0),
                "entry_cents": s.get("entry_cents", 0),
                "price_cents": s.get("entry_cents", 0),
                "date": (
                    s.get("settled_at_utc", "")[:10]
                    if s.get("settled_at_utc")
                    else s.get("date", "")
                ),
                "settled_at": s.get("settled_at_utc", ""),
            }
            for s in settlements
        ],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(os.path.join(DATA_DIR, "data.json"), "w") as f:
        json.dump(data, f, indent=2)

    size = len(json.dumps(data))
    print(f"[dashboard] data.json generated: {size} bytes")
    return data


def generate_live_data():
    """Generate live data from Kalshi API."""
    print("[dashboard] Fetching live data from Kalshi...")

    positions_raw = run_kalshi("portfolio", "positions", "--json")
    balance_raw = run_kalshi("portfolio", "balance", "--json")

    poses = (
        positions_raw.get("market_positions", [])
        if isinstance(positions_raw, dict)
        else []
    )
    bal = balance_raw.get("balance", 0) if isinstance(balance_raw, dict) else 0
    portfolio = (
        balance_raw.get("portfolio_value", 0)
        if isinstance(balance_raw, dict)
        else 0
    )

    pos_list = []
    non_weather_cost = 0.0
    total_fees = 0.0
    total_resting = 0

    for p in poses:
        ticker = p.get("ticker", "")
        # Skip non-weather positions for the weather dashboard
        if "IRAN" in ticker or "AGREEMENT" in ticker:
            non_weather_cost += float(p.get("total_traded_dollars", 0))
            continue
        fp = float(p.get("position_fp", 0))
        if abs(fp) < 0.01:
            continue
        side = "yes" if fp > 0 else "no"
        qty = abs(round(fp))              # round, not truncate, so a sub-1 holding survives (bug 2)
        if qty == 0 and abs(fp) >= 0.01:
            qty = 1
        cost = float(p.get("total_traded_dollars", 0))
        fees = float(p.get("fees_paid_dollars", 0))
        total_fees += fees
        total_resting += int(p.get("resting_orders_count", 0))

        pos_list.append(
            {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "position_fp": fp,
                "total_cost_dollars": cost,
                "entry_price_cents": int(round(cost / qty * 100)) if qty > 0 else 0,
                "unreal_pnl_dollars": float(p.get("realized_pnl_dollars", 0)),
                "resting_orders": int(p.get("resting_orders_count", 0)),
                "fees_paid": fees,
            }
        )

    # Enrich with close_time
    for pos in pos_list:
        try:
            m = run_kalshi("markets", "get", pos["ticker"], "--json")
            mk = m.get("market", m) if isinstance(m, dict) else {}
            pos["close_time"] = mk.get("close_time")
            pos["market_title"] = mk.get("title", "")
            pos["status"] = mk.get("status", "")
        except Exception:
            pass

    unrealized = sum(p.get("unreal_pnl_dollars", 0) for p in pos_list)

    live_data = {
        "balance_cents": bal,
        "balance_dollars": bal / 100,
        "portfolio_value_cents": portfolio,
        "portfolio_value_dollars": portfolio / 100,
        "positions": pos_list,
        "position_count": len(pos_list),
        "total_resting_orders": total_resting,
        "total_fees_paid": total_fees,
        "unrealized_pnl_dollars": unrealized,
        "non_weather_exposure_dollars": non_weather_cost,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(os.path.join(DATA_DIR, "data-live.json"), "w") as f:
        json.dump(live_data, f, indent=2)

    size = len(json.dumps(live_data))
    print(f"[dashboard] data-live.json generated: {size} bytes")
    print(f"[dashboard] {len(pos_list)} positions, balance=${bal/100:.2f}")
    return live_data


def main():
    print("=" * 60)
    print("Kalshi Trader Dashboard - Data Generator")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Generate both data files
    generate_dashboard_data()
    generate_live_data()

    print("=" * 60)
    print("Done. Dashboard data updated.")
    print("=" * 60)


if __name__ == "__main__":
    main()

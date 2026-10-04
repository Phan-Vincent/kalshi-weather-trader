#!/usr/bin/env python3
# Nightly performance review for Kalshi Weather Paper Trader
# Writes: ~/.openclaw/workspace/automations/kalshi-weather/reviews/YYYY-MM-DD-review.md
# Posts summary to stdout for Discord

import json, sys, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

root = os.path.expanduser("~/.openclaw/workspace/automations/kalshi-weather")
review_dir = os.path.join(root, "reviews")

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def load_jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []

def file_freshness(path):
    """Return human-readable age of a file, or 'missing'."""
    try:
        mtime = os.path.getmtime(path)
        age = datetime.now() - datetime.fromtimestamp(mtime)
        if age.total_seconds() < 60:
            return f"{int(age.total_seconds())}s ago"
        elif age.total_seconds() < 3600:
            return f"{int(age.total_seconds()/60)}m ago"
        elif age.total_seconds() < 86400:
            return f"{int(age.total_seconds()/3600)}h ago"
        else:
            return f"{int(age.total_seconds()/86400)}d ago"
    except (FileNotFoundError, OSError):
        return "missing"

def parse_utc(ts):
    """Parse an ISO timestamp string to a datetime."""
    if not ts:
        return None
    try:
        # Handle Z suffix and various formats
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def count_past_due_open(open_positions):
    """Count open positions whose market_close_time has passed (should be settled)."""
    now = datetime.now(timezone.utc)
    past_due = []
    for pos in (open_positions.values() if isinstance(open_positions, dict) else open_positions):
        close_time = parse_utc(pos.get("market_close_time"))
        if close_time and close_time < now:
            past_due.append(pos.get("ticker", "unknown"))
    return past_due

def format_freshness_table(paths, labels):
    lines = ["| Source | Age | Status |", "|--------|-----|--------|"]
    for path, label in zip(paths, labels):
        age = file_freshness(path)
        status = "✅ fresh" if age.endswith("m ago") or age.endswith("s ago") or age.endswith("h ago") else ("⚠️ old" if "d" in age else "❌ missing")
        lines.append(f"| {label} | {age} | {status} |")
    return "\n".join(lines)

# --- Load state ---
# 2026-06-21: read the live main paper book at state/paper. The A/B harness moved each arm
# to state/<arm>; the root state/ is orphaned (frozen), which made this review stale.
# Env-overridable, consistent with the other bin scripts.
state_dir = os.environ.get("KALSHI_WEATHER_STATE_DIR", os.path.join(root, "state", "paper"))
book_path = os.path.join(state_dir, "paper-book.json")
settlement_path = os.path.join(state_dir, "settlement-log.jsonl")
brier_path = os.path.join(state_dir, "brier-log.jsonl")
risk_path = os.path.join(state_dir, "risk-state.json")

book = load_json(book_path) or {}
cash = book.get("cash_cents", 0)
starting = book.get("starting_bank_cents", book.get("starting_cents", 30000))
open_positions = book.get("open", [])
closed = book.get("closed", [])
pending = book.get("pending_makers", [])
book_updated = book.get("updated_utc", "unknown")

# Realized P&L
realized = sum(s.get("pnl_cents", 0) for s in closed)
realized_dollars = realized / 100

# Freshness metadata
freshness_table = format_freshness_table(
    [book_path, settlement_path, brier_path, risk_path],
    ["paper-book.json", "settlement-log.jsonl", "brier-log.jsonl", "risk-state.json"]
)

# Check for stale open positions (past market close time but still open)
past_due_tickers = count_past_due_open(open_positions)
stale_warning = ""
if past_due_tickers:
    stale_warning = f"⚠️ **{len(past_due_tickers)} position(s) past market close time still open:** {', '.join(past_due_tickers[:5])}{'...' if len(past_due_tickers) > 5 else ''}\n"

# --- Settlement analysis ---
settlements = load_jsonl(settlement_path)

# Filter today's settlements
today_str = datetime.now().strftime("%Y-%m-%d")
today_settlements = [s for s in settlements if s.get("settled_at", "").startswith(today_str)]

# All-time stats
all_wins = [s for s in settlements if s.get("pnl_cents", 0) > 0]
all_losses = [s for s in settlements if s.get("pnl_cents", 0) <= 0]
all_win_rate = len(all_wins) / len(settlements) * 100 if settlements else 0

# Today's stats
today_wins = [s for s in today_settlements if s.get("pnl_cents", 0) > 0]
today_losses = [s for s in today_settlements if s.get("pnl_cents", 0) <= 0]
today_pnl = sum(s.get("pnl_cents", 0) for s in today_settlements)
today_win_rate = len(today_wins) / len(today_settlements) * 100 if today_settlements else 0

# --- Brier score (from brier-log) ---
brier_entries = load_jsonl(brier_path)

# Deduplicate by (paper_order_id, fill_time)
seen = set()
unique_brier = []
for b in brier_entries:
    key = (b.get("paper_order_id"), b.get("fill_time"), b.get("ticker"))
    if key not in seen:
        seen.add(key)
        unique_brier.append(b)

# Calculate Brier on settled entries only
brier_sum = 0
brier_count = 0
for b in unique_brier:
    # Use precomputed Brier if available
    our_brier = b.get("our_brier")
    if our_brier is not None:
        brier_sum += our_brier
        brier_count += 1
        continue
    # Otherwise compute manually
    outcome = b.get("outcome")
    if outcome is not None:
        prob = b.get("price", 0) / 100  # price in cents -> probability
        if b.get("side") == "no":
            prob = 1 - prob
        # Convert string outcome to numeric
        outcome_val = 1.0 if str(outcome).lower() == "yes" else 0.0
        brier = (prob - outcome_val) ** 2
        brier_sum += brier
        brier_count += 1

avg_brier = brier_sum / brier_count if brier_count > 0 else None

# --- City performance ---
by_city = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
for s in settlements:
    ticker = s.get("ticker", "")
    # Extract city from ticker: KXHIGHTBOS-... -> BOS
    if "HIGHT" in ticker:
        city = ticker.split("HIGHT")[1].split("-")[0]
    elif "LOWT" in ticker:
        city = ticker.split("LOWT")[1].split("-")[0]
    else:
        city = "UNKNOWN"

    pnl = s.get("pnl_cents", 0)
    by_city[city]["pnl"] += pnl
    if pnl > 0:
        by_city[city]["w"] += 1
    else:
        by_city[city]["l"] += 1

# --- Lessons learned today ---
lessons = []
lessons_file = os.path.join(root, "LESSONS.md")
if os.path.exists(lessons_file):
    with open(lessons_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("- [") and today_str in line:
                lessons.append(line)

# --- Write review ---
review_path = os.path.join(review_dir, f"{today_str}-review.md")
with open(review_path, "w") as f:
    f.write(f"# Kalshi Weather Paper Trader - Daily Review\n\n")
    f.write(f"**Date:** {today_str}  \n")
    f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S PDT')}\n\n")

    f.write(f"## Data Freshness\n\n")
    f.write(f"_Book last updated: {book_updated}_\n\n")
    f.write(freshness_table + "\n\n")
    if stale_warning:
        f.write(f"### ⚠️ Stale Data Warning\n\n")
        f.write(stale_warning)
        f.write(f"\nThese positions have passed their market close time but are still open. "
                f"They may settle in the next batch, or Kalshi may not have finalized results yet. "
                f"P\u0026L below does NOT include these positions.\n\n")

    f.write(f"## Overall Performance\n\n")
    f.write(f"| Metric | Value |\n")
    f.write(f"|--------|-------|\n")
    f.write(f"| Starting Bank | ${starting/100:.2f} |\n")
    f.write(f"| Cash | ${cash/100:.2f} |\n")
    f.write(f"| Realized P&L (all-time) | **${realized_dollars:.2f}** |\n")
    f.write(f"| Return on Bank | {realized_dollars / (starting/100) * 100:.1f}% |\n")
    f.write(f"| Open Positions | {len(open_positions)} |\n")
    f.write(f"| Pending Makers | {len(pending)} |\n")
    f.write(f"| All-Time Settled | {len(settlements)} |\n")
    f.write(f"| All-Time Win Rate | {all_win_rate:.1f}% ({len(all_wins)}W/{len(all_losses)}L) |\n")
    if avg_brier is not None:
        f.write(f"| Brier Score (lower=better) | {avg_brier:.4f} |\n")
    f.write(f"\n")

    if today_settlements:
        f.write(f"## Today's Performance\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Settled Today | {len(today_settlements)} |\n")
        f.write(f"| Win Rate | {today_win_rate:.1f}% ({len(today_wins)}W/{len(today_losses)}L) |\n")
        f.write(f"| P&L | ${today_pnl/100:.2f} |\n")
        f.write(f"\n")

    f.write(f"## City Performance (All-Time)\n\n")
    f.write(f"| City | W | L | Win Rate | P&L |\n")
    f.write(f"|------|---|---|----------|-----|\n")
    for city, stats in sorted(by_city.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
        total = stats["w"] + stats["l"]
        wr = stats["w"] / total * 100 if total > 0 else 0
        f.write(f"| {city} | {stats['w']} | {stats['l']} | {wr:.0f}% | ${stats['pnl']/100:.2f} |\n")
    f.write(f"\n")

    if lessons:
        f.write(f"## Lessons Learned Today\n\n")
        for lesson in lessons:
            f.write(f"- {lesson}\n")
        f.write(f"\n")

    # Biggest winners and losers
    if settlements:
        f.write(f"## Biggest Trades (All-Time)\n\n")
        sorted_by_pnl = sorted(settlements, key=lambda s: s.get("pnl_cents", 0), reverse=True)
        f.write(f"**Top 3 Winners:**\n")
        for s in sorted_by_pnl[:3]:
            f.write(f"- {s.get('ticker')} {s.get('side')} +${s.get('pnl_cents', 0)/100:.2f}\n")
        f.write(f"\n**Top 3 Losers:**\n")
        for s in sorted_by_pnl[-3:]:
            f.write(f"- {s.get('ticker')} {s.get('side')} ${s.get('pnl_cents', 0)/100:.2f}\n")
        f.write(f"\n")

    f.write(f"## Notes\n\n")
    f.write(f"- Brier score calculated on {brier_count} unique settled predictions\n")
    f.write(f"- Review generated automatically. Check `state/paper-book.json` for raw data.\n")

print(f"Review written to: {review_path}")

# --- Print Discord summary ---
print(f"\n{'='*50}")
print(f"KALSHI WEATHER PAPER TRADER — DAILY REVIEW")
print(f"{'='*50}")
print(f"Date: {today_str}")
print(f"Data freshness: book={file_freshness(book_path)} | settlements={file_freshness(settlement_path)} | brier={file_freshness(brier_path)}")
if past_due_tickers:
    print(f"⚠️ {len(past_due_tickers)} position(s) past close time still open — P&L may be understated")
print(f"")
print(f"Bank: ${starting/100:.2f} → Cash: ${cash/100:.2f} | Realized: ${realized_dollars:.2f} ({realized_dollars/(starting/100)*100:.1f}% return)")
print(f"All-Time: {len(settlements)} settled | {len(all_wins)}W/{len(all_losses)}L ({all_win_rate:.1f}% WR)")
if today_settlements:
    print(f"Today: {len(today_settlements)} settled | {len(today_wins)}W/{len(today_losses)}L | P&L: ${today_pnl/100:.2f}")
if avg_brier is not None:
    print(f"Brier Score: {avg_brier:.4f} (on {brier_count} predictions)")
print(f"")
print(f"Top Cities:")
for city, stats in sorted(by_city.items(), key=lambda x: -(x[1]["w"] + x[1]["l"]))[:5]:
    total = stats["w"] + stats["l"]
    wr = stats["w"] / total * 100 if total > 0 else 0
    print(f"  {city}: {stats['w']}W/{stats['l']}L ({wr:.0f}%) ${stats['pnl']/100:.2f}")
print(f"{'='*50}")

# --- A/B arm standings + live-arm health (2026-06-21) ---
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ab_health import render as ab_render
    ab_block = ab_render()
    print(ab_block)
    with open(review_path, "a") as f:
        f.write("\n## A/B Arms — Standings\n\n```\n" + ab_block.strip() + "\n```\n")
except Exception as e:
    print(f"(ab_health unavailable: {e})")

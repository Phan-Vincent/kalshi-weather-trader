#!/usr/bin/env python3
"""
weekly-review.py — Compute weekly Kalshi Weather Paper Trader review from live state.

Reads directly from KALSHI_WEATHER_STATE_DIR (default state/paper — the live main book):
  - <state_dir>/paper-book.json (current book)
  - <state_dir>/settlement-log.jsonl (all settlements)
  - <state_dir>/brier-log.jsonl (all Brier records)
  - <state_dir>/risk-state.json (consecutive loss state)
  - LESSONS.md (recent systemic patterns)

Writes:
  - reviews/weekly-YYYY-MM-DD.md (the week ending on this Monday)

Includes data freshness timestamps and stale-data warnings.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "reviews"

def load_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def load_jsonl(path: Path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []

def file_freshness(path: Path):
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

def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def count_past_due_open(open_positions) -> list[str]:
    now = datetime.now(timezone.utc)
    past_due = []
    for pos in (open_positions.values() if isinstance(open_positions, dict) else open_positions):
        close_time = parse_utc(pos.get("market_close_time"))
        if close_time and close_time < now:
            past_due.append(pos.get("ticker", "unknown"))
    return past_due

def week_boundaries(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Return (week_start, week_end, week_label) where week ends on the most recent Monday."""
    if now is None:
        now = datetime.now(timezone.utc)
    # Week ends on Monday (0=Monday in weekday())
    days_since_monday = now.weekday()
    week_end = now - timedelta(days=days_since_monday, hours=now.hour, minutes=now.minute, seconds=now.second, microseconds=now.microsecond)
    week_start = week_end - timedelta(days=7)
    week_label = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
    return week_start, week_end, week_label

def main() -> int:
    # --- Paths ---
    # 2026-07-01 (QA-14): the root state/ is orphaned/frozen — each A/B arm now writes to
    # state/<arm>/ via KALSHI_WEATHER_STATE_DIR. Read the live main paper book at state/paper
    # (the same fix nightly-review.py got 2026-06-21) so the weekly Discord review isn't stale.
    state_dir = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", ROOT / "state" / "paper"))
    book_path = state_dir / "paper-book.json"
    settlement_path = state_dir / "settlement-log.jsonl"
    brier_path = state_dir / "brier-log.jsonl"
    risk_path = state_dir / "risk-state.json"
    lessons_path = ROOT / "LESSONS.md"

    # --- Load state ---
    book = load_json(book_path) or {}
    settlements = load_jsonl(settlement_path)
    brier_entries = load_jsonl(brier_path)
    risk = load_json(risk_path) or {}

    cash = book.get("cash_cents", 0)
    starting = book.get("starting_bank_cents", book.get("starting_cents", 30000))
    open_positions = book.get("open", [])
    closed = book.get("closed", [])
    pending = book.get("pending_makers", [])
    book_updated = book.get("updated_utc", "unknown")

    # --- Week boundaries ---
    week_start, week_end, week_label = week_boundaries()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # --- Filter this week's settlements ---
    week_settlements = []
    for s in settlements:
        settled_at = parse_utc(s.get("settled_at_utc"))
        if settled_at and week_start <= settled_at < week_end:
            week_settlements.append(s)

    # All-time stats
    all_realized = sum(s.get("pnl_cents", 0) for s in settlements)
    all_wins = [s for s in settlements if s.get("pnl_cents", 0) > 0]
    all_losses = [s for s in settlements if s.get("pnl_cents", 0) <= 0]
    all_win_rate = len(all_wins) / len(settlements) * 100 if settlements else 0

    # Week stats
    week_pnl = sum(s.get("pnl_cents", 0) for s in week_settlements)
    week_wins = [s for s in week_settlements if s.get("pnl_cents", 0) > 0]
    week_losses = [s for s in week_settlements if s.get("pnl_cents", 0) <= 0]
    week_win_rate = len(week_wins) / len(week_settlements) * 100 if week_settlements else 0

    # --- Previous week for comparison ---
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start
    prev_week_settlements = []
    for s in settlements:
        settled_at = parse_utc(s.get("settled_at_utc"))
        if settled_at and prev_week_start <= settled_at < prev_week_end:
            prev_week_settlements.append(s)
    prev_week_pnl = sum(s.get("pnl_cents", 0) for s in prev_week_settlements)
    prev_week_win_rate = (
        len([s for s in prev_week_settlements if s.get("pnl_cents", 0) > 0]) /
        len(prev_week_settlements) * 100
        if prev_week_settlements else None
    )

    # --- Brier score (on all settled entries) ---
    seen = set()
    unique_brier = []
    for b in brier_entries:
        key = (b.get("paper_order_id"), b.get("fill_time"), b.get("ticker"))
        if key not in seen:
            seen.add(key)
            unique_brier.append(b)

    brier_sum = 0
    brier_count = 0
    week_brier_sum = 0
    week_brier_count = 0
    for b in unique_brier:
        our_brier = b.get("our_brier")
        if our_brier is not None:
            brier_sum += our_brier
            brier_count += 1
            ts = parse_utc(b.get("timestamp_utc"))
            if ts and week_start <= ts < week_end:
                week_brier_sum += our_brier
                week_brier_count += 1
            continue
        outcome = b.get("outcome")
        if outcome is not None:
            prob = b.get("price", 0) / 100
            if b.get("side") == "no":
                prob = 1 - prob
            outcome_val = 1.0 if str(outcome).lower() == "yes" else 0.0
            brier = (prob - outcome_val) ** 2
            brier_sum += brier
            brier_count += 1
            ts = parse_utc(b.get("timestamp_utc"))
            if ts and week_start <= ts < week_end:
                week_brier_sum += brier
                week_brier_count += 1

    avg_brier = brier_sum / brier_count if brier_count > 0 else None
    week_avg_brier = week_brier_sum / week_brier_count if week_brier_count > 0 else None

    # --- City performance (this week) ---
    by_city_week = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for s in week_settlements:
        ticker = s.get("ticker", "")
        if "HIGHT" in ticker:
            city = ticker.split("HIGHT")[1].split("-")[0]
        elif "LOWT" in ticker:
            city = ticker.split("LOWT")[1].split("-")[0]
        else:
            city = "UNKNOWN"
        pnl = s.get("pnl_cents", 0)
        by_city_week[city]["pnl"] += pnl
        if pnl > 0:
            by_city_week[city]["w"] += 1
        else:
            by_city_week[city]["l"] += 1

    # All-time city performance (for context)
    by_city_all = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for s in settlements:
        ticker = s.get("ticker", "")
        if "HIGHT" in ticker:
            city = ticker.split("HIGHT")[1].split("-")[0]
        elif "LOWT" in ticker:
            city = ticker.split("LOWT")[1].split("-")[0]
        else:
            city = "UNKNOWN"
        pnl = s.get("pnl_cents", 0)
        by_city_all[city]["pnl"] += pnl
        if pnl > 0:
            by_city_all[city]["w"] += 1
        else:
            by_city_all[city]["l"] += 1

    # --- Stale data check ---
    past_due_tickers = count_past_due_open(open_positions)
    stale_warning = ""
    if past_due_tickers:
        stale_warning = (
            f"⚠️ **{len(past_due_tickers)} position(s) past market close time still open:** "
            f"{', '.join(sorted(set(past_due_tickers))[:5])}{'…' if len(set(past_due_tickers)) > 5 else ''}\n"
        )

    # --- Risk state ---
    risk_week_pnl = risk.get("week_pnl_cents", 0)
    risk_consecutive = risk.get("consecutive_losses", 0)
    risk_by_city = risk.get("consecutive_losses_by_city", {})

    # --- Recent lessons ---
    lessons = []
    if lessons_path.exists():
        with open(lessons_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("-"):
                    lessons.append(line)
    recent_lessons = lessons[-20:] if lessons else []

    # --- Write review ---
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    review_path = REVIEW_DIR / f"weekly-{today_str}.md"
    with open(review_path, "w") as f:
        f.write(f"# Kalshi Weather Paper Trader — Weekly Review\n\n")
        f.write(f"**Week:** {week_label}  \n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S PDT')}\n\n")

        # Data freshness section
        f.write(f"## Data Freshness\n\n")
        f.write(f"_Book last updated: {book_updated}_  \n")
        f.write(f"_Risk state last updated: {risk.get('updated_utc', 'unknown')}_\n\n")
        f.write(f"| Source | Age | Status |\n")
        f.write(f"|--------|-----|--------|\n")
        for path, label in [
            (book_path, "paper-book.json"),
            (settlement_path, "settlement-log.jsonl"),
            (brier_path, "brier-log.jsonl"),
            (risk_path, "risk-state.json"),
        ]:
            age = file_freshness(path)
            status = "✅ fresh" if age.endswith("m ago") or age.endswith("s ago") or age.endswith("h ago") else ("⚠️ old" if "d" in age else "❌ missing")
            f.write(f"| {label} | {age} | {status} |\n")
        f.write(f"\n")

        if stale_warning:
            f.write(f"### ⚠️ Stale Data Warning\n\n")
            f.write(stale_warning)
            f.write(
                f"\nThese positions have passed their market close time but are still open. "
                f"Week P&L does **NOT** include unrealized gains/losses on these positions. "
                f"Run `settle_paper.py` before trusting this P&L as final.\n\n"
            )

        # Overall performance
        f.write(f"## Overall Performance\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Starting Bank | ${starting/100:.2f} |\n")
        f.write(f"| Cash | ${cash/100:.2f} |\n")
        f.write(f"| Realized P&L (all-time) | **${all_realized/100:.2f}** |\n")
        f.write(f"| Return on Bank | {all_realized/starting*100:.1f}% |\n")
        f.write(f"| All-Time Settled | {len(settlements)} |\n")
        f.write(f"| All-Time Win Rate | {all_win_rate:.1f}% ({len(all_wins)}W/{len(all_losses)}L) |\n")
        if avg_brier is not None:
            f.write(f"| Brier Score (all-time) | {avg_brier:.4f} |\n")
        f.write(f"\n")

        # Week performance
        f.write(f"## This Week ({week_label})\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Settled This Week | {len(week_settlements)} |\n")
        f.write(f"| Win Rate | {week_win_rate:.1f}% ({len(week_wins)}W/{len(week_losses)}L) |\n")
        f.write(f"| P&L | ${week_pnl/100:.2f} |\n")
        if week_avg_brier is not None:
            f.write(f"| Brier Score | {week_avg_brier:.4f} |\n")
        if prev_week_pnl != 0 or prev_week_win_rate is not None:
            pnl_delta = (week_pnl - prev_week_pnl) / 100
            pnl_arrow = "↑" if pnl_delta >= 0 else "↓"
            f.write(f"| vs Previous Week P&L | {pnl_arrow} ${pnl_delta:+.2f} |\n")
        f.write(f"| Open Positions (end of week) | {len(open_positions)} |\n")
        f.write(f"| Pending Makers | {len(pending)} |\n")
        if past_due_tickers:
            f.write(f"| ⚠️ Unsettled (past close) | {len(set(past_due_tickers))} |\n")
        f.write(f"\n")

        # City performance this week
        if by_city_week:
            f.write(f"## City Performance (This Week)\n\n")
            f.write(f"| City | W | L | Win Rate | P&L |\n")
            f.write(f"|------|---|---|----------|-----|\n")
            for city, stats in sorted(by_city_week.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
                total = stats["w"] + stats["l"]
                wr = stats["w"] / total * 100 if total > 0 else 0
                f.write(f"| {city} | {stats['w']} | {stats['l']} | {wr:.0f}% | ${stats['pnl']/100:.2f} |\n")
            f.write(f"\n")

        # City performance all-time (brief)
        f.write(f"## City Performance (All-Time)\n\n")
        f.write(f"| City | W | L | Win Rate | P&L | Consec. Losses |\n")
        f.write(f"|------|---|---|----------|-----|----------------|\n")
        for city, stats in sorted(by_city_all.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
            total = stats["w"] + stats["l"]
            wr = stats["w"] / total * 100 if total > 0 else 0
            consec = risk_by_city.get(city, 0)
            consec_warn = f" ⚠️" if consec >= 5 else ""
            f.write(f"| {city} | {stats['w']} | {stats['l']} | {wr:.0f}% | ${stats['pnl']/100:.2f} | {consec}{consec_warn} |\n")
        f.write(f"\n")

        # Risk state
        f.write(f"## Risk State\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Consecutive Losses (global) | {risk_consecutive} |\n")
        f.write(f"| Week P&L (risk tracker) | ${risk_week_pnl/100:.2f} |\n")
        worst_cities = sorted(risk_by_city.items(), key=lambda x: -x[1])[:3]
        if worst_cities:
            f.write(f"| Worst Cities (consec. losses) | {', '.join(f'{c}={n}' for c, n in worst_cities)} |\n")
        f.write(f"\n")

        # Recent lessons
        if recent_lessons:
            f.write(f"## Recent Lessons (Last 20)\n\n")
            for lesson in recent_lessons:
                f.write(f"- {lesson}\n")
            f.write(f"\n")

        # Biggest week trades
        if week_settlements:
            f.write(f"## Biggest Trades This Week\n\n")
            sorted_by_pnl = sorted(week_settlements, key=lambda s: s.get("pnl_cents", 0), reverse=True)
            f.write(f"**Top 3 Winners:**\n")
            for s in sorted_by_pnl[:3]:
                f.write(f"- {s.get('ticker')} {s.get('side')} +${s.get('pnl_cents', 0)/100:.2f}\n")
            f.write(f"\n**Top 3 Losers:**\n")
            for s in sorted_by_pnl[-3:]:
                f.write(f"- {s.get('ticker')} {s.get('side')} ${s.get('pnl_cents', 0)/100:.2f}\n")
            f.write(f"\n")

        f.write(f"## Notes\n\n")
        f.write(f"- Brier score calculated on {brier_count} unique settled predictions (all-time), {week_brier_count} this week\n")
        f.write(f"- Week defined as Monday–Monday UTC (ends on the Monday this report is generated)\n")
        f.write(f"- Review generated directly from live state files at runtime\n")
        if past_due_tickers:
            f.write(f"- ⚠️ {len(set(past_due_tickers))} position(s) are past their close time and not yet settled\n")

    print(f"Review written to: {review_path}")

    # --- Discord summary ---
    print(f"\n{'='*50}")
    print(f"KALSHI WEATHER PAPER TRADER — WEEKLY REVIEW")
    print(f"{'='*50}")
    print(f"Week: {week_label}")
    print(f"Data freshness: book={file_freshness(book_path)} | settlements={file_freshness(settlement_path)} | brier={file_freshness(brier_path)}")
    if past_due_tickers:
        print(f"⚠️ {len(set(past_due_tickers))} position(s) past close time still open — week P&L may be understated")
    print(f"")
    print(f"Bank: ${starting/100:.2f} → Cash: ${cash/100:.2f} | All-Time Realized: ${all_realized/100:.2f} ({all_realized/starting*100:.1f}% return)")
    print(f"All-Time: {len(settlements)} settled | {len(all_wins)}W/{len(all_losses)}L ({all_win_rate:.1f}% WR)")
    print(f"This Week: {len(week_settlements)} settled | {len(week_wins)}W/{len(week_losses)}L | P&L: ${week_pnl/100:.2f}")
    if week_avg_brier is not None:
        print(f"Week Brier: {week_avg_brier:.4f}")
    if avg_brier is not None:
        print(f"All-Time Brier: {avg_brier:.4f}")
    if prev_week_pnl != 0 or prev_week_win_rate is not None:
        pnl_delta = (week_pnl - prev_week_pnl) / 100
        print(f"vs Previous Week: {'↑' if pnl_delta >= 0 else '↓'} ${pnl_delta:+.2f}")
    print(f"")
    print(f"Top Cities This Week:")
    for city, stats in sorted(by_city_week.items(), key=lambda x: -(x[1]["w"] + x[1]["l"]))[:5]:
        total = stats["w"] + stats["l"]
        wr = stats["w"] / total * 100 if total > 0 else 0
        print(f"  {city}: {stats['w']}W/{stats['l']}L ({wr:.0f}%) ${stats['pnl']/100:.2f}")
    print(f"Risk: {risk_consecutive} consecutive losses globally")
    worst = sorted(risk_by_city.items(), key=lambda x: -x[1])[:2]
    if worst:
        print(f"  Worst: {', '.join(f'{c}={n}' for c, n in worst)}")
    print(f"{'='*50}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

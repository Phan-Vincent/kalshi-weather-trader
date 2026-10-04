#!/usr/bin/env python3
"""
bin/postmortem_deep.py — Deep qualitative post-mortem of recent losses.

The rule-based `postmortem.py` handles classification and writes a one-line
lesson per loss. This script is the "deep" agent companion: it reads the last
N processed losses, looks for systemic patterns (recurring city, market type,
forecast model that keeps overshooting), and writes:

  - postmortems/_summaries/YYYY-MM-DD-deep.md  (qualitative analysis)
  - LESSONS.md gets a "systemic" lesson appended when a pattern is detected.

Designed to be invoked from a cron when the processed-loss log crosses a
threshold (e.g. 5+ losses since last deep review).

Usage:
  python3 bin/postmortem_deep.py                # default: review last 10
  python3 bin/postmortem_deep.py --window 20
  python3 bin/postmortem_deep.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from kalshi_weather.model.lessons import append_lesson, append_blacklist_systemic


STATE_DIR = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", ROOT / "state"))
PROCESSED = STATE_DIR / "loss-queue-processed.jsonl"
LESSONS_FILE = ROOT / "LESSONS.md"
SUMMARY_DIR = ROOT / "postmortems" / "_summaries"
LAST_REVIEW_FILE = STATE_DIR / "deep-postmortem-last.json"
MAX_LESSONS = 30


def _read_processed() -> list[dict]:
    if not PROCESSED.exists():
        return []
    out = []
    with open(PROCESSED) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _read_last_review() -> dict:
    if not LAST_REVIEW_FILE.exists():
        return {"last_reviewed_count": 0, "last_reviewed_utc": None}
    try:
        return json.loads(LAST_REVIEW_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_reviewed_count": 0, "last_reviewed_utc": None}


def _write_last_review(total: int) -> None:
    LAST_REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_REVIEW_FILE.write_text(json.dumps({
        "last_reviewed_count": total,
        "last_reviewed_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def analyze(losses: list[dict]) -> dict:
    """Look for systemic patterns across recent losses."""
    if not losses:
        return {"status": "no_losses_yet"}

    cities = Counter()
    failure_modes = Counter()
    by_city_failure: dict[str, Counter] = defaultdict(Counter)
    total_pnl_cents = 0
    side_counts = Counter()

    for L in losses:
        cls = L.get("classification", {})
        ticker = L.get("ticker", "")
        # Re-parse city if we have to
        parts = ticker.split("-")
        city = ""
        if parts and parts[0].startswith("KX"):
            # KXHIGHTHOU -> HOU
            prefix = parts[0]
            for tag in ["HIGHT", "LOWT", "TEMP"]:
                if tag in prefix:
                    city = prefix.split(tag, 1)[-1]
                    break
        cities[city or "?"] += 1
        failure_modes[cls.get("failure_mode", "unknown")] += 1
        if city:
            by_city_failure[city][cls.get("failure_mode", "unknown")] += 1
        side_counts[L.get("side", "?")] += 1
        total_pnl_cents += int(L.get("pnl_cents", 0))

    patterns = []
    # Concentration: any city > 50% of recent losses?
    most_city, most_city_count = cities.most_common(1)[0]
    if most_city_count >= max(3, len(losses) // 2) and most_city != "?":
        patterns.append({
            "type": "city_concentration",
            "city": most_city,
            "count": most_city_count,
            "of_total": len(losses),
            "lesson": (
                f"{most_city_count} of last {len(losses)} losses came from {most_city}. "
                f"Halve position size for {most_city} until win-rate recovers."
            ),
        })

    # Recurring failure mode
    most_mode, most_mode_count = failure_modes.most_common(1)[0]
    if most_mode_count >= max(3, len(losses) // 2):
        patterns.append({
            "type": "failure_mode_concentration",
            "mode": most_mode,
            "count": most_mode_count,
            "of_total": len(losses),
            "lesson": (
                f"{most_mode_count} of last {len(losses)} losses share mode '{most_mode}'. "
                f"Add explicit guardrail for this mode before next run."
            ),
        })

    # One-sided pattern: always wrong on the YES side or NO side
    if side_counts.get("yes", 0) >= max(3, len(losses) * 2 // 3):
        patterns.append({
            "type": "side_bias",
            "side": "yes",
            "lesson": "We keep losing on YES bets. Model may be persistently above market on the threshold direction. Trust NO side more for the next 24h.",
        })
    elif side_counts.get("no", 0) >= max(3, len(losses) * 2 // 3):
        patterns.append({
            "type": "side_bias",
            "side": "no",
            "lesson": "We keep losing on NO bets. Model may be persistently below market. Trust YES side more for the next 24h.",
        })

    return {
        "n_losses": len(losses),
        "total_pnl_dollars": total_pnl_cents / 100,
        "by_city": dict(cities),
        "by_failure_mode": dict(failure_modes),
        "by_side": dict(side_counts),
        "patterns_detected": patterns,
    }


def _write_summary(analysis: dict, losses: list[dict]) -> Path:
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    out = SUMMARY_DIR / f"{date}-deep.md"
    body = f"""# Deep post-mortem — {date}

**Losses reviewed:** {analysis.get('n_losses', 0)}
**Total drawdown reviewed:** ${analysis.get('total_pnl_dollars', 0):+.2f}

## By city
```json
{json.dumps(analysis.get('by_city', {}), indent=2)}
```

## By failure mode
```json
{json.dumps(analysis.get('by_failure_mode', {}), indent=2)}
```

## By side
```json
{json.dumps(analysis.get('by_side', {}), indent=2)}
```

## Patterns detected
"""
    patterns = analysis.get("patterns_detected", [])
    if not patterns:
        body += "\n_No systemic patterns above thresholds yet._\n"
    else:
        for p in patterns:
            body += f"\n### {p['type']}\n{json.dumps(p, indent=2)}\n\n**Lesson:** {p['lesson']}\n"

    body += "\n## Losses included\n```json\n" + json.dumps(losses, indent=2, default=str) + "\n```\n"
    out.write_text(body)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=10, help="How many recent losses to analyze")
    p.add_argument("--min-new", type=int, default=3, help="Skip if fewer than this many new losses since last review")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    all_losses = _read_processed()
    if not all_losses:
        print(json.dumps({"status": "no_processed_losses"}, indent=2))
        return 0

    state = _read_last_review()
    new_since_last = len(all_losses) - state.get("last_reviewed_count", 0)
    if new_since_last < args.min_new:
        print(json.dumps({
            "status": "skipped",
            "reason": f"only {new_since_last} new losses since last review",
            "total_processed": len(all_losses),
        }, indent=2))
        return 0

    recent = all_losses[-args.window:]
    analysis = analyze(recent)

    if args.dry_run:
        print(json.dumps(analysis, indent=2))
        return 0

    out = _write_summary(analysis, recent)
    for pat in analysis.get("patterns_detected", []):
        append_lesson(LESSONS_FILE, f"(SYSTEMIC) {pat['lesson']}", max_lessons=MAX_LESSONS)
    _write_last_review(len(all_losses))

    print(json.dumps({
        "status": "ok",
        "summary_path": str(out.relative_to(ROOT)),
        "patterns_count": len(analysis.get("patterns_detected", [])),
        "n_losses_reviewed": len(recent),
        "total_processed": len(all_losses),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
model/lessons.py — Centralized LESSONS.md parsing, deduplication, bias capping,
and blacklist detection.

Rules:
1. Dedupe: for a given (city, action_type, direction), keep only the most recent
   lesson when computing aggregate bias. Duplicate lines in LESSONS.md are
   ignored for bias calculation.
2. Bias cap: per-city temp_bias absolute value capped at 1.0°F (tightened from
   1.5→1.0 on 2026-06-18).
3. Blacklist: if a city accumulates 3+ lessons with the SAME sign bias
   (all warm-bias or all cold-bias), flag it as "blacklisted" and freeze
   bias at 0 for that city. A systemic lesson is appended when this happens.
4. Time decay: weight = 0.9 ** days_old.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_LESSONS = 30
BIAS_CAP_F = 1.0  # 2026-06-18: tightened from 1.5 → 1.0; model calibration shows LESSONS biases over-accumulate
BLACKLIST_THRESHOLD = 3  # same-city same-sign lessons before freeze


@dataclass
class ParsedLesson:
    raw_line: str
    timestamp_str: str
    weight: float
    cities: List[str]
    action_type: str  # "cold-bias", "warm-bias", "widen-std", "position-scale", "side-bias", "systemic", "other"
    direction: str  # "negative", "positive", "neutral" (for warm/cold bias)
    lesson_text: str  # text without the leading "- [timestamp] "


def _extract_timestamp(line: str) -> Tuple[Optional[str], float]:
    """Extract YYYY-MM-DD from a lesson line and compute weight."""
    match = re.search(r"\[([0-9]{4}-[0-9]{2}-[0-9]{2})\s", line)
    if not match:
        return None, 1.0
    try:
        date_str = match.group(1)
        lesson_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        days_old = (today - lesson_date).days
        return date_str, 0.9 ** max(0, days_old)
    except Exception:
        return None, 1.0


def _parse_lesson(line: str) -> Optional[ParsedLesson]:
    """Parse a single lesson line into structured form."""
    if not line.startswith("- ["):
        return None

    # Extract the text after "- [timestamp] "
    m = re.match(r"- \[([^\]]+)\]\s*(.*)", line)
    if not m:
        return None

    ts_str, text = m.group(1), m.group(2)
    date_str, weight = _extract_timestamp(line)

    # Extract cities (2-5 uppercase letters)
    cities = re.findall(r"\bfor ([A-Z]{2,5})\b", text)
    # Also catch city names in other patterns like "warm-bias the model for HOU"
    # The above regex handles "for XXXX" which is the standard pattern.
    # Also catch city at the start of the lesson text before "bin market"
    extra = re.findall(r"^([A-Z]{2,5})\s+(?:bin|daily|forecast|undershot)", text)
    for c in extra:
        if c not in cities:
            cities.append(c)

    # Determine action type and direction
    if "cold-bias" in text:
        action_type = "cold-bias"
        direction = "negative"
    elif "warm-bias" in text:
        action_type = "warm-bias"
        direction = "positive"
    elif "widen" in text and ("std" in text or "floor" in text):
        action_type = "widen-std"
        direction = "neutral"
    elif "Halve position size" in text or "Quarter position size" in text:
        action_type = "position-scale"
        direction = "neutral"
    elif "Trust NO side more" in text or "Trust YES side more" in text:
        action_type = "side-bias"
        direction = "neutral"
    elif "(SYSTEMIC)" in text or "blacklisted" in text:
        action_type = "systemic"
        direction = "neutral"
    else:
        action_type = "other"
        direction = "neutral"

    return ParsedLesson(
        raw_line=line,
        timestamp_str=ts_str,
        weight=weight,
        cities=cities,
        action_type=action_type,
        direction=direction,
        lesson_text=text,
    )


def parse_lessons(text: str) -> List[ParsedLesson]:
    """Parse all lesson lines from LESSONS.md text."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        parsed = _parse_lesson(line)
        if parsed:
            out.append(parsed)
    return out


def dedupe_lessons(lessons: List[ParsedLesson]) -> List[ParsedLesson]:
    """Keep only the most recent lesson per (city, action_type) for city-scoped
    lessons, and per normalized text for global lessons (side-bias, systemic).

    This preserves distinct systemic lessons while deduping identical bias actions.
    """
    seen: Dict[str, ParsedLesson] = {}
    for lesson in lessons:
        if not lesson.cities:
            # Global lessons: dedupe by normalized text (so distinct systemic lessons survive)
            key = lesson.lesson_text.strip()
        else:
            # City-scoped lessons: dedupe by (city, action_type)
            # Use the first city as the key (lessons typically target one city)
            city = lesson.cities[0]
            key = f"{city}:{lesson.action_type}"
        if key not in seen or lesson.weight > seen[key].weight:
            seen[key] = lesson
    # Return deduped list in original order (stable)
    deduped_set = set(id(v) for v in seen.values())
    return [l for l in lessons if id(l) in deduped_set]


def compute_biases(raw_lessons: List[ParsedLesson], deduped_lessons: List[ParsedLesson], error_tracker=None) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float], Optional[str], List[str]]:
    """Compute city_temp_bias_f, city_std_floor_f, city_position_scale, side_bias, and city_blacklist.

    Uses *deduped* lessons for bias calculation (so duplicates don't stack),
    but uses *raw* lessons for blacklist detection (so repeated same-sign
    failures are detected even when deduped).

    Returns:
        (city_temp_bias_f, city_std_floor_f, city_position_scale, side_bias, city_blacklist)
    """
    city_temp_bias_f: Dict[str, float] = {}
    city_std_floor_f: Dict[str, float] = {}
    city_position_scale: Dict[str, float] = {}
    side_bias: Optional[str] = None
    city_blacklist: List[str] = []

    # --- Blacklist detection: count from RAW lessons ---
    # FIX 2026-06-11 (audit-fair-value-model Finding 6): the old logic
    # blacklisted cities with ≥3 same-sign bias lessons, but that activates
    # on REPEATED failures — precisely when we need the MOST bias correction.
    # The correct intent is: blacklist when signals are MIXED (≥3 same-sign
    # AND ≥3 opposite-sign) because the model can't decide. Same-sign streaks
    # are evidence, not a reason to freeze.
    city_sign_counts: Dict[str, Dict[str, int]] = {}
    for lesson in raw_lessons:
        if lesson.action_type in ("cold-bias", "warm-bias"):
            for city in lesson.cities:
                sign = lesson.direction  # "positive" or "negative"
                city_sign_counts.setdefault(city, {}).setdefault(sign, 0)
                city_sign_counts[city][sign] += 1

    for city, counts in city_sign_counts.items():
        same_sign_max = max(counts.values()) if counts else 0
        # opposite-sign count
        opposite_sign_total = sum(c for s, c in counts.items()
                                  if s != max(counts, key=counts.get))
        # FIX 2026-06-12: previous "blacklist on mixed signals" logic was wrong.
        # It blacklisted EVERY city that had both warm-bias and cold-bias
        # lessons (which is all of them in summer — warm months push forecasts
        # high, but observed can still run low on cold fronts, or vice versa).
        # Effect: bias frozen at 0 for ~10 cities, and the EWMA error-model.json
        # (which has the real per-city bias values) was being thrown away.
        # Correct intent: blacklist only when we have a HIGH-VOLUME same-sign
        # streak (≥5 same-sign, ≤1 opposite-sign) AND the EWMA error model
        # also has very low sample size (<2 obs). That's truly no signal.
        ewma_n = 0
        try:
            ewma_n = error_tracker.get_effective_n(city) if error_tracker else 0
        except Exception:
            ewma_n = 0
        if (same_sign_max >= 5
                and opposite_sign_total <= 1
                and ewma_n < 2):
            if city not in city_blacklist:
                city_blacklist.append(city)

    # --- Bias calculation: use DEDUPED lessons ---
    for lesson in deduped_lessons:
        if lesson.action_type == "side-bias":
            if "Trust NO side more" in lesson.lesson_text:
                side_bias = "prefer_no"
            elif "Trust YES side more" in lesson.lesson_text:
                side_bias = "prefer_yes"
            continue

        if lesson.action_type == "systemic":
            if "blacklisted" in lesson.lesson_text:
                for city in lesson.cities:
                    if city not in city_blacklist:
                        city_blacklist.append(city)
            continue

        for city in lesson.cities:
            if lesson.action_type == "cold-bias":
                if city in city_blacklist:
                    continue  # freeze bias for blacklisted cities
                city_temp_bias_f[city] = city_temp_bias_f.get(city, 0.0) - 0.5 * lesson.weight
            elif lesson.action_type == "warm-bias":
                if city in city_blacklist:
                    continue  # freeze bias for blacklisted cities
                city_temp_bias_f[city] = city_temp_bias_f.get(city, 0.0) + 0.5 * lesson.weight
            elif lesson.action_type == "widen-std":
                city_std_floor_f[city] = max(city_std_floor_f.get(city, 0.0), 2.0)
            elif lesson.action_type == "position-scale":
                if "Halve" in lesson.lesson_text:
                    city_position_scale[city] = min(city_position_scale.get(city, 1.0), 0.5)
                elif "Quarter" in lesson.lesson_text:
                    city_position_scale[city] = min(city_position_scale.get(city, 1.0), 0.25)

    # Apply bias cap
    for city in list(city_temp_bias_f.keys()):
        city_temp_bias_f[city] = max(-BIAS_CAP_F, min(BIAS_CAP_F, city_temp_bias_f[city]))
    
    # Ensure blacklisted cities have explicit 0.0 bias
    for city in city_blacklist:
        city_temp_bias_f[city] = 0.0
    
    return city_temp_bias_f, city_std_floor_f, city_position_scale, side_bias, city_blacklist


def load_lessons(lessons_path: Path, error_tracker=None) -> dict:
    """Parse LESSONS.md and return structured lesson data with deduping, decay, capping, and blacklist.

    Returns:
        {
            "_raw": str,
            "city_temp_bias_f": {"HOU": -0.5, ...},
            "city_std_floor_f": {"HOU": 2.5, ...},
            "city_position_scale": {"HOU": 0.5, ...},
            "city_blacklist": ["HOU", ...],
            "side_bias": "prefer_no" | "prefer_yes" | None,
            "lines": [str, ...],
            "deduped_lines": [str, ...],
            "bias_hits_cap": ["HOU", ...],  # cities that hit the ±1.5F cap
        }
    """
    out = {
        "_raw": "",
        "city_temp_bias_f": {},
        "city_std_floor_f": {},
        "city_position_scale": {},
        "city_blacklist": [],
        "side_bias": None,
        "lines": [],
        "deduped_lines": [],
        "bias_hits_cap": [],
    }

    if not lessons_path.exists():
        return out

    try:
        text = lessons_path.read_text()
    except OSError:
        return out

    out["_raw"] = text
    parsed = parse_lessons(text)
    out["lines"] = [l.raw_line for l in parsed]

    deduped = dedupe_lessons(parsed)
    out["deduped_lines"] = [l.raw_line for l in deduped]

    city_temp_bias_f, city_std_floor_f, city_position_scale, side_bias, city_blacklist = compute_biases(parsed, deduped, error_tracker)
    out["city_temp_bias_f"] = city_temp_bias_f
    out["city_std_floor_f"] = city_std_floor_f
    out["city_position_scale"] = city_position_scale
    out["city_blacklist"] = city_blacklist
    out["side_bias"] = side_bias

    # Track which cities hit the cap
    for city, bias in city_temp_bias_f.items():
        if abs(bias) >= BIAS_CAP_F - 0.001:
            out["bias_hits_cap"].append(city)

    return out


def append_lesson(lessons_path: Path, lesson_line: str, max_lessons: int = MAX_LESSONS) -> dict:
    """Append a one-line lesson to LESSONS.md, deduping and keeping most recent N.

    If the same normalized lesson (ignoring timestamp) already exists, the
    append is skipped. Returns a status dict.
    """
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    new_line = f"- [{timestamp}] {lesson_line}"

    existing: List[str] = []
    if lessons_path.exists():
        text = lessons_path.read_text()
        existing = [l for l in text.splitlines() if l.startswith("- [")]

    # Deduplication check: normalize by stripping timestamp
    def _normalize(line: str) -> str:
        m = re.match(r"- \[[^\]]+\]\s*(.*)", line)
        return m.group(1) if m else line

    new_norm = _normalize(new_line)
    for existing_line in existing:
        if _normalize(existing_line) == new_norm:
            return {
                "status": "skipped_duplicate",
                "lessons_path": str(lessons_path),
                "normalized": new_norm,
            }

    existing.append(new_line)
    existing = existing[-max_lessons:]

    header = (
        "# Kalshi Weather Trader — Lessons\n\n"
        "Distilled from confirmed losses (`bin/postmortem.py`). The fair-value\n"
        "builder loads this file on every run to course-correct.\n\n"
    )
    lessons_path.write_text(header + "\n".join(existing) + "\n")

    return {
        "status": "appended",
        "lessons_path": str(lessons_path),
        "line": new_line,
    }


def append_blacklist_systemic(lessons_path: Path, city: str, reason: str) -> dict:
    """Append a systemic blacklisting lesson for a city."""
    lesson_line = f"(SYSTEMIC) {city} blacklisted: {reason}"
    return append_lesson(lessons_path, lesson_line)

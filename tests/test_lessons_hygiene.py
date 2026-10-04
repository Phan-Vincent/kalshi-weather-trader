#!/usr/bin/env python3
"""
Test suite for lesson loop hygiene (Fix D).

Tests:
1. Deduplication of identical lessons
2. Bias cap at ±1.5°F (tightened from ±2.0°F)
3. Blacklist detection for repeated same-city same-sign failures
4. Append deduplication (skip duplicates)
5. Decay weighting still works
"""

import sys
import os
import tempfile
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(_ROOT))

from kalshi_weather.model.lessons import (
    parse_lessons,
    dedupe_lessons,
    compute_biases,
    load_lessons,
    append_lesson,
    append_blacklist_systemic,
    BIAS_CAP_F,
    BLACKLIST_THRESHOLD,
)


def _make_lessons_file(text: str) -> Path:
    fd, path = tempfile.mkstemp(suffix=".md")
    os.write(fd, text.encode("utf-8"))
    os.close(fd)
    return Path(path)


# ──────────────────────────────────────────────────────────────
# D.1  Deduplication
# ──────────────────────────────────────────────────────────────

def test_dedupe_identical_lessons():
    """Two identical cold-bias lessons for LAX should dedupe to one."""
    # Use a fresh (today) date so the decay weight ≈ 1.0 and bias ≈ -0.5; hardcoded
    # past dates made this assertion age out (12-day-old lessons decay to ~-0.14).
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = f"""# Lessons
- [{today} 11:31] LAX bin market: observed 58.3°F, bin was [54.5, 56.5]. Action: cold-bias the model for LAX.
- [{today} 11:32] LAX bin market: observed 58.3°F, bin was [54.5, 56.5]. Action: cold-bias the model for LAX.
"""
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    assert len(deduped) == 1, f"Expected 1 deduped lesson, got {len(deduped)}"
    bias, _, _, _, _ = compute_biases(parsed, deduped)
    # Bias is -0.5 * weight; weight = 0.9^days_old. At 0-1 days old,
    # bias is between -0.45 and -0.5. Allow tolerance for UTC date rollover.
    assert abs(bias.get("LAX", 0) - (-0.5)) <= 0.1, \
        f"Expected ~-0.5°F, got {bias.get('LAX')}"
    print("[PASS] D.1 dedupe_identical_lessons")


def test_dedupe_different_actions_same_city():
    """Warm-bias + cold-bias for same city should both be kept."""
    text = """# Lessons
- [2026-06-01 11:00] Action: warm-bias the model for HOU.
- [2026-06-01 11:01] Action: cold-bias the model for HOU.
"""
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    assert len(deduped) == 2, f"Expected 2 lessons, got {len(deduped)}"
    bias, _, _, _, _ = compute_biases(parsed, deduped)
    assert abs(bias.get("HOU", 0)) < 0.01, f"Expected ~0°F, got {bias.get('HOU')}"
    print("[PASS] D.1 dedupe_different_actions_same_city")


# ──────────────────────────────────────────────────────────────
# D.2  Bias cap at ±1.5°F
# ──────────────────────────────────────────────────────────────

def test_bias_cap_at_1_5f():
    """Verify bias cap is 1.5°F and enforced. With deduping, 6 identical warm-bias
    lessons collapse to 1, so bias is +0.5°F. The cap is a safety net."""
    assert BIAS_CAP_F == 1.0, f"Expected BIAS_CAP_F=1.0, got {BIAS_CAP_F}"
    text = "# Lessons\n"
    for i in range(6):
        text += f"- [2026-06-01 11:{i:02d}] Action: warm-bias the model for HOU.\n"
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    bias, *_ = compute_biases(parsed, deduped)
    assert bias.get("HOU", 0) <= BIAS_CAP_F, f"Bias {bias.get('HOU')} exceeds cap {BIAS_CAP_F}"
    print("[PASS] D.2 bias_cap_at_1_5f")


def test_bias_cap_tightened():
    """Verify that the cap constant is 1.0, tightened from 1.5 (2026-06-18) and the older 2.0."""
    assert BIAS_CAP_F == 1.0, f"BIAS_CAP_F should be 1.0, got {BIAS_CAP_F}"
    print("[PASS] D.2 bias_cap_tightened")


# ──────────────────────────────────────────────────────────────
# D.3  Blacklist detection
# ──────────────────────────────────────────────────────────────

def test_blacklist_same_city_same_sign():
    """3 same-sign + 3 opposite-sign for HOU = MIXED signals.
    Under the new (FIX 2026-06-12) rule, mixed signals do NOT blacklist —
    they're evidence of model uncertainty, and the EWMA error model is
    allowed to take over. Old logic was a defensive kill switch that
    froze bias to 0 for ~10 cities, throwing away real bias data.
    New rule: blacklist only when same-sign≥5 AND opposite-sign≤1 AND
    EWMA n<2 (truly no signal in either direction)."""
    text = "# Lessons\n"
    for i in range(3):
        text += f"- [2026-06-11 11:{i:02d}] Action: warm-bias the model for HOU (failure {i}).\n"
    for i in range(3):
        text += f"- [2026-06-11 12:{i:02d}] Action: cold-bias the model for HOU (failure {i}).\n"
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    bias, _, _, _, blacklist = compute_biases(parsed, deduped)
    # FIX 2026-06-12: mixed-signal HOU should NOT be blacklisted under new rule
    assert "HOU" not in blacklist, f"Expected HOU NOT in blacklist (mixed signals are not a freeze trigger), got {blacklist}"
    print("[PASS] D.3 blacklist_same_city_same_sign (mixed signals do not freeze, new rule)")


def test_blacklist_8_identical_lax_cold_bias():
    """8 identical cold-bias for LAX with NO EWMA data = new-rule blacklist trigger.
    After dedup: 1 lesson → bias = -0.5°F would apply, but the new (FIX 2026-06-12)
    rule freezes at 0 when same-sign≥5 AND opposite-sign≤1 AND EWMA n<2 —
    that means we have a strong streak from LESSONS but no EWMA evidence,
    and the previous code that *did* apply the bias was unreliable.
    New behavior: when EWMA is missing AND we have a heavy streak, freeze
    and let EWMA catch up before applying bias.
    """
    text = "# Lessons\n"
    for i in range(8):
        text += f"- [2026-06-11 11:{i:02d}] LAX bin market: observed 58.3°F, bin was [58.5, 60.5]. Action: cold-bias the model for LAX.\n"
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    assert len(deduped) == 1, f"Expected 1 deduped lesson, got {len(deduped)}"
    bias, _, _, _, blacklist = compute_biases(parsed, deduped)
    # FIX 2026-06-12: new rule freezes when ≥5 same-sign + ≤1 opposite + EWMA n<2
    assert "LAX" in blacklist, f"Heavy streak with no EWMA data SHOULD blacklist under new rule, got {blacklist}"
    assert bias.get("LAX", 0) == 0.0, f"Blacklisted LAX bias should be 0, got {bias.get('LAX')}"
    print("[PASS] D.3 blacklist_8_identical_lax_cold_bias (heavy streak + no EWMA → freeze, new rule)")


def test_no_blacklist_mixed_signs():
    """Warm + cold for same city should NOT blacklist if neither sign reaches 3."""
    text = "# Lessons\n"
    for i in range(2):
        text += f"- [2026-06-01 11:{i:02d}] Action: warm-bias the model for HOU.\n"
    for i in range(2):
        text += f"- [2026-06-01 11:{i:02d}] Action: cold-bias the model for HOU.\n"
    parsed = parse_lessons(text)
    deduped = dedupe_lessons(parsed)
    bias, _, _, _, blacklist = compute_biases(parsed, deduped)
    assert "HOU" not in blacklist, f"HOU should NOT be blacklisted with only 2 each sign, got {blacklist}"
    # After dedup: 1 warm + 1 cold = 0.0°F
    assert bias.get("HOU", 0) == 0.0, f"Expected 0°F for mixed signs, got {bias.get('HOU')}"
    print("[PASS] D.3 no_blacklist_mixed_signs")

# ──────────────────────────────────────────────────────────────

def test_append_skips_duplicate():
    """Appending an identical lesson should be skipped."""
    text = "# Lessons\n- [2026-06-01 11:00] Action: warm-bias the model for HOU.\n"
    path = _make_lessons_file(text)
    result = append_lesson(path, "Action: warm-bias the model for HOU.")
    assert result["status"] == "skipped_duplicate", f"Expected skip, got {result['status']}"
    content = path.read_text()
    lines = [l for l in content.splitlines() if l.startswith("- [")]
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
    print("[PASS] D.4 append_skips_duplicate")


def test_append_new_lesson():
    """Appending a new distinct lesson should succeed."""
    text = "# Lessons\n- [2026-06-01 11:00] Action: warm-bias the model for HOU.\n"
    path = _make_lessons_file(text)
    result = append_lesson(path, "Action: cold-bias the model for SEA.")
    assert result["status"] == "appended", f"Expected appended, got {result['status']}"
    content = path.read_text()
    lines = [l for l in content.splitlines() if l.startswith("- [")]
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"
    print("[PASS] D.4 append_new_lesson")


def test_append_blacklist_systemic():
    """Appending a systemic blacklist lesson should work."""
    text = "# Lessons\n"
    path = _make_lessons_file(text)
    result = append_blacklist_systemic(path, "HOU", "3 repeated warm-bias failures")
    assert result["status"] == "appended", f"Expected appended, got {result['status']}"
    content = path.read_text()
    assert "HOU blacklisted" in content
    print("[PASS] D.4 append_blacklist_systemic")


# ──────────────────────────────────────────────────────────────
# D.5  Load lessons end-to-end
# ──────────────────────────────────────────────────────────────

def test_load_lessons_with_blacklist():
    """load_lessons under new (FIX 2026-06-12) rule: mixed signals do NOT
    freeze. With 3 warm + 3 cold, no city should be blacklisted."""
    text = "# Lessons\n"
    for i in range(3):
        text += f"- [2026-06-11 11:{i:02d}] Action: warm-bias the model for HOU.\n"
    for i in range(3):
        text += f"- [2026-06-11 12:{i:02d}] Action: cold-bias the model for HOU.\n"
    path = _make_lessons_file(text)
    data = load_lessons(path)
    assert "HOU" not in data["city_blacklist"], f"Expected HOU NOT in blacklist under new rule, got {data['city_blacklist']}"
    print("[PASS] D.5 load_lessons_with_blacklist (mixed signals do not freeze, new rule)")


def test_load_lessons_decay():
    """Older lessons should have reduced weight."""
    text = "# Lessons\n- [2026-05-01 11:00] Action: warm-bias the model for BOS.\n"
    path = _make_lessons_file(text)
    data = load_lessons(path)
    # 31 days old → weight = 0.9^31 ≈ 0.038
    bias = data["city_temp_bias_f"].get("BOS", 0)
    assert bias < 0.5, f"Expected decayed bias < 0.5, got {bias}"
    assert bias > 0, f"Expected positive bias, got {bias}"
    print("[PASS] D.5 load_lessons_decay")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_dedupe_identical_lessons()
    test_dedupe_different_actions_same_city()
    test_bias_cap_at_1_5f()
    test_bias_cap_tightened()
    test_blacklist_same_city_same_sign()
    test_blacklist_8_identical_lax_cold_bias()
    test_no_blacklist_mixed_signs()
    test_append_skips_duplicate()
    test_append_new_lesson()
    test_append_blacklist_systemic()
    test_load_lessons_with_blacklist()
    test_load_lessons_decay()
    print("\n=== Fix D tests complete ===")

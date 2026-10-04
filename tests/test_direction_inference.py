#!/usr/bin/env python3
"""
tests/test_direction_inference.py — Regression for the 2026-07-06 audit finding that
T-market direction was inferred from the title with a SILENT "above" default. A
phrasing the keyword list missed (e.g. "93° or lower") flipped a below-market to the
wrong tail, and in the trading path that posts max-size wrong-side orders across the
whole band. The parser now returns None on an undetermined title and the pricing path
(strict_direction=True) skips+alerts instead of guessing.

Run: python3 -m pytest tests/test_direction_inference.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.weather_data import _infer_direction_from_title, parse_market_ticker

TK = "KXHIGHTHOU-26JUL06-T93"


def test_explicit_inequalities():
    assert _infer_direction_from_title("high temp be <93° on Jul 6?", 93) == "below"
    assert _infer_direction_from_title("high temp be >93° on Jul 6?", 93) == "above"


def test_word_phrasings_including_previously_missed():
    assert _infer_direction_from_title("high of 93° or lower?", 93) == "below"
    assert _infer_direction_from_title("high of 93° or higher?", 93) == "above"
    assert _infer_direction_from_title("at most 93°?", 93) == "below"
    assert _infer_direction_from_title("at least 93°?", 93) == "above"
    assert _infer_direction_from_title("temperature under 93°?", 93) == "below"


def test_undetermined_returns_none():
    assert _infer_direction_from_title("", 93) is None
    assert _infer_direction_from_title("some unrelated headline", 93) is None
    # contradictory cues → undetermined, not a coin-flip default
    assert _infer_direction_from_title("above or lower nonsense", 93) is None


def test_strict_pricing_path_skips_undetermined():
    # Trading path: an unreadable title must RAISE (builder then skips the market),
    # never silently price the wrong tail.
    try:
        parse_market_ticker(TK, {"title": "blank headline with no direction"}, strict_direction=True)
        assert False, "strict path must raise on undetermined direction"
    except ValueError:
        pass


def test_lenient_diagnostic_path_defaults_above():
    # Non-trading fallback preserves the documented default (used by postmortem/settle).
    p = parse_market_ticker(TK, {"title": "blank headline"}, strict_direction=False)
    assert p["bin_kind"] == "above"
    # bare ticker (no raw_market) also lenient
    assert parse_market_ticker(TK)["bin_kind"] == "above"


def test_below_title_parses_below_in_both_modes():
    for strict in (True, False):
        p = parse_market_ticker(TK, {"title": "will the high be <93° on Jul 6?"}, strict_direction=strict)
        assert p["bin_kind"] == "below"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

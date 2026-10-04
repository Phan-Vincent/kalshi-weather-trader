#!/usr/bin/env python3
"""
tests/test_state_corruption_failclosed.py — Regression for the 2026-07-06 audit finding
that money-state loaders silently reset a corrupt/unreadable file to defaults AND then
persisted the amnesia — erasing the day's realized losses + manual city halts (risk-state)
or wiping the calibration-source book (paper-book). Loaders now fail CLOSED: back up the
corrupt bytes and raise, never overwrite recoverable state with zeros.

Run: python3 -m pytest tests/test_state_corruption_failclosed.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.risk import RiskGate, _load_json_state_or_raise
from trader.paper_book import PaperBook


def test_absent_file_is_fresh_start():
    with tempfile.TemporaryDirectory() as d:
        r = RiskGate(_state_dir=Path(d))  # no file yet → defaults, no raise
        assert r._today_pnl_cents == 0


def test_corrupt_risk_state_raises_and_preserves():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "risk-state.json"
        p.write_text('{"week_pnl_cents": -450, this is not json')  # corrupt but non-empty
        original = p.read_text()
        raised = False
        try:
            RiskGate(_state_dir=Path(d))
        except RuntimeError:
            raised = True
        assert raised, "corrupt risk-state must fail closed (raise), not reset to zeros"
        # Original bytes untouched (never overwritten with defaults)...
        assert p.read_text() == original
        # ...and a .corrupt-* backup exists for recovery.
        assert list(Path(d).glob("risk-state.json.corrupt-*")), "corrupt file must be backed up"


def test_corrupt_paper_book_raises():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "paper-book.json"
        p.write_text('{"cash_cents": 999999, GARBAGE')
        raised = False
        try:
            PaperBook(state_path=p)
        except RuntimeError:
            raised = True
        assert raised, "corrupt paper-book must fail closed, not reset to a fresh cash book"
        assert list(Path(d).glob("paper-book.json.corrupt-*"))


def test_empty_file_is_tolerated():
    # A 0-byte file (torn write) has nothing recoverable → defaults, no raise.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "risk-state.json"
        p.write_text("")
        assert _load_json_state_or_raise(p, "risk") == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

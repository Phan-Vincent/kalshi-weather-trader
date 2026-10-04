#!/usr/bin/env python3
"""H4 (AUDIT-REPORT-2026-06-23): the weekly loss-stop must reset on a true ISO calendar
week, not slide daily. Previously week_start=today−6d reset week_pnl every UTC day, so the
$150 weekly stop never spanned a week. Runs under plain python3."""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.risk import RiskGate  # noqa: E402


def _current_week_id():
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _gate_with_state(tmp, **state):
    base = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "today_pnl_cents": 0}
    base.update(state)
    (tmp / "risk-state.json").write_text(json.dumps(base))
    return RiskGate(_state_dir=tmp)


def test_same_iso_week_persists():
    # -$50 accumulated this ISO week → must NOT reset on reload (the old daily-slide bug).
    with tempfile.TemporaryDirectory() as d:
        g = _gate_with_state(Path(d), week_id=_current_week_id(), week_pnl_cents=-5000)
        assert g._week_id == _current_week_id()
        assert g._week_pnl_cents == -5000, "weekly P&L wrongly reset within the same ISO week"


def test_new_iso_week_resets():
    with tempfile.TemporaryDirectory() as d:
        g = _gate_with_state(Path(d), week_id="2000-W01", week_pnl_cents=-9000)
        assert g._week_id == _current_week_id()
        assert g._week_pnl_cents == 0, "weekly P&L must reset when the ISO week changes"


def test_legacy_week_start_migrates_without_spurious_reset():
    # Old files stored "week_start" (no "week_id"); migration seeds the current week id and
    # carries existing weekly P&L (one-time, no spurious reset).
    with tempfile.TemporaryDirectory() as d:
        g = _gate_with_state(Path(d), week_start="2026-06-10", week_pnl_cents=-3000)
        assert g._week_id == _current_week_id()
        assert g._week_pnl_cents == -3000


def test_week_id_survives_save_load():
    with tempfile.TemporaryDirectory() as d:
        g = _gate_with_state(Path(d), week_id=_current_week_id(), week_pnl_cents=-4200)
        g._save_state()
        g2 = RiskGate(_state_dir=Path(d))
        assert g2._week_id == _current_week_id()
        assert g2._week_pnl_cents == -4200


if __name__ == "__main__":
    test_same_iso_week_persists();                            print("[PASS] same ISO week → weekly P&L persists")
    test_new_iso_week_resets();                               print("[PASS] new ISO week → weekly P&L resets")
    test_legacy_week_start_migrates_without_spurious_reset(); print("[PASS] legacy week_start migrates cleanly")
    test_week_id_survives_save_load();                        print("[PASS] week_id survives save/load round-trip")
    print("\nAll weekly-window tests pass.")

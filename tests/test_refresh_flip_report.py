#!/usr/bin/env python3
"""Tests for bin/refresh_flip_report.py — the REPORT-ONLY quote-refresh flip panel (2026-07-13 F2).

Locks the pre-registered semantics agreed with the operator:
  • the trigger is the MATCHED (comparable-length) pre-window, NOT the all-time pre-flip history;
  • a matched drop within 10¢ ⇒ KEEP the flip (no auto-revert wired, ever);
  • the all-time drop is reported for CONTEXT and can exceed 10¢ without firing;
  • the A/B SIDE-SPLIT is surfaced (the real tell — loss concentrated on one side = correlated
    weather drawdown, not a refresh-execution effect).
No network. Plain settlement-log fixtures.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT.parent))

import refresh_flip_report as rf  # noqa: E402

_CITIES = ["NY", "LA", "CHI", "HOU", "MIA", "SEA", "DEN", "PHX", "ATL"]
FLIP = "2026-07-10T18:20:00Z"


def _r(city, day, hh, side, pnl):
    return {"ticker": f"KXHIGHT{city}-26JUL{day:02d}-B80.5", "side": side, "qty": 1,
            "pnl_cents": pnl, "opened_utc": f"2026-07-{day:02d}T{hh:02d}:00:00Z",
            "settlement_result": side}


def _write(state_dir: Path, rows):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _keep_scenario():
    """all-time pre ~0, matched pre ~-4¢, post ~-11¢ (NO -20 / YES +2) → matched drop ~7¢ (< 10, no
    fire) while the all-time drop ~11¢ (> 10, context-only). Distinct days keep events from straddling
    the flip."""
    rows = []
    for day in range(1, 8):                       # old pre-flip: benign +1¢
        for c in _CITIES:
            rows.append(_r(c, day, 6, "yes", 1))
    cnt = 0                                        # recent pre-flip (matched window): -4¢
    for day in (8, 9, 10):
        for c in _CITIES:
            if cnt >= 25:
                break
            rows.append(_r(c, day, 6, "no", -4)); cnt += 1
    no_cnt = yes_cnt = 0                            # post-flip: NO -20¢, YES +2¢
    for day in (11, 12, 13):
        for c in _CITIES:
            if no_cnt < 15:
                rows.append(_r(c, day, 19, "no", -20)); no_cnt += 1
            elif yes_cnt < 10:
                rows.append(_r(c, day, 19, "yes", 2)); yes_cnt += 1
    return rows


def test_matched_window_is_the_trigger_and_keeps_the_flip(tmp_path):
    _write(tmp_path, _keep_scenario())
    r = rf.evaluate(tmp_path, FLIP)
    assert r["evaluable"] is True                                  # >= 20 post events
    assert r["post_flip"]["ev_ct"] < r["pre_flip_matched"]["ev_ct"]  # post worse than matched pre
    assert 0 < r["drop_matched_c"] < r["drop_trigger_c"]           # matched drop within trigger
    assert r["matched_trigger_fires"] is False
    assert r["drop_all_time_c"] > r["drop_trigger_c"]              # all-time drop bigger (context)
    assert r["verdict"].startswith("KEEP FLIP")
    # side-split is the real tell: loss on the NO side, not the (refresh-touched) YES side
    assert r["post_by_side"]["no"]["ev_ct"] < r["post_by_side"]["yes"]["ev_ct"]


def test_matched_drop_over_trigger_flags_review_but_never_reverts(tmp_path):
    # If the MATCHED drop itself exceeds 10¢, the panel flags operator review — but it is still
    # report-only: evaluate() writes no state and there is no revert path.
    rows = []
    for day in range(1, 8):
        for c in _CITIES:
            rows.append(_r(c, day, 6, "yes", 0))       # matched pre ~0
    cnt = 0
    for day in (8, 9, 10):
        for c in _CITIES:
            if cnt >= 22:
                break
            rows.append(_r(c, day, 6, "yes", 0)); cnt += 1
    for day in (11, 12, 13):                            # post catastrophic both sides ~-15¢
        for c in _CITIES:
            rows.append(_r(c, day, 19, "no" if (day % 2) else "yes", -15))
    _write(tmp_path, rows)
    r = rf.evaluate(tmp_path, FLIP)
    assert r["drop_matched_c"] > r["drop_trigger_c"]
    assert r["matched_trigger_fires"] is True
    assert "no auto-revert" in r["verdict"]
    assert not (tmp_path / "refresh-flip-report.json").exists()    # evaluate() writes nothing


def test_not_evaluable_under_min_events(tmp_path):
    # Too few post-flip events → not evaluable, trigger cannot fire (keeps the flip by default).
    rows = [_r(c, 11, 19, "no", -30) for c in _CITIES[:5]]         # 5 post events < 20
    rows += [_r(c, 5, 6, "yes", 0) for c in _CITIES]               # some pre history
    _write(tmp_path, rows)
    r = rf.evaluate(tmp_path, FLIP)
    assert r["evaluable"] is False
    assert r["matched_trigger_fires"] is False

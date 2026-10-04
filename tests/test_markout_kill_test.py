#!/usr/bin/env python3
"""Markout kill-test tests (quant review 2026-07-01, experiment #5).

Pins: weather-only filter excludes the Iran commingle; short-horizon markout uses the [2-8h]
window + per-position dedup; the verdict is KILL when realized net EV/ct upper-CI < 0 and
INCONCLUSIVE when the CI spans 0; the captured-half-spread N/A path (no P0b telemetry) doesn't
crash. No network; tmp dirs.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import markout_kill_test as kt   # noqa: E402


def _sd(tmp_path, markout=None, settle=None, life=None):
    sd = tmp_path / "live-premium"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "markout-log.jsonl").write_text("\n".join(json.dumps(r) for r in (markout or [])))
    (sd / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in (settle or [])))
    (sd / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(r) for r in (life or [])))
    return sd


def _mk(ticker, fill_px, cur_mid, age=5.0, side="yes", oid=None):
    return {"ticker": ticker, "side": side, "fill_px": fill_px, "cur_mid": cur_mid,
            "age_hours": age, "paper_order_id": oid, "opened_utc": f"o-{ticker}-{oid}"}


def _posted(ticker, side="yes", spread=None):
    r = {"event": "posted_live", "ticker": ticker, "side": side}
    if spread is not None:
        r["spread_cents_at_post"] = spread
    return r


def test_weather_filter_excludes_iran(tmp_path):
    mk = [_mk("KXHIGHTNY-26JUL01-B80.5", 40, 34, oid="a"),      # weather, adverse -6
          _mk("KXUSAIRANAGREEMENT-27-26SEP", 68, 40, oid="b")]  # non-weather → excluded
    life = [_posted("KXHIGHTNY-26JUL01-B80.5"), _posted("KXUSAIRANAGREEMENT-27-26SEP")]
    r = kt.evaluate(_sd(tmp_path, markout=mk, life=life))
    assert r["markout_short_horizon"]["n_positions"] == 1   # Iran dropped


def test_empty_posted_set_yields_na_markout_not_all_attributed(tmp_path):
    # Regression (mirrors the live_paper_gap.py fix): weather markout rows exist, but there are NO
    # posted_live rows in maker-lifecycle → empty attribution set. An empty set must NOT be coerced
    # to None (which would attribute markout to ALL rows, re-admitting the non-weather commingle);
    # it must yield n/a markout — nothing attributed.
    mk = [_mk("KXHIGHTNY-26JUL01-B80.5", 40, 34, oid="a"),      # weather, adverse -6
          _mk("KXHIGHTLAX-26JUL01-B95.5", 50, 45, oid="b")]     # weather, adverse -5
    r = kt.evaluate(_sd(tmp_path, markout=mk, life=[]))          # no posted_live rows
    mo = r["markout_short_horizon"]
    assert mo["n_positions"] == 0                                 # nothing attributed (not 2)
    assert mo["ci"][0] is None                                    # CI n/a
    assert mo["ref_mean"] is None and mo["ref_n"] == 0            # canonical cross-check also n/a


def test_short_horizon_window_and_dedup(tmp_path):
    # one position, three obs: out-of-window (22h) dropped, and of the two in-window the one
    # closest to the 5h centre wins.
    tk = "KXHIGHTNY-26JUL02-B80.5"
    mk = [_mk(tk, 40, 20, age=22.0, oid="a"),   # near-settlement → excluded
          _mk(tk, 40, 33, age=4.5, oid="a"),    # closest to 5h → chosen (drift -7)
          _mk(tk, 40, 30, age=8.0, oid="a")]
    life = [_posted(tk)]
    r = kt.evaluate(_sd(tmp_path, markout=mk, life=life))
    assert r["markout_short_horizon"]["n_positions"] == 1
    assert abs(r["markout_short_horizon"]["ci"][0] - (-7.0)) < 1e-9


def test_verdict_kill_on_confirmed_negative_net_edge(tmp_path):
    # 40 varied events, all clearly losing (~-30¢/ct) → the ANYTIME-VALID CS upper bound < 0 → KILL.
    # (The kill now keys on the anytime CS, not the fixed-n bootstrap, so it needs enough varied
    # events to exclude 0 under continuous monitoring — the correct, more-conservative behavior.)
    settle = [{"ticker": f"KXHIGHTNY-26JUL{e:02d}-B80.5", "side": "yes", "qty": 2,
               "pnl_cents": -60 + (e % 5 - 2) * 6, "entry_cents": 40} for e in range(40)]
    r = kt.evaluate(_sd(tmp_path, settle=settle))
    assert r["net_ev_per_ct"]["cs"][2] < 0, r["net_ev_per_ct"]   # anytime CS upper bound < 0
    assert r["verdict"].startswith("KILL"), r["verdict"]


def test_verdict_inconclusive_when_ci_spans_zero(tmp_path):
    settle = [{"ticker": f"KXHIGHTNY-26JUL{e:02d}-B80.5", "side": "yes", "qty": 2,
               "pnl_cents": (120 if e % 2 else -120), "entry_cents": 40} for e in range(12)]
    r = kt.evaluate(_sd(tmp_path, settle=settle))
    lo, hi = r["net_ev_per_ct"]["ci"][1], r["net_ev_per_ct"]["ci"][2]
    assert lo < 0 < hi
    assert r["verdict"].startswith("INCONCLUSIVE"), r["verdict"]


def test_half_spread_na_path_does_not_crash(tmp_path):
    # posted_live rows without spread_cents_at_post (pre-P0b) → captured half-spread N/A, no crash
    settle = [{"ticker": "KXHIGHTNY-26JUL01-B80.5", "side": "yes", "qty": 2,
               "pnl_cents": 20, "entry_cents": 40}]
    life = [_posted("KXHIGHTNY-26JUL01-B80.5")]   # no spread field
    r = kt.evaluate(_sd(tmp_path, settle=settle, life=life))
    assert r["captured_half_spread"]["mean_c"] is None
    assert kt.main is not None   # smoke


def test_half_spread_computed_when_telemetry_present(tmp_path):
    life = [_posted("KXHIGHTNY-26JUL01-B80.5", spread=8), _posted("KXHIGHTLAX-26JUL01-B95.5", spread=12)]
    r = kt.evaluate(_sd(tmp_path, life=life))
    assert abs(r["captured_half_spread"]["mean_c"] - 5.0) < 1e-9   # (8/2 + 12/2)/2 = 5
    assert r["captured_half_spread"]["n"] == 2

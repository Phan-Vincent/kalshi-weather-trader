#!/usr/bin/env python3
"""Tests for bin/yes_lever_decision.py — the standing, re-runnable YES live-skip lever decision.

Locks the properties that make it a faithful, non-overfitting reproduction of the pre-registered
2026-07-05 packet (reviews/yes-lever-decision-2026-07-05.md):
  • the gate FIRES only when ALL 3 conditions + both prerequisites + drop-top-3 robustness hold;
  • the gate estimand is CONTRACT-WEIGHTED (the size-dampened per-trade mean must not be the gate,
    or the big-lot bleed the lever targets vanishes);
  • the drop-top-3 guard blocks a concentration-only "deficit" from firing;
  • --settled-through parses variable fractional-second timestamps (python3.9 fromisoformat bug);
  • non-weather rows are excluded; transition alerts fire once, not every cycle.
Plus a reproduction anchor: the real settlement log filtered to ≤2026-07-05 must reproduce the
packet's counts + 1-of-3 HOLD verdict. No network.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import yes_lever_decision as yl   # noqa: E402

_CITIES = ["NY", "LA", "CHI", "HOU", "MIA", "SEA", "DEN", "PHX", "ATL"]
_B = 400   # small bootstrap for snappy tests; engineered cases are clear-cut


def _mk(side, n_events, pnl_per_ct, qty=2, spread=1.0, day_base=4, strike="B80.5", jitter=True):
    """n_events distinct weather events (city×day), all post-fix (day≥04), at ~pnl_per_ct ¢/ct."""
    rows = []
    for i in range(n_events):
        city = _CITIES[i % len(_CITIES)]
        day = day_base + i // len(_CITIES)
        assert day <= 28, "test would produce an invalid ticker date"
        j = ((i % 3) - 1) * 2 if jitter else 0        # ±2¢ so the bootstrap isn't degenerate
        r = {"ticker": f"KXHIGHT{city}-26JUL{day:02d}-{strike}", "side": side, "qty": qty,
             "pnl_cents": (pnl_per_ct + j) * qty, "entry_cents": 40,
             "settled_at_utc": "2026-07-08T00:00:00Z"}
        if spread is not None:
            r["spread_cents_at_post"] = spread
        rows.append(r)
    return rows


def _write(state_dir: Path, rows: list[dict]):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _fire_dataset():
    """All conditions engineered to FIRE: YES uniformly ≈−20¢/ct, NO ≈+20¢/ct, 45 post-fix events
    each side, spread tagged."""
    return _mk("yes", 45, -20, spread=1.0, day_base=4) + _mk("no", 45, +20, spread=1.0, day_base=10)


# ── gate logic ───────────────────────────────────────────────────────

def test_all_conditions_fire_yields_fire_verdict(tmp_path):
    _write(tmp_path, _fire_dataset())
    d = yl.evaluate(tmp_path, B=_B)
    assert d["gate"]["i_ge40_yes_events"] and d["gate"]["ii_yes_ev_confidently_neg"] \
        and d["gate"]["iii_yes_confidently_lt_no"], d["gate"]
    assert d["prerequisites"]["regime_usable_ge40_postfix"] and d["prerequisites"]["spread_telemetry_present"]
    assert d["robustness_flags"]["survives_drop_top3"]
    assert d["verdict"] == "FIRE-YES-LEVER", d


def test_noisy_yes_spans_zero_holds(tmp_path):
    # YES alternating +40/−40 ¢/ct → mean ~0, CI spans 0 → condition (ii) fails → HOLD
    rows = []
    for i in range(45):
        city, day = _CITIES[i % 9], 4 + i // 9
        rows.append({"ticker": f"KXHIGHT{city}-26JUL{day:02d}-B80.5", "side": "yes", "qty": 2,
                     "pnl_cents": (80 if i % 2 else -80), "entry_cents": 40,
                     "settled_at_utc": "2026-07-08T00:00:00Z", "spread_cents_at_post": 1.0})
    rows += _mk("no", 45, +20, day_base=13)
    _write(tmp_path, rows)
    d = yl.evaluate(tmp_path, B=_B)
    assert not d["gate"]["ii_yes_ev_confidently_neg"]
    assert d["verdict"] == "HOLD"


def test_missing_spread_prereq_holds(tmp_path):
    _write(tmp_path, _mk("yes", 45, -20, spread=None, day_base=4) + _mk("no", 45, +20, spread=None, day_base=10))
    d = yl.evaluate(tmp_path, B=_B)
    assert d["gate"]["ii_yes_ev_confidently_neg"]                      # edge is there…
    assert not d["prerequisites"]["spread_telemetry_present"]          # …but the prereq isn't
    assert d["verdict"] == "HOLD"


def _write_lifecycle(state_dir: Path, settle_rows: list[dict], *, spread=5):
    """maker-lifecycle.jsonl posted_live rows carrying spread_cents_at_post for each (ticker, side)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lc = [{"event": "posted_live", "ticker": r["ticker"], "side": r["side"], "ts": "2026-07-07T00:00:00Z",
           **({"spread_cents_at_post": spread} if spread is not None else {})} for r in settle_rows]
    (state_dir / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(x) for x in lc))


def test_spread_telemetry_joined_from_lifecycle_log(tmp_path):
    # PRODUCTION REALITY (2026-07-13 fix): spread_cents_at_post is logged ONLY in maker-lifecycle.jsonl,
    # NEVER in settlement-log.jsonl. Pre-fix, load_rows read only the settlement log, so the settled
    # rows carried no spread, spread_telemetry_present was permanently False, and FIRE-YES-LEVER was
    # unreachable regardless of the statistics. The settlement rows here carry NO spread; the telemetry
    # lives only in the lifecycle log and must be joined by (ticker, side) for the gate to fire.
    settle = _mk("yes", 45, -20, spread=None, day_base=4) + _mk("no", 45, +20, spread=None, day_base=10)
    _write(tmp_path, settle)
    _write_lifecycle(tmp_path, settle)
    rows = yl.load_rows(tmp_path)
    assert sum(1 for r in rows if r.get("spread_cents_at_post") is not None) > 0, "join must tag settled rows"
    d = yl.evaluate(tmp_path, B=_B)
    assert d["prerequisites"]["spread_telemetry_present"], d["prerequisites"]
    assert d["verdict"] == "FIRE-YES-LEVER", d


def test_spread_prereq_false_when_lifecycle_untagged(tmp_path):
    # Control: the join must not FABRICATE coverage — settlement rows without spread AND a lifecycle log
    # whose posts also lack spread_cents_at_post → nothing to join → prerequisite stays False → HOLD.
    settle = _mk("yes", 45, -20, spread=None, day_base=4) + _mk("no", 45, +20, spread=None, day_base=10)
    _write(tmp_path, settle)
    _write_lifecycle(tmp_path, settle, spread=None)
    d = yl.evaluate(tmp_path, B=_B)
    assert not d["prerequisites"]["spread_telemetry_present"], d["prerequisites"]
    assert d["verdict"] != "FIRE-YES-LEVER"


def test_insufficient_postfix_events_holds(tmp_path):
    # 45 YES events but only 18 post-fix (rest dated 26JUN, before the 26JUL04 cutoff)
    pre = _mk("yes", 27, -20, day_base=4)
    for i, r in enumerate(pre):
        r["ticker"] = f"KXHIGHT{_CITIES[i % 9]}-26JUN{4 + i // 9:02d}-B80.5"   # pre-fix dates
    post = _mk("yes", 18, -20, day_base=4)
    _write(tmp_path, pre + post + _mk("no", 45, +20, day_base=12))
    d = yl.evaluate(tmp_path, B=_B)
    assert d["gate"]["i_ge40_yes_events"]                             # ≥40 total events
    assert not d["prerequisites"]["regime_usable_ge40_postfix"]       # but <40 POST-FIX
    assert d["counts"]["yes_postfix_events"] == 18
    assert d["verdict"] == "HOLD"


def test_drop_top3_guard_blocks_concentration_only_deficit(tmp_path):
    """A deficit that lives ENTIRELY in 3 large-lot events must NOT fire: the contract-weighted gate
    may read negative, but drop-top-3 removes it → survives_drop_top3 False → HOLD. This is the
    overfit the packet warned about."""
    flat = _mk("yes", 42, 0, qty=2, day_base=4, jitter=False)          # 42 events at ~0
    whales = _mk("yes", 3, -300, qty=30, day_base=14, jitter=False)    # 3 huge-lot losers
    _write(tmp_path, flat + whales + _mk("no", 45, +20, day_base=18))
    d = yl.evaluate(tmp_path, B=_B)
    assert not d["robustness_flags"]["survives_drop_top3"], d["robustness"]["drop_top3"]
    assert d["verdict"] == "HOLD"


# ── estimand: contract-weighted gate surfaces the big-lot bleed ──────

def test_contract_weight_is_more_negative_than_pertrade_when_big_lots_lose(tmp_path):
    """The gate must be contract-weighted: big losing lots + small winning lots → contract-wt point is
    MORE negative than the size-dampened per-trade mean. If they were equal the size signal is lost."""
    small_win = _mk("yes", 30, +6, qty=2, day_base=4, jitter=False)    # many small winners
    big_lose = _mk("yes", 30, -30, qty=20, day_base=10, jitter=False)  # fewer, bigger losers
    _write(tmp_path, small_win + big_lose + _mk("no", 20, +5, day_base=18))
    d = yl.evaluate(tmp_path, B=_B)
    cw = d["yes_ev_ct"]["contract_weighted_percentile"]["point"]
    pt = d["yes_ev_ct"]["pertrade_mean_bca"]["point"]
    assert cw < pt - 3.0, (cw, pt)     # contract weighting is materially more negative
    qs = d["robustness"]["qty_split"]
    assert qs["qty<=5"]["ev_ct_contract_wt"] > 0 > qs["qty>=6"]["ev_ct_contract_wt"]


# ── parser + filters ────────────────────────────────────────────────

def test_settled_through_parses_variable_fractional_seconds(tmp_path):
    """Regression: datetime.fromisoformat on python3.9 rejects 4/5-digit fractions; the as-of filter
    must still include such rows (14% of the real log)."""
    rows = _mk("yes", 2, -10, day_base=4)
    rows[0]["settled_at_utc"] = "2026-07-05T11:31:23.8307Z"    # 4-digit fraction, ON the cutoff
    rows[1]["settled_at_utc"] = "2026-07-06T11:31:23.33542Z"   # 5-digit fraction, AFTER the cutoff
    _write(tmp_path, rows)
    kept = yl.load_rows(tmp_path, settled_through=date(2026, 7, 5))
    assert len(kept) == 1 and kept[0]["settled_at_utc"].startswith("2026-07-05")


def test_non_weather_rows_excluded(tmp_path):
    rows = _mk("yes", 6, -10, day_base=4)
    rows.append({"ticker": "KXUSAIRANAGREEMENT-27-26SEP", "side": "yes", "qty": 173,
                 "pnl_cents": -9999, "entry_cents": 68, "settled_at_utc": "2026-07-08T00:00:00Z"})
    _write(tmp_path, rows)
    d = yl.evaluate(tmp_path, B=_B)
    assert d["counts"]["yes_positions"] == 6            # Iran row dropped
    assert d["counts"]["n_settled"] == 6


# ── transition-gated alerts (fire once, not every cycle) ─────────────

def test_regime_unblock_alert_fires_once(tmp_path, monkeypatch):
    sd = tmp_path / "live-premium"
    monkeypatch.setattr(yl, "STATE_DIR", sd)
    monkeypatch.setattr(yl, "STATE_PATH", sd / "yes-lever-state.json")
    calls = []
    monkeypatch.setattr(yl, "_alert", lambda msg, key: calls.append(key))
    _write(sd, _fire_dataset())                        # ≥40 post-fix events → regime unblocked
    monkeypatch.setattr(sys, "argv", ["yes_lever_decision.py", "--state-dir", str(sd), "--write", "--bootstrap", str(_B)])
    yl.main()
    assert "yes_lever_regime" in calls
    calls.clear()
    yl.main()                                          # second cycle: already alerted → silent
    assert "yes_lever_regime" not in calls


# ── reproduction anchor: matches the frozen 2026-07-05 packet ────────

def test_reproduces_frozen_packet_as_of_2026_07_05():
    """The real live-premium log filtered to ≤2026-07-05 must reproduce the packet: 119 settled
    (79 YES / 40 NO), 41 YES events, gate 1-of-3 (i✅ ii❌ iii❌), HOLD, top-3≈66% of the deficit,
    qty≥6 ≪ qty≤5. Asserts ranges (not bootstrap decimals). Skips if the prod log is absent."""
    log = ROOT / "state" / "live-premium" / "settlement-log.jsonl"
    if not log.exists():
        pytest.skip("production settlement log not present")
    d = yl.evaluate(ROOT / "state" / "live-premium", settled_through=date(2026, 7, 5), B=2000)
    c = d["counts"]
    assert (c["n_settled"], c["yes_positions"], c["no_positions"], c["yes_events"]) == (119, 79, 40, 41), c
    assert d["gate"]["i_ge40_yes_events"] is True
    assert d["gate"]["ii_yes_ev_confidently_neg"] is False       # YES CI spans 0
    assert d["gate"]["iii_yes_confidently_lt_no"] is False       # YES−NO spans 0
    assert d["gate_conditions_met"] == 1 and d["verdict"] == "HOLD"
    yes_pt = d["yes_ev_ct"]["contract_weighted_percentile"]["point"]
    assert -9.0 < yes_pt < -5.0, yes_pt                          # ≈ −6.9 (packet −6.38 + 7/6 rebate fix)
    assert 0.60 <= d["robustness"]["drop_top3"]["top3_share_of_deficit"] <= 0.72
    qs = d["robustness"]["qty_split"]
    assert qs["qty>=6"]["ev_ct_contract_wt"] < -10 < 0 < qs["qty<=5"]["ev_ct_contract_wt"]

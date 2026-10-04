#!/usr/bin/env python3
"""Tests for bin/pocket_lever_decision.py — the pre-registered pocket-lever gate.

Locks the properties that keep it a governed decision, not a data-mined filter flip:
  • a pocket FIRES only when ALL 5 gate conditions hold AND the futility n=200 verdict exists;
  • the futility prerequisite clamps otherwise-firing pockets to HOLD (blocked-pre-futility),
    fail-closed on unreadable futility state;
  • Westfall–Young across the 13-pocket family blocks a marginal pocket that a naive per-pocket
    CI would flag;
  • the --fix-cutoff regime clock excludes pre-cutoff events from the whole evaluation;
  • drop-top-3 blocks a concentration-only deficit;
  • the frozen family hash refuses to evaluate after a definition edit;
  • classifier copies are pinned to fill_edge_breakdown's (drift = CI failure, not a forked estimand);
  • the fingerprint guard skips the bootstrap when nothing decision-relevant changed;
  • transition alerts fire once, not every cycle.
No network.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import pocket_lever_decision as pl   # noqa: E402
import fill_edge_breakdown as feb    # noqa: E402

_CITIES = ["NY", "LA", "CHI", "HOU", "MIA", "SEA", "DEN", "PHX", "ATL"]
_B = 400   # small bootstrap for snappy tests; engineered cases are clear-cut


def _mk(n_events, pnl_per_ct, strike="T90", qty=2, entry=30, kind="HIGHT",
        day_base=1, lead_h=30, jitter=True):
    """n_events distinct weather events at ~pnl_per_ct ¢/ct. Defaults land in pockets HIGH/T,
    26-40c, 24-48h. Vary strike/entry/lead_h to move pockets."""
    rows = []
    for i in range(n_events):
        city = _CITIES[i % len(_CITIES)]
        day = day_base + i // len(_CITIES)
        assert day <= 28, "test would produce an invalid ticker date"
        j = ((i % 3) - 1) * 2 if jitter else 0        # ±2¢ so the bootstrap isn't degenerate
        assert lead_h < 48
        settled = (f"2026-07-02T{lead_h - 24:02d}:00:00Z" if lead_h >= 24
                   else f"2026-07-01T{lead_h:02d}:00:00Z")
        rows.append({
            "ticker": f"KX{kind}{city}-26JUL{day:02d}-{strike}",
            "side": "yes", "qty": qty, "pnl_cents": (pnl_per_ct + j) * qty,
            "entry_cents": entry,
            "opened_utc": "2026-07-01T00:00:00Z",
            "settled_at_utc": settled,
        })
    return rows


def _write_book(state_dir: Path, rows, futility_decided=True):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    fut = {"checkpoints": [{"n_events": 200,
                            "decision": ("STOP-FOR-FUTILITY" if futility_decided else None)}],
           "n_events_current": 103}
    (state_dir / "futility-checkpoint-state.json").write_text(json.dumps(fut))


def _fire_dataset():
    """HIGH/T uniformly bleeding (−20¢/ct, 45 events) against a healthy HIGH/B complement
    (+15¢/ct, 45 events). Every gate condition should hold for HIGH/T."""
    bad = _mk(45, -20, strike="T90", entry=30, lead_h=30)
    good = _mk(45, +15, strike="B80.5", entry=30, lead_h=30, day_base=10)
    return bad + good


# ── gate logic ───────────────────────────────────────────────────────

def test_engineered_pocket_fires_when_futility_decided(tmp_path):
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    d = pl.evaluate(tmp_path, B=_B)
    ht = next(p for p in d["pockets"] if p["name"] == "HIGH/T")
    assert ht["gate"] == {k: True for k in ht["gate"]}, ht["gate"]
    assert ht["verdict"] == "FIRE-POCKET-HIGH/T"
    assert d["any_fire"] is True
    # the healthy complement must NOT fire
    hb = next(p for p in d["pockets"] if p["name"] == "HIGH/B")
    assert hb["verdict"] == "HOLD"


def test_futility_undecided_clamps_to_hold(tmp_path):
    _write_book(tmp_path, _fire_dataset(), futility_decided=False)
    d = pl.evaluate(tmp_path, B=_B)
    ht = next(p for p in d["pockets"] if p["name"] == "HIGH/T")
    assert all(ht["gate"].values()), "statistical gate should pass; only futility blocks"
    assert ht["verdict"] == "HOLD (blocked-pre-futility)"
    assert d["any_fire"] is False


def test_futility_state_missing_is_fail_closed(tmp_path):
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    (tmp_path / "futility-checkpoint-state.json").unlink()
    d = pl.evaluate(tmp_path, B=_B)
    assert d["futility"]["decided"] is False
    assert d["any_fire"] is False


def test_under_40_events_blocks(tmp_path):
    rows = _mk(20, -20, strike="T90") + _mk(45, +15, strike="B80.5", day_base=10)
    _write_book(tmp_path, rows)
    d = pl.evaluate(tmp_path, B=_B)
    ht = next(p for p in d["pockets"] if p["name"] == "HIGH/T")
    assert ht["events"] == 20 and not ht["gate"]["i_ge40_events"]
    assert ht["verdict"] == "HOLD"


def test_wy_blocks_marginal_pocket():
    """A pocket that WOULD look significant on its own must not survive the 13-family WY
    correction — that is the multiplicity screen the naive per-contract slices lack. Tuned to a
    comfortable margin (adj_p≈0.11, stable across bootstrap seeds) so it is not knife-edge; the
    strong-signal *reject* direction is covered by test_engineered_pocket_fires."""
    import random
    rng = random.Random(1)
    items = {}
    # 12 null pockets + 1 marginal (−2.5 mean, sd 10, n=40 → |t|≈2.8: clearly rejects a NAIVE
    # single-hypothesis test, but the max-t null across 13 pockets pushes adj_p well above alpha).
    for k in range(12):
        items[f"null{k}"] = [(f"E{k}-{i}", rng.gauss(0, 10)) for i in range(40)]
    items["marginal"] = [(f"M-{i}", rng.gauss(-2.5, 10)) for i in range(40)]
    out = pl.westfall_young(items, alpha=0.05, B=4000, seed=7)
    assert out["marginal"]["t"] > 1.96, "marginal pocket must look significant alone, else vacuous"
    assert out["marginal"]["adj_p"] > 0.08, out["marginal"]        # comfortably non-reject, not knife-edge
    assert out["marginal"]["reject"] is False, out["marginal"]


def test_regime_cutoff_excludes_precutoff_events(tmp_path):
    # Bleeding HIGH/T events sit on JUL01-05, the healthy HIGH/B complement on JUL10-14 → a
    # 26JUL06 cutoff empties HIGH/T (its bleed left the estimand) but keeps the complement.
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    d = pl.evaluate(tmp_path, fix_cutoff="26JUL06", B=_B)
    ht = next(p for p in d["pockets"] if p["name"] == "HIGH/T")
    assert ht["events"] == 0 and ht["verdict"] == "HOLD"
    hb = next(p for p in d["pockets"] if p["name"] == "HIGH/B")
    assert hb["events"] == 45
    assert d["counts"]["n_settled"] == 45 and d["any_fire"] is False


def test_concentration_only_deficit_blocked_by_drop_top3(tmp_path):
    # 41 HIGH/T events: 38 flat-ish (±2¢ jitter, mean 0) + 3 catastrophic events carrying the
    # whole deficit → drop-top-3 must strip the signal and block the fire.
    flat = _mk(38, 0, strike="T90")
    crash = _mk(3, -80, strike="T90", qty=10, day_base=20)
    good = _mk(45, +15, strike="B80.5", day_base=10)
    _write_book(tmp_path, flat + crash + good)
    d = pl.evaluate(tmp_path, B=_B)
    ht = next(p for p in d["pockets"] if p["name"] == "HIGH/T")
    assert not ht["gate"]["v_survives_drop_top3"]
    assert ht["verdict"] == "HOLD"


# ── estimand integrity ───────────────────────────────────────────────

def test_classifiers_pinned_to_fill_edge_breakdown():
    tickers = ["KXHIGHTNOLA-26JUN30-B92.5", "KXHIGHTNOLA-26JUL01-T96", "KXLOWTDEN-26JUN30-T54",
               "KXLOWTSEA-26JUN29-B52.5", "KXHIGHMIA-26JUN30-B93.5"]
    for tk in tickers:
        fe = feb._market_type(tk)
        ours = pl._pk_market_type(tk)
        # feb returns bare kind for suffix-less strikes; ours returns None (not a pocket) — both
        # must agree whenever the ticker actually lands in a B/T pocket.
        if fe in ("HIGH/B", "HIGH/T", "LOW/B", "LOW/T"):
            assert ours == fe, tk
        else:
            assert ours is None, tk
    for px in (1, 15, 16, 25, 26, 40, 41, 60, 61, 99, 100, None):
        assert pl._pk_band_label(px) == feb._band_label(px), px
    for o, s in [("2026-06-30T00:00:00Z", "2026-06-30T06:00:00Z"),
                 ("2026-06-30T00:00:00Z", "2026-06-30T18:00:00Z"),
                 ("2026-06-29T00:00:00Z", "2026-06-30T06:00:00Z"),
                 ("2026-06-28T00:00:00Z", "2026-06-30T00:00:00Z"),
                 # 4/5-digit fractional seconds (~22% of the live log) — must parse, not drop, on
                 # any <3.11 runner, and both classifiers must agree (kept identically robust).
                 ("2026-06-30T00:00:00.8307Z", "2026-06-30T06:00:00.12345Z"),
                 ("", "2026-06-30T00:00:00Z")]:
        assert pl._pk_lead_bucket(o, s) == feb._lead_bucket(o, s), (o, s)
    assert pl._pk_lead_bucket("2026-06-30T00:00:00.8307Z",
                              "2026-06-30T06:00:00.12345Z") == "<12h (same-day)"


def test_family_hash_mismatch_refuses(tmp_path, monkeypatch, capsys):
    _write_book(tmp_path, _mk(5, 0))
    st = pl._load_state(tmp_path)
    st["preregistration"]["family_hash"] = "sha256:deadbeef"
    pl._save_state(tmp_path, st)
    monkeypatch.setattr(sys, "argv",
                        ["pocket_lever_decision.py", "--state-dir", str(tmp_path), "--bootstrap", "50"])
    rc = pl.main()
    assert rc == 3
    assert "FAMILY HASH MISMATCH" in capsys.readouterr().err


def test_pocket_diff_ci_pairs_shared_events():
    # One event with fills in TWO bands: the joint resampler must keep them paired (the union pool
    # has 1 event → degenerate → CI is None), while a two-sided independent resampler would happily
    # produce a CI from a single shared weather outcome.
    rows = [{"ticker": "KXHIGHTNY-26JUL01-B80.5", "side": "yes", "qty": 2, "pnl_cents": -40,
             "entry_cents": 20, "opened_utc": "", "settled_at_utc": ""},
            {"ticker": "KXHIGHTNY-26JUL01-B82.5", "side": "yes", "qty": 2, "pnl_cents": 40,
             "entry_cents": 50, "opened_utc": "", "settled_at_utc": ""}]
    member = lambda r: pl._classify(r)["entry_band"] == "16-25c"   # noqa: E731
    point, lo, hi = pl._pocket_diff_ci(rows, member, B=200)
    assert point is not None and lo is None and hi is None


# ── ops: fingerprint, state, alerts ──────────────────────────────────

def _run_main(tmp_path, monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", ["pocket_lever_decision.py", "--state-dir", str(tmp_path),
                                      "--bootstrap", str(_B), "--write", *extra])
    return pl.main()


def test_fingerprint_short_circuits_second_write(tmp_path, monkeypatch, capsys):
    _write_book(tmp_path, _mk(5, 0))
    assert _run_main(tmp_path, monkeypatch) == 0
    capsys.readouterr()

    def _boom(*a, **k):
        raise AssertionError("bootstrap must not run on unchanged data")
    monkeypatch.setattr(pl, "_cw_cluster_ci", _boom)
    assert _run_main(tmp_path, monkeypatch) == 0
    assert "fingerprint match" in capsys.readouterr().out


def test_new_settlement_busts_fingerprint(tmp_path, monkeypatch, capsys):
    rows = _mk(5, 0)
    _write_book(tmp_path, rows)
    assert _run_main(tmp_path, monkeypatch) == 0
    _write_book(tmp_path, rows + _mk(1, -10, day_base=25))
    capsys.readouterr()
    assert _run_main(tmp_path, monkeypatch) == 0
    assert "fingerprint match" not in capsys.readouterr().out


def test_alerts_fire_once(tmp_path, monkeypatch):
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    calls = []
    monkeypatch.setattr(pl, "_alert", lambda msg, key: calls.append(key))
    assert _run_main(tmp_path, monkeypatch) == 0
    fire_keys = [k for k in calls if k.startswith("pocket_lever_fire_")]
    decidable_keys = [k for k in calls if k.startswith("pocket_lever_decidable_")]
    assert fire_keys == ["pocket_lever_fire_HIGH_T"]
    assert "pocket_lever_decidable_HIGH_T" in decidable_keys
    # second write on unchanged data: fingerprint short-circuits, and even with a busted
    # fingerprint the transition flags must prevent a re-alert
    calls.clear()
    assert _run_main(tmp_path, monkeypatch) == 0
    assert calls == []
    st = json.loads((tmp_path / "pocket-lever-state.json").read_text())
    st["last_fingerprint"] = "busted"
    pl._save_state(tmp_path, st)
    calls.clear()
    assert _run_main(tmp_path, monkeypatch) == 0
    assert calls == []


def test_no_alert_when_blocked_pre_futility(tmp_path, monkeypatch):
    _write_book(tmp_path, _fire_dataset(), futility_decided=False)
    calls = []
    monkeypatch.setattr(pl, "_alert", lambda msg, key: calls.append(key))
    assert _run_main(tmp_path, monkeypatch) == 0
    assert calls == [], "no decidable/fire alert may dispatch while futility is undecided"


def test_regime_change_resets_alert_flags(tmp_path, monkeypatch):
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    calls = []
    monkeypatch.setattr(pl, "_alert", lambda msg, key: calls.append(key))
    assert _run_main(tmp_path, monkeypatch) == 0
    assert "pocket_lever_fire_HIGH_T" in calls
    # cutoff moves → clocks and alert flags reset; the (now-empty) pockets hold, nothing re-fires
    calls.clear()
    assert _run_main(tmp_path, monkeypatch, "--fix-cutoff", "26AUG01") == 0
    st = json.loads((tmp_path / "pocket-lever-state.json").read_text())
    assert st["regime"]["cutoff"] == "26AUG01"
    assert st["regime"]["history"][-1]["old"] is None
    assert st["alerts"]["fired"] == {} and calls == []


def test_plain_write_inherits_persisted_cutoff(tmp_path, monkeypatch):
    """A standing cron run (--write, NO --fix-cutoff) must INHERIT the operator's persisted regime
    cutoff, never silently clear it and re-evaluate on the spliced pre-change data. Regression for
    the 2026-07-09 review finding."""
    _write_book(tmp_path, _fire_dataset(), futility_decided=True)
    # operator sets a regime cutoff after a (hypothetical) live selection change
    assert _run_main(tmp_path, monkeypatch, "--fix-cutoff", "26AUG01") == 0
    st = json.loads((tmp_path / "pocket-lever-state.json").read_text())
    assert st["regime"]["cutoff"] == "26AUG01"
    assert len(st["regime"]["history"]) == 1
    # bust the fingerprint so the plain run takes the full write path (not the cache short-circuit)
    st["last_fingerprint"] = "busted"
    pl._save_state(tmp_path, st)
    # a PLAIN --write, exactly like the cron
    assert _run_main(tmp_path, monkeypatch) == 0
    st2 = json.loads((tmp_path / "pocket-lever-state.json").read_text())
    assert st2["regime"]["cutoff"] == "26AUG01", "plain cron run must NOT clear the operator's cutoff"
    assert len(st2["regime"]["history"]) == 1, "no phantom regime change may be recorded"
    # and the evaluation actually used the inherited cutoff (all fire-dataset events pre-date AUG01)
    assert st2["last_reading"]["counts"]["n_settled"] == 0


def test_as_of_run_does_not_write_state(tmp_path, monkeypatch):
    _write_book(tmp_path, _mk(5, 0))
    monkeypatch.setattr(sys, "argv", ["pocket_lever_decision.py", "--state-dir", str(tmp_path),
                                      "--bootstrap", str(_B), "--write",
                                      "--settled-through", "2026-07-01"])
    assert pl.main() == 0
    assert not (tmp_path / "pocket-lever-state.json").exists()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

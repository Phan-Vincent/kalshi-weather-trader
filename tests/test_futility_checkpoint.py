#!/usr/bin/env python3
"""Pre-registered futility checkpoint tests (quant review 2026-07-01, experiment #1).

Pins the properties that make it a real pre-registration: at a reached checkpoint the verdict is
GO iff the event-clustered CI is clear of 0 else STOP-FOR-FUTILITY (never "continue"); the verdict
is IMMUTABLE once written (later data can't overwrite it); the estimand + checkpoints are frozen;
below-threshold is just "awaiting"; non-weather rows are excluded. No network.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import check_futility_checkpoint as fc   # noqa: E402


def _write_settlements(state_dir: Path, rows: list[dict]):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _rows(n_events: int, pnl_per_ct: int, per_event: int = 1, city="NY"):
    """n_events distinct weather events, each `per_event` bins, all at pnl_per_ct ¢/contract."""
    out = []
    for e in range(n_events):
        for b in range(per_event):
            out.append({"ticker": f"KXHIGHT{city}-26JUL{e:02d}-B{80 + b}.5",
                        "side": "yes", "qty": 2, "pnl_cents": pnl_per_ct * 2,
                        "entry_cents": 40, "settled_at_utc": "2026-07-01T00:00:00Z"})
    return out


def _point_to(fc_mod, monkeypatch, tmp_path):
    """Redirect the tool's state dir + paths to a tmp dir."""
    sd = tmp_path / "live-premium"
    monkeypatch.setattr(fc_mod, "STATE_DIR", sd)
    monkeypatch.setattr(fc_mod, "STATE_PATH", sd / "futility-checkpoint-state.json")
    monkeypatch.setenv("KALSHI_WEATHER_FUTILITY_CHECKPOINTS", "10,20")
    return sd


def test_below_threshold_awaits_no_decision(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    _write_settlements(sd, _rows(5, pnl_per_ct=8))       # 5 events < 10
    assert fc.main() == 0
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    assert st["n_events_current"] == 5
    assert all(cp["decision"] is None for cp in st["checkpoints"])


def test_reached_ci_clear_positive_is_GO(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    _write_settlements(sd, _rows(12, pnl_per_ct=8))      # 12 >= 10; every event +8¢ → CI clear >0
    assert fc.main() == 0
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    cp10 = next(c for c in st["checkpoints"] if c["n_events"] == 10)
    assert cp10["decision"] == "GO", cp10
    assert cp10["edge_ci_cents"][0] > 0


def test_reached_ci_spans_zero_is_STOP(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    # 12 events alternating +40/−40 ¢/ct → mean ~0, CI spans 0 → STOP-FOR-FUTILITY
    rows = []
    for e in range(12):
        rows.append({"ticker": f"KXHIGHTNY-26JUL{e:02d}-B80.5", "side": "yes", "qty": 2,
                     "pnl_cents": (80 if e % 2 else -80), "entry_cents": 40,
                     "settled_at_utc": "2026-07-01T00:00:00Z"})
    _write_settlements(sd, rows)
    assert fc.main() == 0
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    cp10 = next(c for c in st["checkpoints"] if c["n_events"] == 10)
    assert cp10["decision"] == "STOP-FOR-FUTILITY", cp10


def test_verdict_is_immutable_across_reruns(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    _write_settlements(sd, _rows(12, pnl_per_ct=8))      # first run → GO at cp10
    fc.main()
    first = json.loads((sd / "futility-checkpoint-state.json").read_text())
    cp10_first = next(c for c in first["checkpoints"] if c["n_events"] == 10)
    assert cp10_first["decision"] == "GO"
    # now the data turns sharply negative — the LOCKED cp10 verdict must NOT change
    _write_settlements(sd, _rows(12, pnl_per_ct=-50))
    fc.main()
    second = json.loads((sd / "futility-checkpoint-state.json").read_text())
    cp10_second = next(c for c in second["checkpoints"] if c["n_events"] == 10)
    assert cp10_second["decision"] == "GO", "a locked checkpoint verdict must be immutable"
    assert cp10_second["verdict_utc"] == cp10_first["verdict_utc"]
    assert second["created_utc"] == first["created_utc"]     # estimand/checkpoints frozen


def test_anytime_verdict_fires_and_is_immutable(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    # 40 varied but clearly-positive events → the anytime-valid CS excludes 0 → GO-EARLY, immutable
    rows = [{"ticker": f"KXHIGHTNY-26JUL{e:02d}-B80.5", "side": "yes", "qty": 2,
             "pnl_cents": 16 + (e % 5 - 2) * 4, "entry_cents": 40,   # per-ct ≈ +8 ± a bit
             "settled_at_utc": "2026-07-01T00:00:00Z"} for e in range(40)]
    _write_settlements(sd, rows)
    fc.main()
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    assert st["anytime_verdict"] is not None and st["anytime_verdict"]["decision"] == "GO-EARLY", st.get("anytime_verdict")
    locked = st["anytime_verdict"]["verdict_utc"]
    # data turns sharply negative — the LOCKED anytime verdict must not change
    _write_settlements(sd, [{**r, "pnl_cents": -100} for r in rows])
    fc.main()
    st2 = json.loads((sd / "futility-checkpoint-state.json").read_text())
    assert st2["anytime_verdict"]["decision"] == "GO-EARLY"
    assert st2["anytime_verdict"]["verdict_utc"] == locked


def test_anytime_verdict_none_when_noisy(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    # alternating ±40¢/ct → mean ~0 → anytime CS spans 0 → no early verdict
    rows = [{"ticker": f"KXHIGHTNY-26JUL{e:02d}-B80.5", "side": "yes", "qty": 2,
             "pnl_cents": (80 if e % 2 else -80), "entry_cents": 40,
             "settled_at_utc": "2026-07-01T00:00:00Z"} for e in range(30)]
    _write_settlements(sd, rows)
    fc.main()
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    assert st["anytime_verdict"] is None


def test_non_weather_rows_excluded(monkeypatch, tmp_path):
    sd = _point_to(fc, monkeypatch, tmp_path)
    rows = _rows(6, pnl_per_ct=8)
    rows += [{"ticker": "KXUSAIRANAGREEMENT-27-26SEP", "side": "yes", "qty": 173,
              "pnl_cents": 9999, "entry_cents": 68, "settled_at_utc": "2026-07-01T00:00:00Z"}]
    _write_settlements(sd, rows)
    fc.main()
    st = json.loads((sd / "futility-checkpoint-state.json").read_text())
    assert st["n_events_current"] == 6   # Iran row dropped

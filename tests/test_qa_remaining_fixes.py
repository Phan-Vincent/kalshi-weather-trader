#!/usr/bin/env python3
"""Regression tests for the 2026-07-01 remaining QA fixes (QA-06/08/10/12/17).

See reviews/QA-full-audit-2026-07-01.md. Plain pytest; no network, temp dirs only.
(QA-09 → tests/test_flatten_live.py; QA-13 → tests/test_paper_trade_guard.py.)
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import is_weather_ticker, extract_city, append_jsonl_atomic  # noqa: E402


# ── QA-10: anchored weather-ticker matcher (no more substring false-positives) ──
def test_qa10_weather_ticker_matcher():
    for t in ["KXHIGHNY-26JUL01-T90", "KXHIGHTHOU-26MAY28-T91",
              "KXLOWTDEN-26JUN17-B59.5", "KXHIGHMIA-26JUL01-T99"]:
        assert is_weather_ticker(t), t
    for t in ["KXUSAIRANAGREEMENT-27-26SEP", "KXALLTIMEHIGH-25-X", "KXFEDLOWER-25-A",
              "KXHIGHESTGROSSINGMOVIE-25DEC31-A", "test", ""]:
        assert not is_weather_ticker(t), t


# ── QA-12: brier + calibrate city extraction agree (incl. bare KXHIGH<CITY>) ──
def test_qa12_brier_calibrate_city_agree():
    import trader.brier as brier
    import model.calibrate as cal
    for t in ["KXHIGHNY-26JUL01-T90", "KXHIGHMIA-26JUL01-T99",
              "KXLOWTDEN-26JUN17-B59.5", "KXHIGHTHOU-26MAY28-T91"]:
        b, c, s = brier._extract_city(t), cal._extract_city(t), extract_city(t)
        assert b == c == s and b, f"{t}: brier={b} cal={c} shared={s}"


# ── QA-06: isotonic calibrator pools tied predictions (n reflects true count) ──
def test_qa06_calibrator_pools_tied_low_probs(tmp_path):
    from model.calibrate import IsotonicCalibrator
    bp = tmp_path / "brier-log.jsonl"
    with open(bp, "w") as f:
        for i in range(60):   # 60 predictions all at p=0.10 that all resolve YES
            f.write(json.dumps({"record_id": f"r{i}", "status": "settled",
                                "our_prob": 0.10, "outcome": "yes"}) + "\n")
    c = IsotonicCalibrator().fit(brier_path=bp, min_trades=10).calibrate(0.10)
    # Pooled bucket (n=60) → Laplace weight 5/65 → ~0.93. The pre-fix per-trade buckets
    # over-regularized each tie (weight 5/6) to ~0.25.
    assert c > 0.6, f"calibrated {c:.3f} — tied low-probs not pooled (QA-06)"


# ── QA-17: flock-guarded atomic JSONL append (single write, dir auto-create, no-op safe) ──
def test_qa17_append_jsonl_atomic(tmp_path):
    p = tmp_path / "sub" / "settlement-log.jsonl"      # nested dir must be created
    append_jsonl_atomic(p, {"a": 1})
    append_jsonl_atomic(p, [{"b": 2}, {"c": 3}])
    append_jsonl_atomic(p, [])                          # no-op
    append_jsonl_atomic(p, [None])                      # None filtered
    rows = [json.loads(l) for l in open(p) if l.strip()]
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]


# ── QA-08: alert dedup is recorded only AFTER a confirmed delivery ──
def test_qa08_failed_send_does_not_burn_dedup_window(monkeypatch, tmp_path):
    import trader.notify as nt
    monkeypatch.setattr(nt, "_DEDUP_STATE", tmp_path / "dedup.json")
    monkeypatch.setattr(nt, "_ALERTS_LOG", tmp_path / "alerts.jsonl")
    monkeypatch.delenv("KALSHI_WEATHER_ALERTS", raising=False)   # ensure delivery enabled
    state = {"rc": 1, "n": 0}

    def fake_run(cmd, **kw):
        state["n"] += 1
        return types.SimpleNamespace(returncode=state["rc"], stdout="", stderr="boom")
    monkeypatch.setattr(nt.subprocess, "run", fake_run)

    # Two failing sends: BOTH must attempt delivery — a failure must not dedup-suppress the retry.
    assert nt.alert("msg", key="k") is False
    assert nt.alert("msg", key="k") is False
    assert state["n"] == 2, "a failed send must NOT burn the dedup window (QA-08)"

    # Now delivery succeeds → recorded → subsequent identical key is suppressed (no new attempt).
    state["rc"] = 0
    assert nt.alert("msg", key="k") is True
    assert state["n"] == 3
    assert nt.alert("msg", key="k") is False    # deduped
    assert state["n"] == 3, "a successful send should suppress repeats within the window"

#!/usr/bin/env python3
"""Fix-1 (quant review 2026-07-01): the global calibrator/model-brier fit source must default to
the SETTLING live-main book (state/paper/brier-log.jsonl), not the frozen root state/brier-log.jsonl
(no settlements after 6/19 — the source of the stale "BSS -0.121" headline), and must honor the
KALSHI_WEATHER_BRIER_LOG override. Artifact WRITE paths stay at root state/ (scanner reads them there).
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))


def _reload(monkeypatch, env=None):
    for k in ("KALSHI_WEATHER_BRIER_LOG",):
        monkeypatch.delenv(k, raising=False)
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    import model.calibrate as cal
    import model_brier as mb
    return importlib.reload(cal), importlib.reload(mb)


def test_default_fit_source_is_settling_paper_book(monkeypatch):
    cal, mb = _reload(monkeypatch)
    expected = ROOT / "state" / "paper" / "brier-log.jsonl"
    assert cal._BRIER_LOG == expected, f"calibrate fits from {cal._BRIER_LOG}, expected {expected}"
    assert mb._BRIER_LOG == expected, f"model_brier reads {mb._BRIER_LOG}, expected {expected}"
    # regression: must NOT be the frozen root log
    assert cal._BRIER_LOG != ROOT / "state" / "brier-log.jsonl"


def test_env_override_respected(monkeypatch, tmp_path):
    override = tmp_path / "custom-brier.jsonl"
    cal, mb = _reload(monkeypatch, env={"KALSHI_WEATHER_BRIER_LOG": str(override)})
    assert cal._BRIER_LOG == override
    assert mb._BRIER_LOG == override
    # restore default resolution for subsequent tests (conftest restores env; re-import cleans globals)
    _reload(monkeypatch)


def test_artifact_write_paths_stay_at_root(monkeypatch):
    cal, mb = _reload(monkeypatch)
    assert cal._CALIBRATION_PATH == ROOT / "state" / "calibration.json"
    assert mb._MODEL_CALIBRATION == ROOT / "state" / "model-calibration.json"

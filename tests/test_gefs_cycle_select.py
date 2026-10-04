#!/usr/bin/env python3
"""GEFS cycle selection must commit only to a FULLY-POSTED run (2026-06-29).

Regression for the daily false "GEFS ingest returned 0 cities" alert at 04:00 UTC:
`_gefs_run()` used to probe a single early file (gec00/f024), so at 04:00 UTC it
committed to the half-uploaded 00Z cycle (00Z fully lands ~05:00-05:30 UTC). The
download then 404'd most of the 25-file ensemble → 0 cities → spurious alert →
24h health DEGRADED. The fix probes the LAST file the ensemble needs
(gep{N_MEMBERS-1}/f{max(FORECAST_HOURS)}), so an incomplete fresh cycle is skipped
and selection falls back to the complete prior cycle. Runs under plain python3."""
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "data"))

from data.gefs_ingester import _gefs_run, FORECAST_HOURS, N_MEMBERS  # noqa: E402

LAST_MEMBER = f"gep{N_MEMBERS - 1:02d}"
LAST_FHOUR = f"f{max(FORECAST_HOURS):03d}"


def _patched_urlopen(available_substrings, probed=None):
    """Fake urlopen: succeed iff the request URL contains one of `available_substrings`,
    else raise (simulating a 404 for a not-yet-posted cycle). Records probed URLs."""
    def fake(req, *a, **k):
        url = getattr(req, "full_url", req)
        if probed is not None:
            probed.append(url)
        if any(s in url for s in available_substrings):
            return mock.MagicMock()
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return fake


def test_skips_incomplete_fresh_cycle_at_04utc():
    # 04:00 UTC -> rounds to 00Z today; only the PRIOR day's 18Z is fully posted.
    now = datetime(2026, 6, 29, 4, 0, tzinfo=timezone.utc)
    with mock.patch("urllib.request.urlopen",
                    _patched_urlopen(["gefs.20260628/18/"])):
        date, run = _gefs_run(now=now)
    assert (date, run) == ("20260628", "18"), (date, run)


def test_uses_current_block_when_complete():
    # 14:00 UTC -> rounds to 12Z today; 12Z is fully posted -> use it (offset 0).
    now = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)
    with mock.patch("urllib.request.urlopen",
                    _patched_urlopen(["gefs.20260629/12/"])):
        date, run = _gefs_run(now=now)
    assert (date, run) == ("20260629", "12"), (date, run)


def test_probes_the_last_ensemble_file_not_control_f024():
    # The completeness sentinel must be the highest member at the highest hour,
    # never the old gec00/f024 (which is posted too early).
    now = datetime(2026, 6, 29, 4, 0, tzinfo=timezone.utc)
    probed = []
    with mock.patch("urllib.request.urlopen",
                    _patched_urlopen(["gefs.20260628/18/"], probed=probed)):
        _gefs_run(now=now)
    assert probed, "expected at least one probe"
    for url in probed:
        assert LAST_MEMBER in url and LAST_FHOUR in url, url
        assert "gec00" not in url and "f024.idx" not in url, url


if __name__ == "__main__":
    test_skips_incomplete_fresh_cycle_at_04utc()
    test_uses_current_block_when_complete()
    test_probes_the_last_ensemble_file_not_control_f024()
    print("ok")

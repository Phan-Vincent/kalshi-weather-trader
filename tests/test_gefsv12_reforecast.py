#!/usr/bin/env python3
"""Tests for bin/gefsv12_reforecast.py — GEFSv12 reforecast scaffold pure logic (no network).

Covers the parts that MUST be exactly right for correctness: the S3 key template, the .idx parse, and the
one-and-only de-accumulation step (keep disjoint 6h buckets, drop the interleaved 3h partials that would
otherwise double-count), plus the mm→inch month total and the ensemble bin probability.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import gefsv12_reforecast as g

# real Days:1-10 .idx head (2017070100 c00) — note the interleaved 3h/6h buckets
IDX = """1:0:d=2017070100:APCP:surface:0-3 hour acc fcst:ENS=low-res ctl
2:467028:d=2017070100:APCP:surface:0-6 hour acc fcst:ENS=low-res ctl
3:1031461:d=2017070100:APCP:surface:6-9 hour acc fcst:ENS=low-res ctl
4:1470345:d=2017070100:APCP:surface:6-12 hour acc fcst:ENS=low-res ctl
5:1900000:d=2017070100:APCP:surface:12-15 hour acc fcst:ENS=low-res ctl
6:2300000:d=2017070100:APCP:surface:12-18 hour acc fcst:ENS=low-res ctl"""


def test_refo_key():
    assert g.refo_key(date(2017, 7, 1), "c00", "Days:1-10") == \
        "GEFSv12/reforecast/2017/2017070100/c00/Days:1-10/apcp_sfc_2017070100_c00.grib2"


def test_parse_idx_offsets_and_windows():
    recs = g.parse_idx(IDX)
    assert len(recs) == 6
    assert recs[0] == {"n": 1, "offset": 0, "next_offset": 467028, "a": 0, "b": 3}
    assert recs[1]["next_offset"] == 1031461               # next line's offset
    assert recs[4]["next_offset"] == 2300000               # line 5 → line 6's offset
    assert recs[-1]["next_offset"] is None                 # last line → open-ended Range GET
    # a non-APCP or malformed line is skipped
    assert g.parse_idx("1:0:d=2017070100:TMP:surface:0-6 hour acc fcst:x") == []


def test_select_6h_buckets_drops_3h_partials():
    kept = g.select_6h_buckets(g.parse_idx(IDX))
    # keep only 0-6, 6-12, 12-18 (the disjoint 6h buckets); drop 0-3, 6-9, 12-15
    assert [(r["a"], r["b"]) for r in kept] == [(0, 6), (6, 12), (12, 18)]


def test_month_open_total_and_max_lead():
    buckets = [(6, 25.4), (12, 25.4), (390, 100.0)]   # mm, tagged by valid-END hour
    assert g.month_open_total_in(buckets) == 5.937       # (25.4+25.4+100)/25.4
    assert g.month_open_total_in(buckets, max_lead_h=384) == 2.0   # drop the 390h bucket → (25.4+25.4)/25.4


def test_bin_prob():
    assert g.bin_prob(2.0, [1.0, 2.5, 3.0, 0.5]) == 0.5   # {2.5,3.0} exceed
    assert g.bin_prob(2.0, []) is None


def test_member_lists():
    assert g.REFO_DAILY_MEMBERS == ["c00", "p01", "p02", "p03", "p04"]
    assert len(g.REFO_WEEKLY_MEMBERS) == 11 and g.REFO_WEEKLY_MEMBERS[-1] == "p10"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    sys.exit(0 if not failed else 1)

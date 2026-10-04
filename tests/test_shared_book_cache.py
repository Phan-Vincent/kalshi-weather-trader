#!/usr/bin/env python3
"""P0a cross-process CRN book cache (bin/paper_trade.py + bin/run_variants.py).

Pins the SAFETY invariant QA flagged as untested (2026-07-01): the live-order path must NEVER read
the shared book cache. Covers all four behaviors so a future refactor can't silently route the live
arm through a cached/stale book:
  1. _shared_book_path() gate (needs SHARED_BOOK=1 AND CYCLE_ID)
  2. paper arms: a second arm is served the SAME snapshot from the file (no re-fetch) — the CRN win
  3. live safety: SHARED_BOOK=0 ALWAYS fetches fresh (bypasses the cache)
  4. run_variants forces SHARED_BOOK=0 for the live arm even under a hostile parent env; and
     paper_trade.main() force-sets it too (process-level defense — the QA HIGH finding)
  5. a failed fetch ({}) is never cached
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import paper_trade as pt        # noqa: E402
import run_variants as rv       # noqa: E402

FAKE_BOOK = {"yes_bid": 10, "yes_ask": 12, "no_bid": 88, "no_ask": 90,
             "yes_bid_qty": 5, "no_bid_qty": 5, "yes_ask_qty": 5, "no_ask_qty": 5, "spread_cents": 2}


def _reset_pt():
    pt._SHARED_CACHE = None
    pt._SHARED_CACHE_PATH = None
    pt._SHARED_PRUNED = False


def _mock_fetch(counter):
    def f(_ticker):
        counter[0] += 1
        return {"orderbook_fp": [[0.10, 5.0]]}   # truthy, has orderbook_fp
    return f


def test_shared_book_path_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "ROOT", tmp_path)
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "c1")
    monkeypatch.delenv("KALSHI_WEATHER_SHARED_BOOK", raising=False)
    assert pt._shared_book_path() is None                  # flag unset
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "0")
    assert pt._shared_book_path() is None                  # flag off
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "1")
    monkeypatch.delenv("KALSHI_WEATHER_CYCLE_ID", raising=False)
    assert pt._shared_book_path() is None                  # no cycle id
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "c1")
    p = pt._shared_book_path()
    assert p is not None and p.name == "book-cache-c1.json"


def test_paper_second_arm_served_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "ROOT", tmp_path)
    _reset_pt()
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "1")
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "cyc")
    cnt = [0]
    monkeypatch.setattr(pt, "fetch_orderbook", _mock_fetch(cnt))
    monkeypatch.setattr(pt, "derive_book", lambda fp: dict(FAKE_BOOK))
    m1 = pt.fetch_live_market("T")
    pt._SHARED_CACHE = None                                 # simulate a second arm (fresh process)
    m2 = pt.fetch_live_market("T")
    assert cnt[0] == 1, "second paper arm must read the shared cache file, not re-fetch"
    assert m1 == m2 and m2["yes_bid"] == 10


def test_live_bypass_always_fetches(monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "ROOT", tmp_path)
    _reset_pt()
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "0")   # the live-arm setting
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "cyc")
    cnt = [0]
    monkeypatch.setattr(pt, "fetch_orderbook", _mock_fetch(cnt))
    monkeypatch.setattr(pt, "derive_book", lambda fp: dict(FAKE_BOOK))
    pt.fetch_live_market("T")
    pt.fetch_live_market("T")
    assert cnt[0] == 2, "SHARED_BOOK=0 must bypass the cache and fetch fresh EVERY time (live safety)"


def test_failed_fetch_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "ROOT", tmp_path)
    _reset_pt()
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "1")
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "cyc")
    cnt = [0]
    monkeypatch.setattr(pt, "fetch_orderbook", lambda t: (cnt.__setitem__(0, cnt[0] + 1), None)[1])
    m1 = pt.fetch_live_market("T")
    pt._SHARED_CACHE = None
    m2 = pt.fetch_live_market("T")
    assert m1 == {} and m2 == {}
    assert cnt[0] == 2, "a failed fetch ({}) must never be cached (both arms re-fetch)"


def test_run_variants_forces_off_for_live_under_hostile_env(monkeypatch, tmp_path):
    monkeypatch.setattr(rv, "ROOT", tmp_path)
    monkeypatch.setattr(rv, "_resolve_fv", lambda fv: "fair-values.json")
    captured = []
    monkeypatch.setattr(rv, "_run", lambda cmd, env: (captured.append(env.get("KALSHI_WEATHER_SHARED_BOOK")), 0)[1])
    monkeypatch.setenv("KALSHI_WEATHER_SHARED_BOOK", "1")   # hostile parent env
    monkeypatch.setenv("KALSHI_WEATHER_CYCLE_ID", "cyc")

    rv.run_arm({"name": "L", "live": True, "dir": "live-x", "env": {}})
    live_vals = list(captured)
    captured.clear()
    rv.run_arm({"name": "P", "live": False, "dir": "paper-x", "env": {}})
    paper_vals = list(captured)

    assert live_vals and all(v == "0" for v in live_vals), f"live arm must get SHARED_BOOK=0, got {live_vals}"
    assert paper_vals and all(v == "1" for v in paper_vals), f"paper arm must get SHARED_BOOK=1, got {paper_vals}"


def test_paper_trade_main_has_process_level_live_guard():
    # Pin the process-level defense (QA HIGH finding): main() force-sets SHARED_BOOK=0 when --live so
    # ANY live entrypoint bypasses the cache regardless of inherited env — not just the run_variants path.
    src = (ROOT / "bin" / "paper_trade.py").read_text()
    assert 'os.environ["KALSHI_WEATHER_SHARED_BOOK"] = "0"' in src and "if args.live:" in src, \
        "paper_trade.main() must force SHARED_BOOK=0 when --live (P0a process-level safety)"

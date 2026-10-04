#!/usr/bin/env python3
"""At-fill markout telemetry (2026-07-05; QA-hardened same day): bin/sync_live_positions.py snapshots
the current mid the moment a fill is first observed, so the markout log carries a prompt post-fill
point instead of only the 6×/day cmd_report cycles (~1.9h median first snapshot) — the gap that blocks
the spread-capture-vs-adverse-selection split of the YES bleed (reviews/yes-bleed-decomposition-2026-07-04.md).

These rows are a DIAGNOSTIC channel for the offline decomposition. The QA pass found the original design
let a gap-delayed observation land inside the [2,8]h window the live kill/continue instruments key on
(age = now − last_updated, NOT ~0). The hardened contract, pinned here:
  • WRITER only snapshots PROMPT observations (age < _AT_FILL_MAX_AGE_H) — so a gap's stale-mid fills are
    skipped BEFORE any orderbook fetch (bounding both perturbation and network fan-out); fetches are
    capped per cycle; the append is fail-soft and the return reflects what actually persisted.
  • CONSUMERS every repo markout reader hard-excludes source="sync_fill_detect", so these rows can NEVER
    reach a live instrument regardless of age (defense in depth).

Plain python3 (framework, per repo notes) — runnable standalone or under pytest; no network, tmp dirs.
Timestamps are derived from now() (no date literals) so the suite is not a date-dependent time bomb.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import sync_live_positions as slp   # noqa: E402
from sync_live_positions import _AT_FILL_MAX_AGE_H, _AT_FILL_MAX_FETCHES, _AT_FILL_MAX_SECONDS   # noqa: E402
assert _AT_FILL_MAX_SECONDS > 0   # sanity: the wall-clock budget is a positive bound

CANONICAL_MARKOUT_KEYS = {
    "ts", "ticker", "side", "qty", "fill_px", "opened_utc", "cur_mid", "age_hours", "paper_order_id",
}
WEATHER = "KXHIGHTNY-26JUL05-B80.5"


def _now_minus(hours=0.0, minutes=0.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)).isoformat()


def _pos(ticker=WEATHER, side="yes", qty=4, entry=33, opened=None, oid=None):
    """An OPEN-BOOK position (the shape _snapshot_new_fills_markout receives)."""
    return {"ticker": ticker, "side": side, "qty": qty, "avg_entry_cents": entry,
            "opened_utc": opened if opened is not None else _now_minus(minutes=10), "paper_order_id": oid}


def _rows(state_dir):
    p = Path(state_dir) / "markout-log.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


# ── writer: happy path + schema ─────────────────────────────────────────────────────────────────
def test_prompt_new_fill_writes_at_fill_row():
    d = tempfile.mkdtemp()
    written = slp._snapshot_new_fills_markout(
        d, [_pos(opened=_now_minus(minutes=12), entry=33)], {}, True, mid_fn=lambda tk, side: 36.0)
    on_disk = _rows(d)
    assert len(written) == 1 and len(on_disk) == 1, (written, on_disk)
    row = on_disk[0]
    assert row["ticker"] == WEATHER and row["side"] == "yes"
    assert row["cur_mid"] == 36.0 and row["fill_px"] == 33
    assert row["source"] == "sync_fill_detect"
    assert CANONICAL_MARKOUT_KEYS.issubset(row.keys()), row.keys()
    assert 0 <= row["age_hours"] < _AT_FILL_MAX_AGE_H, "prompt observation must be a small, in-bounds age"


def test_no_side_mid_is_complemented():
    d = tempfile.mkdtemp()
    slp._snapshot_new_fills_markout(
        d, [_pos("KXLOWTLAX-26JUL05-T63", "no", entry=21)], {}, True,
        mid_fn=lambda tk, side: (100.0 - 40.0) if side == "no" else 40.0)
    assert _rows(d)[0]["cur_mid"] == 60.0, "NO mid must be 100 − YES mid"


def test_previously_open_ticker_is_not_resnapshotted():
    d = tempfile.mkdtemp()
    written = slp._snapshot_new_fills_markout(
        d, [_pos("KXLOWTLAX-26JUL05-T63", "no"), _pos(WEATHER, "yes")],
        {"KXLOWTLAX-26JUL05-T63": {}}, True, mid_fn=lambda tk, side: 50.0)
    assert [r["ticker"] for r in written] == [WEATHER], "only the newly-observed fill snapshots"


def test_cold_start_writes_nothing():
    d = tempfile.mkdtemp()
    assert slp._snapshot_new_fills_markout(d, [_pos()], {}, False, mid_fn=lambda tk, side: 50.0) == []
    assert _rows(d) == []


def test_unusable_mid_is_skipped():
    d = tempfile.mkdtemp()
    assert slp._snapshot_new_fills_markout(d, [_pos()], {}, True, mid_fn=lambda tk, side: None) == []
    assert _rows(d) == []


# ── writer: the QA-found bug — gap-delayed / stale-age fills must NOT be snapshotted ──────────────
def test_gap_delayed_fill_is_not_snapshotted_and_makes_no_fetch():
    # A fill first observed hours after it filled (sync gap: laptop asleep, missed launchd slots) has a
    # STALE mid and an age inside the [2,8]h consumer window. It must be skipped — and skipped BEFORE any
    # orderbook fetch, so a gap's burst of new fills can't fan out network calls under the .cycle.lock.
    d = tempfile.mkdtemp()
    calls = []
    written = slp._snapshot_new_fills_markout(
        d, [_pos(opened=_now_minus(hours=3.0))], {}, True,
        mid_fn=lambda tk, side: (calls.append(tk), 50.0)[1])
    assert written == [] and _rows(d) == [], "stale-age (gap-delayed) fill must not be snapshotted"
    assert calls == [], "no orderbook fetch may happen for a gap-delayed fill (fan-out bound)"


def test_unparseable_opened_utc_is_skipped():
    d = tempfile.mkdtemp()
    calls = []
    written = slp._snapshot_new_fills_markout(
        d, [_pos(opened="not-a-timestamp")], {}, True,
        mid_fn=lambda tk, side: (calls.append(tk), 50.0)[1])
    assert written == [] and calls == [], "bad opened_utc → no age → skip, no fetch"


def test_fetch_count_is_capped_per_cycle():
    # Even a large burst of PROMPT new fills cannot make unbounded fetches under the lock.
    d = tempfile.mkdtemp()
    calls = []
    fills = [_pos(f"KXHIGHTNY-26JUL05-B{i}", opened=_now_minus(minutes=5)) for i in range(_AT_FILL_MAX_FETCHES + 6)]
    written = slp._snapshot_new_fills_markout(
        d, fills, {}, True, mid_fn=lambda tk, side: (calls.append(tk), 50.0)[1])
    assert len(calls) == _AT_FILL_MAX_FETCHES, f"fetches must cap at {_AT_FILL_MAX_FETCHES}, got {len(calls)}"
    assert len(written) == _AT_FILL_MAX_FETCHES


def test_wall_clock_budget_bounds_fetches():
    # A pathological slow endpoint can't stretch the .cycle.lock: the total wall-clock budget breaks the
    # loop even before the count cap. Force the budget to 0 (module attr read at call time) so the very
    # first budget check trips — deterministic, no sleeping.
    d = tempfile.mkdtemp()
    calls = []
    saved = slp._AT_FILL_MAX_SECONDS
    try:
        slp._AT_FILL_MAX_SECONDS = 0.0
        fills = [_pos(f"KXHIGHTNY-26JUL05-B{i}", opened=_now_minus(minutes=5)) for i in range(5)]
        written = slp._snapshot_new_fills_markout(
            d, fills, {}, True, mid_fn=lambda tk, side: (calls.append(tk), 50.0)[1])
        assert calls == [] and written == [], "a zero time budget must stop fetching immediately"
    finally:
        slp._AT_FILL_MAX_SECONDS = saved


# ── writer: fail-soft ────────────────────────────────────────────────────────────────────────────
def test_mid_fetch_that_raises_is_fail_soft():
    d = tempfile.mkdtemp()

    def _boom(tk, side):
        raise RuntimeError("orderbook exploded")

    assert slp._snapshot_new_fills_markout(d, [_pos()], {"x": 1}, True, mid_fn=_boom) == []
    assert _rows(d) == []


def test_append_failure_is_fail_soft_and_return_is_honest():
    # An append error (disk full, permission) must never crash the sync, and the return value (used for
    # logging/tests) must not falsely report rows as persisted.
    import data.weather_data as wd
    saved = wd.append_jsonl_atomic
    d = tempfile.mkdtemp()
    try:
        def _raise(path, records):
            raise OSError("disk full")
        wd.append_jsonl_atomic = _raise
        written = slp._snapshot_new_fills_markout(d, [_pos(opened=_now_minus(minutes=5))], {}, True,
                                                  mid_fn=lambda tk, side: 40.0)
        assert written == [], "return must reflect that nothing persisted on append failure"
    finally:
        wd.append_jsonl_atomic = saved


# ── consumer guarantee: source-filtered even when age lands IN the [2,8]h window ─────────────────
def test_source_filtered_by_live_instruments_even_in_window():
    from live_fill_quality import _mean_markout
    import markout_kill_test as kt
    A, B = "KXHIGHTNY-26JUL05-B80.5", "KXHIGHTLAX-26JUL05-B90.5"
    cmd = {"ticker": A, "side": "yes", "fill_px": 34, "cur_mid": 30, "age_hours": 5.0,
           "paper_order_id": None, "opened_utc": "o-A"}                       # legit cmd_report mark
    atfill = {"ticker": B, "side": "yes", "fill_px": 40, "cur_mid": 20, "age_hours": 5.0,   # IN window!
              "paper_order_id": None, "opened_utc": "o-B", "source": "sync_fill_detect"}
    # _mean_markout (used by live_fill_quality, live_paper_gap, pnl_replay, and kill-test's cross-check)
    mean, n = _mean_markout([cmd, atfill], our_tickers={A, B})
    assert n == 1 and mean == 30 - 34, "only the cmd_report mark counts; the at-fill row is excluded"
    # markout_kill_test's own aggregator (the KILL verdict input)
    items = kt.markout_items_by_event([cmd, atfill], {A, B})
    assert len(items) == 1, "kill-test must drop the at-fill row even when its age is in [2,8]"


def test_at_fill_row_excluded_from_mean_markout_in_attribution_mode():
    # Production attributes to OUR posted tickers; the exclusion must hold when the at-fill ticker IS in
    # the attribution set (the case that matters), not only under our_tickers=None.
    from live_fill_quality import _mean_markout
    d = tempfile.mkdtemp()
    slp._snapshot_new_fills_markout(d, [_pos(opened=_now_minus(minutes=8))], {}, True, mid_fn=lambda tk, side: 36.0)
    rows = _rows(d)
    assert rows and rows[0]["source"] == "sync_fill_detect"
    mean_none, n_none = _mean_markout(rows, our_tickers=None)
    mean_attr, n_attr = _mean_markout(rows, our_tickers={WEATHER})
    assert n_none == 0 and mean_none is None
    assert n_attr == 0 and mean_attr is None, "at-fill row dropped even when fully attributed"


# ── _fetch_mid_cents unit (no network) ───────────────────────────────────────────────────────────
def test_fetch_mid_cents_side_normalization_and_failsoft():
    import data.kalshi_data as kd
    saved_fo, saved_db = kd.fetch_orderbook, kd.derive_book
    try:
        kd.fetch_orderbook = lambda tk, **kw: {"orderbook_fp": {"_": tk}}
        kd.derive_book = lambda fp: {"yes_bid": 30, "yes_ask": 34}
        assert slp._fetch_mid_cents(WEATHER, "yes") == 32.0
        assert slp._fetch_mid_cents(WEATHER, "no") == 68.0
        kd.derive_book = lambda fp: {"yes_bid": 0, "yes_ask": 0}
        assert slp._fetch_mid_cents(WEATHER, "yes") is None            # one-sided → no mid
        def _raise(tk, **kw):
            raise RuntimeError("net down")
        kd.fetch_orderbook = _raise
        assert slp._fetch_mid_cents(WEATHER, "yes") is None            # never propagates
    finally:
        kd.fetch_orderbook, kd.derive_book = saved_fo, saved_db


# ── integration: placement (after book write) + fail-soft through sync_live_book ─────────────────
def _kpos(ticker, side="yes", qty=5, cost=1650, opened=None):
    return {"ticker": ticker, "side": side, "qty": qty, "position_fp": qty if side == "yes" else -qty,
            "total_cost_cents": cost, "realized_pnl_cents": 0, "market_exposure_cents": 2500,
            "fees_paid_cents": 0, "resting_orders": 0,
            "last_updated": opened if opened is not None else _now_minus(minutes=6)}


def _run_sync(positions, patch_fetch):
    """Drive sync_live_book end-to-end against a temp state dir with a prior (empty-open) book, so a new
    fill is 'newly observed' with had_previous_book=True. Returns (book_dict, markout_rows, state_dir)."""
    import os
    import data.kalshi_data as kd
    from sync_live_positions import sync_live_book
    orig_settle = slp.get_kalshi_settlements
    slp.get_kalshi_settlements = lambda *a, **k: {}
    saved_fo, saved_db = kd.fetch_orderbook, kd.derive_book
    saved_env = os.environ.get("KALSHI_WEATHER_STATE_DIR")
    d = tempfile.mkdtemp()
    try:
        os.environ["KALSHI_WEATHER_STATE_DIR"] = d
        (Path(d) / "paper-book.json").write_text(json.dumps(
            {"version": 2, "open": [], "closed": [], "pending_settlement": [], "cash_cents": 60000}))
        kd.fetch_orderbook, kd.derive_book = patch_fetch(d)
        state = {"available_cents": 45000, "portfolio_cents": 15000, "total_cents": 60000,
                 "balance_ok": True, "positions": positions, "num_positions": len(positions),
                 "total_deployed_cents": 0, "timestamp_utc": datetime.now(timezone.utc).isoformat()}
        sync_live_book(state)
        book = json.loads((Path(d) / "paper-book.json").read_text())
        return book, _rows(d), d
    finally:
        slp.get_kalshi_settlements = orig_settle
        kd.fetch_orderbook, kd.derive_book = saved_fo, saved_db
        if saved_env is None:
            import os as _os; _os.environ.pop("KALSHI_WEATHER_STATE_DIR", None)
        else:
            os.environ["KALSHI_WEATHER_STATE_DIR"] = saved_env


def test_end_to_end_snapshot_fires_after_book_committed():
    seen = {}

    def _patch(d):
        def _fo(tk, **kw):
            # Prove the book was already written before the telemetry fetch runs.
            bk = json.loads((Path(d) / "paper-book.json").read_text())
            seen["book_had_pos_at_fetch"] = any(p["ticker"] == WEATHER for p in bk.get("open", []))
            return {"orderbook_fp": {"_": tk}}
        return _fo, (lambda fp: {"yes_bid": 30, "yes_ask": 34})

    book, mk, _ = _run_sync([_kpos(WEATHER)], _patch)
    assert any(p["ticker"] == WEATHER for p in book["open"]), "book synced the new fill"
    assert seen.get("book_had_pos_at_fetch") is True, "snapshot must fetch AFTER the book is committed"
    assert len(mk) == 1 and mk[0]["source"] == "sync_fill_detect" and mk[0]["cur_mid"] == 32.0


def test_end_to_end_book_still_written_when_snapshot_fetch_raises():
    def _patch(d):
        def _fo(tk, **kw):
            raise RuntimeError("kalshi orderbook down")
        return _fo, (lambda fp: {})

    book, mk, sd = _run_sync([_kpos(WEATHER)], _patch)
    assert any(p["ticker"] == WEATHER for p in book["open"]), "book must be written despite fetch failure"
    assert (Path(sd) / "sync-state.json").exists(), "sync-state must be written despite fetch failure"
    assert mk == [], "no markout row when the mid can't be fetched (fail-soft)"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f(); print(f"[PASS] {n}")
    print(f"\nAll {len(fns)} at-fill markout snapshot tests pass.")

#!/usr/bin/env python3
"""live_fill_quality watchdog: < MIN_N fills → ACCUMULATING; ADVERSE gates on NEGATIVE REALIZED edge
(the money signal for a hold-to-settlement maker); adverse markout ALONE (with non-negative realized)
is WATCH only — on same-day weather markets markout measures settlement drift, not adverse selection
(validation 2026-06-28). Both realized<0 AND markout<=-2 → ADVERSE. Runs under plain python3."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from live_fill_quality import evaluate, MIN_N, _mean_markout  # noqa: E402


def _mk(ticker, fill_px, cur_mid, age, oid="o1", side="yes"):
    return {"ticker": ticker, "side": side, "fill_px": fill_px, "cur_mid": cur_mid,
            "age_hours": age, "paper_order_id": oid}


def test_markout_uses_short_horizon_not_settlement():
    # same position observed at 4h (drift -3) and at 22h near settlement (drift -50):
    # must use the 4h obs, NOT the settlement-age one.
    mk = [_mk("KXT", 50, 47, 4), _mk("KXT", 50, 0, 22)]
    mean, n = _mean_markout(mk, our_tickers={"KXT"})
    assert n == 1 and mean == -3, (mean, n)


def test_markout_excludes_non_our_tickers():
    mk = [_mk("KXUSAIRANAGREEMENT-27-26SEP", 68, 40, 4, side="no")]
    mean, n = _mean_markout(mk, our_tickers={"KXT"})
    assert mean is None and n == 0


def test_markout_drops_impossible_fill_px():
    mean, n = _mean_markout([_mk("KXT", 101, 49, 4)], our_tickers={"KXT"})
    assert mean is None and n == 0


def test_markout_window_excludes_too_young_and_too_old():
    mk = [_mk("KXA", 50, 48, 0.5), _mk("KXB", 50, 48, 30, oid="o2")]  # too young / too old
    mean, n = _mean_markout(mk, our_tickers={"KXA", "KXB"})
    assert mean is None and n == 0


def _make_state(d: Path, n_fills, markout_drift=None, settle_pnl=None):
    lc = [{"event": "posted_live", "ticker": f"T{i}", "side": "yes", "limit_price_cents": 50}
          for i in range(n_fills)]
    (d / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(r) for r in lc) + "\n")
    book = {"open": [{"ticker": f"T{i}", "side": "yes", "avg_entry_cents": 50} for i in range(n_fills)],
            "closed": []}
    (d / "paper-book.json").write_text(json.dumps(book))
    if markout_drift is not None:
        mk = [{"ticker": f"T{i}", "side": "yes", "fill_px": 50, "cur_mid": 50 + markout_drift,
               "age_hours": 6, "paper_order_id": f"o{i}"} for i in range(n_fills)]
        (d / "markout-log.jsonl").write_text("\n".join(json.dumps(r) for r in mk) + "\n")
    if settle_pnl is not None:
        st = [{"ticker": f"T{i}", "side": "yes", "entry_cents": 50, "qty": 1, "pnl_cents": settle_pnl,
               "settled_at_utc": "2026-06-24T00:00:00Z"} for i in range(n_fills)]
        (d / "settlement-log.jsonl").write_text("\n".join(json.dumps(r) for r in st) + "\n")


def test_below_min_n_is_accumulating():
    with tempfile.TemporaryDirectory() as d:
        # even with terrible markout, < MIN_N fills must NOT judge (no noise-triggered alert)
        _make_state(Path(d), n_fills=5, markout_drift=-5, settle_pnl=-20)
        r = evaluate(Path(d))
        assert r["status"] == "ACCUMULATING", r
        assert r["filled"] == 5


def test_adverse_markout_alone_is_watch_not_adverse():
    # adverse markout (-3) but POSITIVE realized edge → WATCH, NOT ADVERSE. Markout on same-day weather
    # markets is settlement drift, not adverse selection, so it must not escalate while the money is good.
    with tempfile.TemporaryDirectory() as d:
        _make_state(Path(d), n_fills=MIN_N + 3, markout_drift=-3, settle_pnl=+12)
        r = evaluate(Path(d))
        assert r["status"] == "WATCH", r
        assert any("markout" in f for f in r["flags"]), r["flags"]


def test_both_negative_realized_and_markout_is_adverse():
    # only when realized EV is ALSO negative does adverse markout escalate to ADVERSE.
    with tempfile.TemporaryDirectory() as d:
        _make_state(Path(d), n_fills=MIN_N + 3, markout_drift=-3, settle_pnl=-12)
        r = evaluate(Path(d))
        assert r["status"] == "ADVERSE", r
        assert any("edge" in f for f in r["flags"]), r["flags"]


def test_negative_realized_edge_flags():
    with tempfile.TemporaryDirectory() as d:
        _make_state(Path(d), n_fills=MIN_N + 3, markout_drift=+1, settle_pnl=-12)  # losing fills
        r = evaluate(Path(d))
        assert r["status"] == "ADVERSE", r
        assert any("edge" in f for f in r["flags"]), r["flags"]


def test_healthy_fills_are_ok():
    with tempfile.TemporaryDirectory() as d:
        _make_state(Path(d), n_fills=MIN_N + 3, markout_drift=+1, settle_pnl=+12)
        r = evaluate(Path(d))
        assert r["status"] == "OK", r
        assert not r["flags"], r["flags"]


if __name__ == "__main__":
    test_below_min_n_is_accumulating();   print("[PASS] < MIN_N fills → ACCUMULATING (no judgment)")
    test_adverse_markout_alone_is_watch_not_adverse(); print("[PASS] adverse markout + positive realized → WATCH (not ADVERSE)")
    test_both_negative_realized_and_markout_is_adverse(); print("[PASS] negative realized + adverse markout → ADVERSE")
    test_negative_realized_edge_flags();  print("[PASS] negative realized edge → ADVERSE")
    test_healthy_fills_are_ok();          print("[PASS] healthy fills → OK")
    test_markout_uses_short_horizon_not_settlement(); print("[PASS] markout uses short-horizon, not settlement age")
    test_markout_excludes_non_our_tickers();          print("[PASS] markout excludes non-our-quote tickers")
    test_markout_drops_impossible_fill_px();          print("[PASS] markout drops impossible fill_px")
    test_markout_window_excludes_too_young_and_too_old(); print("[PASS] markout window excludes too-young/too-old obs")
    print("\nAll live-fill-quality tests pass.")

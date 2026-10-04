#!/usr/bin/env python3
"""bin/fill_edge_breakdown.py — the sliced realized-edge + live fill-rate instrument.

Pins: (1) the SIDE split is computed correctly and reconciles to the total realized P&L (the
2026-06-25 YES-lose / NO-win finding, now a standing metric); (2) ticker parsing (city / market
type / lead time); (3) gap-settled / malformed rows are dropped, not crashed on; (4) the live
fill-rate attribution MATCHES reconcile_fills.live_fill_stats exactly (so the two never drift)."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

import fill_edge_breakdown as feb  # noqa: E402
from reconcile_fills import live_fill_stats  # noqa: E402
from trader.orders import fee_per_contract_cents  # noqa: E402


def _settle(ticker, side, qty, entry, pnl, opened="2026-06-29T00:00:00Z",
            settled="2026-06-30T00:00:00Z"):
    return {"ticker": ticker, "side": side, "qty": qty, "entry_cents": entry, "pnl_cents": pnl,
            "settlement_result": side if pnl > 0 else "?", "opened_utc": opened, "settled_at_utc": settled}


def test_parsing():
    assert feb._city("KXHIGHTNOLA-26JUN30-B92.5") == "NOLA"
    assert feb._city("KXHIGHMIA-26JUN30-B93.5") == "MIA"
    assert feb._city("KXLOWTDEN-26JUN30-T54") == "DEN"
    assert feb._city("KXUSAIRANAGREEMENT-27-26SEP") is None
    assert feb._market_type("KXHIGHTNOLA-26JUN30-B92.5") == "HIGH/B"
    assert feb._market_type("KXLOWTDEN-26JUN30-T54") == "LOW/T"
    assert feb._lead_bucket("2026-06-30T00:00:00Z", "2026-06-30T06:00:00Z") == "<12h (same-day)"
    assert feb._lead_bucket("2026-06-28T00:00:00Z", "2026-06-30T00:00:00Z") == ">=48h"
    assert feb._lead_bucket("", "2026-06-30T00:00:00Z") is None


def test_side_split_and_reconcile():
    # YES bins lose, NO bins win — the 2026-06-25 pattern.
    settle = [
        _settle("KXHIGHMIA-26JUN30-B93.5", "yes", 4, 60, -240),
        _settle("KXHIGHTNOLA-26JUN30-B92.5", "yes", 3, 50, -150),
        _settle("KXLOWTDEN-26JUN30-T54", "no", 5, 25, +375),
        _settle("KXHIGHTHOU-26JUN30-B93.5", "no", 6, 27, +438),
    ]
    by = {n: feb.edge_by(settle, k) for n, k in feb.DIMENSIONS}
    side = {s["key"]: s for s in by["SIDE"]}
    assert side["yes"]["ev_after_fee_ct"] < 0 < side["no"]["ev_after_fee_ct"]
    assert side["yes"]["n"] == 2 and side["no"]["n"] == 2
    # reconcile: side slices repartition the full realized P&L exactly
    total = sum(r["pnl_cents"] for r in settle)
    assert sum(s["pnl_cents"] for s in by["SIDE"]) == total == (-240 - 150 + 375 + 438)


def test_drops_gap_settled_rows():
    settle = [
        _settle("KXHIGHMIA-26JUN30-B93.5", "yes", 4, 60, -240),
        {"ticker": "KXHIGHX-26JUN30-B1.5", "side": None, "qty": 0, "entry_cents": None,
         "pnl_cents": 99, "gap_settled": True},  # malformed/gap — must be dropped, not crash
    ]
    clean = feb._clean_settlements(settle)
    assert len(clean) == 1 and clean[0]["side"] == "yes"


def test_fill_rate_matches_reconcile():
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        lc = [
            {"event": "posted_live", "ticker": "A", "side": "yes", "limit_price_cents": 30},
            {"event": "posted_live", "ticker": "B", "side": "no", "limit_price_cents": 22},
            {"event": "posted_live", "ticker": "C", "side": "yes", "limit_price_cents": 45},
            {"event": "posted_live", "ticker": "A", "side": "yes", "limit_price_cents": 30},  # repost dup
        ]
        (dd / "maker-lifecycle.jsonl").write_text("\n".join(json.dumps(r) for r in lc))
        book = {"open": [{"ticker": "A", "side": "yes", "avg_entry_cents": 31}],
                "closed": [{"ticker": "B", "side": "no", "entry_cents": 22}]}  # C never filled
        (dd / "paper-book.json").write_text(json.dumps(book))

        feb_rate = feb.live_fill_rate(dd)["overall"]
        recon = live_fill_stats(dd)
        assert feb_rate["fill_rate"] == recon["fill_rate"]      # attribution must match exactly
        assert feb_rate["posted"] == recon["posted"] == 3       # deduped distinct (ticker,side)
        assert feb_rate["filled"] == recon["filled"] == 2
        # by-side rate: yes 1/2 filled (A yes, C yes posted; A filled), no 1/1
        bs = feb.live_fill_rate(dd)["by_side"]
        assert bs["yes"]["filled"] == 1 and bs["yes"]["posted"] == 2
        assert bs["no"]["filled"] == 1 and bs["no"]["posted"] == 1


def test_ev_after_fee_is_already_net():
    # FIX 2026-07-06 (audit): settlement pnl_cents is ALREADY net of fees/rebate (live rows
    # are Kalshi net realized P&L; paper credits the rebate into cost). The old code
    # re-subtracted fee_per_contract_cents(maker=True) — a NEGATIVE rebate — re-crediting it
    # and inflating EV. ev_after_fee must now equal the plain net EV (pnl/q), with no
    # further fee adjustment, so ev_ct and ev_after_fee_ct coincide.
    rows = [_settle("KXHIGHTNOLA-26JUN30-B92.5", "no", 1, 50, 10),
            _settle("KXHIGHTHOU-26JUN30-B93.5", "no", 100, 50, 1000)]
    s = feb._slice(feb._clean_settlements(rows))
    q, pnl = 101, 1010
    assert abs(s["ev_after_fee_ct"] - pnl / q) < 1e-9, "ev_after_fee must be the net EV (pnl already net)"
    assert abs(s["ev_after_fee_ct"] - s["ev_ct"]) < 1e-9, "no hidden fee adjustment remains"
    # And it must NOT re-add the rebate (the old buggy value would differ from net here).
    rebate_addback = (pnl - (fee_per_contract_cents(1, 50, maker=True) * 1
                             + fee_per_contract_cents(100, 50, maker=True) * 100)) / q
    if abs(rebate_addback - pnl / q) > 1e-9:
        assert abs(s["ev_after_fee_ct"] - rebate_addback) > 1e-9, "still double-counting the rebate"


def test_drops_zero_entry_rows():
    # A disappeared-reconcile row can carry entry_cents=0 (unknown entry): authoritative pnl but no
    # usable price → must be excluded from the entry-priced breakdown, like gap_settled rows.
    rows = [_settle("KXHIGHMIA-26JUN30-B93.5", "yes", 4, 60, -240),
            _settle("KXLOWTDEN-26JUN30-T54", "no", 5, 0, 375)]  # entry 0 → excluded
    clean = feb._clean_settlements(rows)
    assert len(clean) == 1 and clean[0]["side"] == "yes"


def test_slice_counts_distinct_events_not_positions():
    # Two strikes of the SAME event (series+city+date) = 1 event, 2 positions — the exact
    # concentration signal the per-contract slices were hiding (2026-07-09).
    rows = [_settle("KXHIGHTNOLA-26JUN30-B92.5", "yes", 4, 40, -160),
            _settle("KXHIGHTNOLA-26JUN30-B94.5", "yes", 3, 35, -105),
            _settle("KXHIGHTHOU-26JUN30-B93.5", "no", 5, 25, 375)]   # different event
    s = feb._slice(rows)
    assert s["n"] == 3 and s["events"] == 2


def test_analyze_labeled_not_decision_grade():
    # The JSON artifact must carry the honesty labels so downstream readers (and future
    # sessions briefing off it) can't mistake per-contract slices for decision-grade evidence.
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        (dd / "settlement-log.jsonl").write_text(
            json.dumps(_settle("KXHIGHTNOLA-26JUN30-B92.5", "yes", 4, 40, -160)))
        (dd / "maker-lifecycle.jsonl").write_text("")
        (dd / "paper-book.json").write_text(json.dumps({"open": [], "closed": []}))
        a = feb.analyze(dd)
    assert a["decision_grade"] is False
    assert "not event-clustered" in a["weighting"]
    assert "pocket_lever_decision" in a["note"]
    assert a["overall"]["events"] == 1
    # report renders the header + ev= column without crashing
    txt = feb.report(a)
    assert "NOT DECISION-GRADE" in txt and "ev=" in txt


if __name__ == "__main__":
    test_parsing();                    print("[PASS] ticker parsing (city/type/lead)")
    test_side_split_and_reconcile();   print("[PASS] side split + reconcile invariant")
    test_drops_gap_settled_rows();     print("[PASS] gap/malformed rows dropped")
    test_fill_rate_matches_reconcile();print("[PASS] live fill-rate matches reconcile_fills")
    test_ev_after_fee_is_already_net();print("[PASS] ev_after_fee is the net EV (no rebate double-count)")
    test_drops_zero_entry_rows();      print("[PASS] zero-entry rows dropped")
    test_slice_counts_distinct_events_not_positions(); print("[PASS] events = distinct clusters")
    test_analyze_labeled_not_decision_grade();         print("[PASS] not-decision-grade labels present")
    print("\nAll fill-edge-breakdown tests pass.")

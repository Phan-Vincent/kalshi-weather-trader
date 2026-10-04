#!/usr/bin/env python3
"""
bin/backfill_brier.py — One-shot backfill: patch brier-log open records with
settlement outcomes from settlement-log.jsonl.

Reads state/brier-log.jsonl and state/settlement-log.jsonl, matches each
settlement to its open brier record, rewrites the brier log in-place so the
open record becomes settled with actual, outcome, our_brier, market_brier, etc.
Removes duplicate settled append records.

BACKS UP brier-log.jsonl before writing.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from trader.brier import _brier_path


BRIER_PATH = _brier_path()
SETTLE_PATH = ROOT / "state" / "settlement-log.jsonl"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def backfill() -> dict:
    # ── 1. Load brier records ────────────────────────────────────────
    brier_records: list[dict] = []
    with open(BRIER_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                brier_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ── 2. Load + deduplicate settlement records ─────────────────────
    raw_settlement_records: list[dict] = []
    with open(SETTLE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw_settlement_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Deduplicate by paper_order_id (keep first, since later duplicates
    # tend to have spurious qty/pnl). This prevents the same position
    # from inflating the settled sample when matched against multiple
    # open brier records.
    seen_oids: set[str] = set()
    settlement_records: list[dict] = []
    for srec in raw_settlement_records:
        oid = srec.get("paper_order_id")
        if oid is None:
            settlement_records.append(srec)
            continue
        if oid not in seen_oids:
            seen_oids.add(oid)
            settlement_records.append(srec)

    # If after dedup there are no settlement records, nothing to do
    if not settlement_records:
        print("[backfill] No settlement records to process.")
        return {"matched": 0, "unmatched": 0, "backup": None,
                "post_counts": {}, "deduped_from": len(raw_settlement_records)}

    print(f"[backfill] Brier records: {len(brier_records)}")
    print(f"[backfill] Settlement records: {len(raw_settlement_records)} "
          f"(deduped to {len(settlement_records)})")

    # ── 3. IDEMPOTENCY: which settlements already processed? ────────
    # Track paper_order_ids already present in settled brier records.
    # This allows incremental backfill: new settlement records are
    # processed; already-processed ones are skipped.
    already_processed_oids: set[str] = set()
    legacy_settled_count = 0
    for rec in brier_records:
        if rec.get("status") == "settled":
            if rec.get("paper_order_id"):
                already_processed_oids.add(rec["paper_order_id"])
            elif "actual" in rec:
                legacy_settled_count += 1

    # LEGACY STATE GUARD: if the brier log contains in-place settled records
    # from a pre-paper_order_id backfill, we cannot safely do incremental
    # matching (the same settlements might match to different open records).
    # Abort and require a one-time migration (or restore pristine log).
    if legacy_settled_count >= len(settlement_records) and len(settlement_records) > 0:
        print(f"[backfill] ABORT: {legacy_settled_count} legacy settled records found "
              f"(>= {len(settlement_records)} unique settlements). These lack "
              f"paper_order_id tracking, so re-running backfill is unsafe. "
              f"Run state migration to add paper_order_id, or restore the "
              f"pristine pre-backfill brier-log first.")
        return {"aborted": True, "legacy_settled_count": legacy_settled_count,
                "settlements": len(settlement_records)}

    unprocessed_settlements = [
        s for s in settlement_records
        if s.get("paper_order_id") not in already_processed_oids
    ]
    skipped = len(settlement_records) - len(unprocessed_settlements)
    if skipped:
        print(f"[backfill] Skipping {skipped} already-processed settlements.")

    if not unprocessed_settlements:
        print("[backfill] All settlements already processed. Nothing to do.")
        return {"matched": 0, "unmatched": 0, "backup": None,
                "post_counts": {}, "skipped": skipped}

    # ── 4. Build maps ────────────────────────────────────────────────
    open_by_id: dict[str, dict] = {}
    settled_by_id: dict[str, list[dict]] = defaultdict(list)
    for rec in brier_records:
        rid = rec.get("record_id")
        if not rid:
            continue
        status = rec.get("status")
        if status == "open":
            open_by_id[rid] = rec
        elif status == "settled":
            settled_by_id[rid].append(rec)

    # Fingerprint map for open records: (ticker, side, qty, price_cents, our_prob) -> rid
    open_by_fp: dict[tuple, str] = {}
    for rid, rec in open_by_id.items():
        fp = (rec.get("ticker"), rec.get("side"), rec.get("qty"), rec.get("price_cents"), rec.get("our_prob"))
        open_by_fp[fp] = rid

    # ── 5. Match settlements to open records ───────────────────────
    matched: list[tuple[str, dict]] = []   # (rid, settlement_record)
    unmatched: list[dict] = []
    matched_rids = set()

    for srec in unprocessed_settlements:
        # Strategy 1: already have a settled brier record with this record_id
        rid_matched = None
        for rid, srecs in settled_by_id.items():
            if rid in open_by_id and rid not in matched_rids:
                orec = open_by_id[rid]
                if (orec.get("ticker") == srec.get("ticker") and
                        orec.get("side") == srec.get("side") and
                        orec.get("qty") == srec.get("qty")):
                    rid_matched = rid
                    break

        if rid_matched:
            matched.append((rid_matched, srec))
            matched_rids.add(rid_matched)
            continue

        # Strategy 2: exact fingerprint match
        fp = (srec.get("ticker"), srec.get("side"), srec.get("qty"),
              srec.get("entry_cents"), srec.get("fair_prob_at_open"))
        if fp in open_by_fp:
            rid = open_by_fp[fp]
            if rid not in matched_rids:
                matched.append((rid, srec))
                matched_rids.add(rid)
                continue

        # Strategy 3: relaxed match (ticker+side+qty, closest timestamp)
        ticker = srec.get("ticker")
        side = srec.get("side")
        qty = srec.get("qty")
        candidates = []
        for rid, orec in open_by_id.items():
            if rid in matched_rids:
                continue
            if (orec.get("ticker") == ticker and
                    orec.get("side") == side and
                    orec.get("qty") == qty):
                candidates.append((rid, orec))

        if candidates:
            s_time = _parse_iso(srec.get("opened_utc", "1970-01-01T00:00:00+00:00"))
            best_rid = None
            best_diff = None
            for rid, orec in candidates:
                o_time = _parse_iso(orec.get("timestamp_utc", "1970-01-01T00:00:00+00:00"))
                diff = abs((o_time - s_time).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_rid = rid
            if best_rid:
                matched.append((best_rid, srec))
                matched_rids.add(best_rid)
                continue

        unmatched.append(srec)

    print(f"[backfill] Matched: {len(matched)}, Unmatched: {len(unmatched)}")
    for u in unmatched:
        print(f"[backfill] UNMATCHED: {u.get('ticker')} {u.get('side')} qty={u.get('qty')} oid={u.get('paper_order_id')}")

    # ── 6. Backup and rewrite brier-log ────────────────────────────
    backup = BRIER_PATH.with_suffix(
        f".jsonl.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(BRIER_PATH, backup)
    print(f"[backfill] Backup: {backup}")

    # Patch matched open records in-place
    patched_rids = set()
    for rid, srec in matched:
        orec = open_by_id[rid]
        outcome = srec.get("settlement_result")
        actual = 1.0 if outcome == "yes" else 0.0
        outcome_int = 1 if outcome == "yes" else 0

        side = orec.get("side")
        our_prob_yes = float(orec.get("our_prob", 0))
        market_prob_side = float(orec.get("market_prob", 0))

        if outcome == "yes":
            our_prob_for_outcome = our_prob_yes
            market_prob_for_outcome = market_prob_side if side == "yes" else (1.0 - market_prob_side)
        else:
            our_prob_for_outcome = 1.0 - our_prob_yes
            market_prob_for_outcome = (1.0 - market_prob_side) if side == "yes" else market_prob_side

        # FIX 2026-06-01: winning side occurred -> score against 1.0, not outcome_int.
        our_brier = round((our_prob_for_outcome - 1.0) ** 2, 6)
        market_brier = round((market_prob_for_outcome - 1.0) ** 2, 6)

        orec["status"] = "settled"
        orec["actual"] = actual
        orec["outcome"] = outcome
        orec["settled_at_utc"] = srec.get("settled_at_utc")
        orec["our_brier"] = our_brier
        orec["market_brier"] = market_brier
        orec["our_prob_for_outcome"] = round(our_prob_for_outcome, 6)
        orec["market_prob_for_outcome"] = round(market_prob_for_outcome, 6)
        orec["paper_order_id"] = srec.get("paper_order_id")
        patched_rids.add(rid)

    # Rebuild the file from scratch:
    #   - Patched open records become settled (keep them)
    #   - Unpatched open records stay open
    #   - Original settled append records for patched rids are dropped
    #   - Original settled append records for unpatched rids are kept (deduped)
    deduped: list[dict] = []
    for rid, orec in open_by_id.items():
        if rid in patched_rids:
            deduped.append(orec)          # already patched -> settled
        else:
            deduped.append(orec)          # still open

    # Keep unpatched settled append records (deduped by record_id)
    seen_settled = set()
    for rec in brier_records:
        rid = rec.get("record_id")
        status = rec.get("status")
        if status == "settled" and rid not in patched_rids:
            if rid in seen_settled:
                continue
            seen_settled.add(rid)
            deduped.append(rec)

    with open(BRIER_PATH, "w") as f:
        for rec in deduped:
            f.write(json.dumps(rec) + "\n")

    print(f"[backfill] Wrote {len(deduped)} records (deduped from {len(brier_records)})")

    # ── 7. Verify ──────────────────────────────────────────────────
    counts = {"open": 0, "settled": 0, "with_actual": 0}
    with open(BRIER_PATH) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                counts[rec.get("status", "?")] += 1
                if "actual" in rec:
                    counts["with_actual"] += 1
            except Exception:
                pass
    print(f"[backfill] Post-backfill counts: {counts}")

    # Strict cardinality check: settled count should equal unique processed
    # settlement count (deduped + already processed)
    expected_settled = len(settlement_records)
    if counts["with_actual"] != expected_settled:
        print(f"[backfill] WARNING: settled count {counts['with_actual']} != "
              f"expected {expected_settled} (unique settlements)")

    return {
        "matched": len(matched),
        "unmatched": len(unmatched),
        "backup": str(backup),
        "post_counts": counts,
        "deduped_from": len(raw_settlement_records),
        "expected_settled": expected_settled,
        "skipped": skipped,
    }


if __name__ == "__main__":
    result = backfill()
    print(json.dumps(result, indent=2))

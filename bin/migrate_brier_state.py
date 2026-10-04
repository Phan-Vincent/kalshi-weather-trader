#!/usr/bin/env python3
"""
bin/migrate_brier_state.py — One-time migration: add paper_order_id to legacy
settled brier records by matching against settlement-log.jsonl.

This removes the legacy guard in backfill_brier.py so future incremental
backfills work correctly.

Usage:
  python3 bin/migrate_brier_state.py [--dry-run]
"""
from __future__ import annotations

import json
import shutil
import sys
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


def migrate(dry_run: bool = False) -> dict:
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

    # ── 2. Load settlement records ───────────────────────────────────
    settlement_records: list[dict] = []
    with open(SETTLE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                settlement_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Index settlements by (ticker, side, qty, entry_cents, opened_utc)
    # Keep first in case of duplicates; we just need a paper_order_id.
    settle_by_fp: dict[tuple, dict] = {}
    for srec in settlement_records:
        fp = (srec.get("ticker"), srec.get("side"), srec.get("qty"),
              srec.get("entry_cents"), srec.get("opened_utc"))
        if fp not in settle_by_fp:
            settle_by_fp[fp] = srec

    # Also build open-record map for legacy settled append records
    open_by_id: dict[str, dict] = {}
    for rec in brier_records:
        if rec.get("status") == "open":
            open_by_id[rec.get("record_id")] = rec

    # Also index by paper_order_id for direct lookups
    settle_by_oid: dict[str, dict] = {}
    for srec in settlement_records:
        oid = srec.get("paper_order_id")
        if oid and oid not in settle_by_oid:
            settle_by_oid[oid] = srec

    migrated = 0
    already_ok = 0
    no_match = 0

    for rec in brier_records:
        if rec.get("status") != "settled" or "actual" not in rec:
            continue
        if rec.get("paper_order_id"):
            already_ok += 1
            continue

        # Try exact fingerprint match using the record itself (in-place)
        fp = (rec.get("ticker"), rec.get("side"), rec.get("qty"),
              rec.get("price_cents"), rec.get("timestamp_utc"))
        srec = settle_by_fp.get(fp)
        if srec:
            rec["paper_order_id"] = srec.get("paper_order_id")
            migrated += 1
            continue

        # For legacy settled append records (no ticker/side), look up the open record
        rid = rec.get("record_id")
        open_rec = open_by_id.get(rid)
        if open_rec:
            fp2 = (open_rec.get("ticker"), open_rec.get("side"), open_rec.get("qty"),
                   open_rec.get("price_cents"), open_rec.get("timestamp_utc"))
            srec = settle_by_fp.get(fp2)
            if srec:
                rec["paper_order_id"] = srec.get("paper_order_id")
                migrated += 1
                continue

        # Try relaxed match: ticker+side+qty, closest timestamp
        # For in-place records, use rec directly; for legacy, use open_rec
        lookup_rec = rec if rec.get("ticker") else open_by_id.get(rid)
        if not lookup_rec:
            no_match += 1
            continue

        candidates = [
            s for s in settlement_records
            if (s.get("ticker") == lookup_rec.get("ticker") and
                s.get("side") == lookup_rec.get("side") and
                s.get("qty") == lookup_rec.get("qty"))
        ]
        if candidates:
            r_time = _parse_iso(lookup_rec.get("timestamp_utc", "1970-01-01T00:00:00+00:00"))
            best = min(candidates,
                       key=lambda s: abs(
                           (_parse_iso(s.get("opened_utc", "1970-01-01T00:00:00+00:00")) - r_time).total_seconds()
                       ))
            rec["paper_order_id"] = best.get("paper_order_id")
            migrated += 1
            continue

        no_match += 1

    print(f"[migrate] Brier records: {len(brier_records)}")
    print(f"[migrate] Settlement records: {len(settlement_records)}")
    print(f"[migrate] Already have paper_order_id: {already_ok}")
    print(f"[migrate] Migrated: {migrated}")
    print(f"[migrate] No match found: {no_match}")

    if dry_run:
        print("[migrate] DRY RUN — no changes written.")
        return {"migrated": migrated, "already_ok": already_ok, "no_match": no_match, "dry_run": True}

    if migrated == 0 and no_match == 0:
        print("[migrate] Nothing to migrate.")
        return {"migrated": 0, "already_ok": already_ok, "no_match": 0}

    # Backup
    backup = BRIER_PATH.with_suffix(
        f".jsonl.migrate-bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(BRIER_PATH, backup)
    print(f"[migrate] Backup: {backup}")

    with open(BRIER_PATH, "w") as f:
        for rec in brier_records:
            f.write(json.dumps(rec) + "\n")

    print(f"[migrate] Wrote {len(brier_records)} records.")
    return {"migrated": migrated, "already_ok": already_ok, "no_match": no_match, "backup": str(backup)}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = migrate(dry_run=dry)
    print(json.dumps(result, indent=2))

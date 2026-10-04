#!/usr/bin/env python3
"""
trader/brier.py — Brier-score + calibration logger for Kalshi weather paper-trading bot.

Deterministic, dependency-free. Persisted as append-only JSONL at
state/brier-log.jsonl.
"""
from __future__ import annotations

import json
import math
import os
import random
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── paths ────────────────────────────────────────────────────────────

def _brier_path() -> Path:
    base = os.environ.get("KALSHI_WEATHER_STATE_DIR",
          str(Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"))
    p = Path(base) / "brier-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── BrierLogger ──────────────────────────────────────────────────────

class BrierLogger:
    """
    Append-only JSONL logger for prediction calibration.
    Each prediction gets a uuid. Open record written at trade entry;
    settled record appended at settlement.
    """

    def __init__(self, log_path: Optional[Path] = None):
        self.log_path = log_path or _brier_path()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_prediction(
        self,
        ticker: str,
        side: str,
        our_prob: float,
        market_prob: float,
        qty: int,
        price_cents: int,
        mode: str,
        timestamp_utc: str,
        source: str = "live",
        best_bid: Optional[int] = None,
        best_ask: Optional[int] = None,
    ) -> str:
        """
        Log an open prediction at time of trade entry.
        our_prob   = signal.fair_prob (probability of YES)
        market_prob = price we got filled at, as probability of the side traded
        best_bid/best_ask = orderbook snapshot at entry (for fill-rate model)
        Returns record_id (uuid4).
        """
        record_id = str(uuid.uuid4())
        record = {
            "record_id": record_id,
            "status": "open",
            "ticker": ticker,
            "side": side,
            "our_prob": round(float(our_prob), 6),
            "market_prob": round(float(market_prob), 6),
            "qty": int(qty),
            "price_cents": int(price_cents),
            "mode": mode,
            "timestamp_utc": timestamp_utc,
            "source": source,
        }
        if best_bid is not None:
            record["best_bid"] = int(best_bid)
        if best_ask is not None:
            record["best_ask"] = int(best_ask)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        return record_id

    def record_outcome(self, record_id: str, outcome: bool) -> None:
        """
        Append a settlement record for an existing open prediction.
        outcome=True  → YES won
        outcome=False → NO won

        our_brier   = (our_prob_for_outcome - outcome_int)**2
        market_brier = (market_prob_for_outcome - outcome_int)**2
        prob_for_outcome = probability assigned to the side that actually won.
        """
        # Need to read the open record to get our_prob, market_prob, side
        open_record = self._find_open_record(record_id)
        if open_record is None:
            raise ValueError(f"No open record found for {record_id}")

        # Guard against duplicate settlement
        if self._is_settled(record_id):
            return

        side = open_record["side"]
        our_prob_yes = float(open_record["our_prob"])
        market_prob_side = float(open_record["market_prob"])
        outcome_int = 1 if outcome else 0

        # Probability we (and market) assigned to the side that WON
        if outcome:  # YES won
            our_prob_for_outcome = our_prob_yes
            market_prob_for_outcome = market_prob_side if side == "yes" else (1.0 - market_prob_side)
        else:  # NO won
            our_prob_for_outcome = 1.0 - our_prob_yes
            market_prob_for_outcome = (1.0 - market_prob_side) if side == "yes" else market_prob_side

        # FIX 2026-06-01: our_prob_for_outcome is P(side that WON); the winning
        # outcome by definition occurred, so it must be scored against 1.0, NOT
        # outcome_int (yes/no space). Prior code mixed spaces -> every NO-wins
        # trade scored backwards (inflated Brier, inverted directional accuracy).
        our_brier = (our_prob_for_outcome - 1.0) ** 2
        market_brier = (market_prob_for_outcome - 1.0) ** 2

        settle_record = {
            "record_id": record_id,
            "status": "settled",
            "outcome": "yes" if outcome else "no",
            "actual": 1.0 if outcome else 0.0,
            "our_brier": round(our_brier, 6),
            "market_brier": round(market_brier, 6),
            "our_prob_for_outcome": round(our_prob_for_outcome, 6),
            "market_prob_for_outcome": round(market_prob_for_outcome, 6),
            "settled_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(settle_record) + "\n")

    def find_record_by_fingerprint(self, ticker: str, side: str, qty: int, price_cents: int, our_prob: float) -> Optional[str]:
        """
        Find an open brier record by trade fingerprint.
        Returns record_id or None.
        """
        if not self.log_path.exists():
            return None
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") != "open":
                    continue
                if (rec.get("ticker") == ticker and
                        rec.get("side") == side and
                        rec.get("qty") == qty and
                        rec.get("price_cents") == price_cents and
                        abs(float(rec.get("our_prob", 0)) - float(our_prob)) < 1e-6):
                    return rec.get("record_id")
        return None

    def _find_open_record(self, record_id: str) -> Optional[dict]:
        """Scan JSONL for the open record with this record_id."""
        if not self.log_path.exists():
            return None
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("record_id") == record_id and rec.get("status") == "open":
                    return rec
        return None

    def _is_settled(self, record_id: str) -> bool:
        """Check if a settled record already exists for this record_id."""
        if not self.log_path.exists():
            return False
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("record_id") == record_id and rec.get("status") == "settled":
                    return True
        return False

    def summary(self, min_trades: int = 0) -> dict:
        """
        Read entire JSONL, pair open + settled by record_id, compute summary stats.

        Supports two formats:
        - Legacy: open record + separate settled append record (paired by record_id)
        - New: single in-place settled record with all fields (status == "settled")
        """
        opens: dict[str, dict] = {}
        settled: dict[str, dict] = {}
        all_settled: list[dict] = []
        all_open: list[dict] = []

        if self.log_path.exists():
            with open(self.log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = rec.get("record_id")
                    if not rid:
                        continue
                    status = rec.get("status")
                    if status == "open":
                        opens[rid] = rec
                        all_open.append(rec)
                    elif status == "settled":
                        settled[rid] = rec
                        all_settled.append(rec)

        # Build paired list: in-place settled records OR open+settled pairs
        paired: list[dict] = []
        seen = set()
        for rec in all_settled:
            rid = rec.get("record_id")
            if rid in seen:
                continue
            seen.add(rid)
            # If there's a matching open record, merge (legacy mode)
            o = opens.get(rid)
            if o:
                paired.append({**o, **rec})
            else:
                # In-place settled record already has all fields
                paired.append(rec)

        n_trades = len(paired)
        # n_open = records that are open but NOT yet settled (legacy open+settled pairs)
        settled_rids = {p.get("record_id") for p in paired}
        n_open = sum(1 for rid in opens if rid not in settled_rids)

        if n_trades == 0:
            return {
                "n_trades": 0,
                "n_open": n_open,
                "our_brier_mean": None,
                "market_brier_mean": None,
                "brier_skill_score": None,
                "directional_accuracy": None,
                "calibration_buckets": [],
                "gate_status": "WAITING",
            }

        our_briers = [p["our_brier"] for p in paired]
        market_briers = [p["market_brier"] for p in paired]
        our_brier_mean = sum(our_briers) / n_trades
        market_brier_mean = sum(market_briers) / n_trades

        if market_brier_mean == 0:
            brier_skill_score = None
        else:
            brier_skill_score = 1.0 - (our_brier_mean / market_brier_mean)

        # Directional accuracy: our_prob_for_outcome is P(side that WON), which
        # by definition occurred. So a "correct" directional call means we assigned
        # >0.5 to the side that ended up winning.
        # FIX 2026-06-01: prior code compared against `actual` (yes-space) while
        # our_prob_for_outcome is winning-side-space -> inverted accuracy.
        correct_directional = 0
        for p in paired:
            our_prob = p.get("our_prob_for_outcome", 0.0)
            if our_prob > 0.5:
                correct_directional += 1
        directional_accuracy = correct_directional / n_trades if n_trades > 0 else None

        # Calibration buckets: deciles of our_prob [0-10%, 10-20%, ..., 90-100%]
        # Calibration in YES-space: bucket trades by P(yes) we forecast, compare to
        # the rate at which YES actually won in that bucket. This is the meaningful
        # reliability diagram. (our_prob is P(yes); actual==1.0 means YES won.)
        # FIX 2026-06-01: prior code bucketed by our_prob_for_outcome (winning-side
        # space) vs yes-space outcome -> degenerate/inverted. Now both yes-space.
        buckets: list[dict] = []
        for i in range(10):
            lo = i / 10.0
            hi = (i + 1) / 10.0
            label = f"{int(lo*100)}-{int(hi*100)}%"
            items = []
            for p in paired:
                prob = float(p.get("our_prob", 0.0))  # P(yes)
                if (lo <= prob < hi) or (hi == 1.0 and prob == 1.0):
                    items.append(p)
            if items:
                mean_our = sum(float(p.get("our_prob", 0.0)) for p in items) / len(items)
                mean_outcome = sum(
                    (p.get("actual") if p.get("actual") is not None
                     else (1.0 if p.get("outcome") == "yes" else 0.0))
                    for p in items
                ) / len(items)
            else:
                mean_our = None
                mean_outcome = None
            buckets.append({
                "range": label,
                "n": len(items),
                "mean_our_prob": round(mean_our, 4) if mean_our is not None else None,
                "mean_outcome": round(mean_outcome, 4) if mean_outcome is not None else None,
                "diff": round(mean_our - mean_outcome, 4) if mean_our is not None else None,
            })

        # Worst calibration decile (largest absolute diff)
        worst = None
        worst_diff = -1
        for b in buckets:
            if b["diff"] is not None and abs(b["diff"]) > worst_diff:
                worst_diff = abs(b["diff"])
                worst = b

        # Gate status with statistical significance (z-test via paired bootstrap)
        # FIX 2026-06-11: point-estimate comparison replaced with bootstrap z-test
        # to prevent false confidence from small-sample noise.
        bss_se = None
        z_score = None
        if n_trades < 150:
            gate_status = "WAITING"
        elif our_brier_mean < market_brier_mean:
            # Bootstrap BSS to get standard error
            bss = brier_skill_score
            bss_se = _bootstrap_bss_se(paired, n_bootstrap=500)
            if bss_se is not None and bss_se > 0:
                z_score = bss / bss_se
                if z_score >= 1.96:
                    gate_status = "GREEN"  # significant at 95%+
                elif z_score >= 1.28:
                    gate_status = "LIGHT_GREEN"  # significant at 90%+
                else:
                    gate_status = "YELLOW"  # favorable but not significant
            else:
                gate_status = "LIGHT_GREEN"  # fallback: favorable direction
        elif our_brier_mean >= market_brier_mean:
            bss = brier_skill_score if brier_skill_score is not None else 0
            bss_se = _bootstrap_bss_se(paired, n_bootstrap=500)
            if bss_se is not None and bss_se > 0:
                z_score = bss / bss_se
                if z_score <= -1.96:
                    gate_status = "RED"  # significantly worse
                elif z_score <= -1.28:
                    gate_status = "LIGHT_RED"  # marginally worse
                else:
                    gate_status = "YELLOW"  # negative but not significant
            else:
                gate_status = "RED"  # fallback: unfavorable direction
        else:
            gate_status = "WAITING"

        # City counts
        city_counts: dict[str, int] = defaultdict(int)
        for p in paired:
            city = _extract_city(p.get("ticker", ""))
            if city:
                city_counts[city] += 1
        top_city = max(city_counts, key=city_counts.get) if city_counts else None

        result = {
            "n_trades": n_trades,
            "n_open": n_open,
            "our_brier_mean": round(our_brier_mean, 6),
            "market_brier_mean": round(market_brier_mean, 6),
            "brier_skill_score": round(brier_skill_score, 6) if brier_skill_score is not None else None,
            "bss_se": round(bss_se, 6) if bss_se is not None else None,
            "z_score": round(z_score, 3) if z_score is not None else None,
            "directional_accuracy": round(directional_accuracy, 4) if directional_accuracy is not None else None,
            "calibration_buckets": buckets,
            "worst_bucket": worst,
            "gate_status": gate_status,
            "top_city": top_city,
            "city_counts": dict(city_counts),
        }

        if n_trades < min_trades:
            result["_filtered"] = True
        return result


def _bootstrap_bss_se(paired: list[dict], n_bootstrap: int = 500) -> float | None:
    """Bootstrap standard error of Brier Skill Score via paired resampling.

    For each bootstrap iteration:
    1. Resample n trades with replacement (pairs of our_brier, market_brier)
    2. Compute BSS = 1 - mean(our) / mean(market)
    3. Repeat n_bootstrap times
    4. Return std of BSS samples

    Returns None if insufficient data.
    """
    if len(paired) < 10:
        return None

    n = len(paired)
    our = [p["our_brier"] for p in paired]
    market = [p["market_brier"] for p in paired]

    bss_samples: list[float] = []
    for _ in range(n_bootstrap):
        idxs = [random.randint(0, n - 1) for _ in range(n)]
        our_mean = sum(our[i] for i in idxs) / n
        market_mean = sum(market[i] for i in idxs) / n
        if market_mean > 0:
            bss_samples.append(1.0 - our_mean / market_mean)
        else:
            bss_samples.append(0.0)

    if not bss_samples:
        return None

    mean_bss = sum(bss_samples) / len(bss_samples)
    variance = sum((b - mean_bss) ** 2 for b in bss_samples) / (len(bss_samples) - 1)
    return math.sqrt(variance) if variance > 0 else None


# ── helpers ──────────────────────────────────────────────────────────

def _extract_city(ticker: str) -> str:
    """City code from a Kalshi weather ticker. QA-12 (2026-07-01): delegates to the shared
    data.weather_data.extract_city so brier + calibrate never diverge — the old local tag list
    omitted the bare KXHIGH<CITY> form and silently dropped NY/MIA/LAX from per-city reports.

    Import robust to context (2026-07-02 QA-all): works whether loaded top-level (`data.*`) or via
    the automations/ symlink as `kalshi_weather.*` — same class as the calibrate build_fair_values break."""
    try:
        from data.weather_data import extract_city
    except ModuleNotFoundError:
        from kalshi_weather.data.weather_data import extract_city
    return extract_city(ticker)

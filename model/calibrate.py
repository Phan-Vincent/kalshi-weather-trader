#!/usr/bin/env python3
"""
model/calibrate.py — Probability calibration for fair-value outputs.

TWO calibrators provided:
  1. BetaCalibrator      — single-parameter power transform (legacy/fallback)
  2. IsotonicCalibrator  — non-parametric isotonic regression (primary)

Isotonic regression (Pool Adjacent Violators Algorithm) is the gold standard
for probability calibration. Unlike Beta (one parameter), it fits a monotonic
piecewise-constant function that can push 4%→34% while only nudging 44%→51%.

For prediction: linear interpolation between adjacent bin means on the
logit-transformed space (with a small amount of regularization).

Usage:
    from kalshi_weather.model.calibrate import IsotonicCalibrator
    cal = IsotonicCalibrator()
    cal.fit()                          # fit from settled brier log
    cal.calibrate(0.15)                # → 0.34 (example)
    cal.save()                         # persist to state/calibration.json

    # Or: auto-select best calibrator
    from kalshi_weather.model.calibrate import AutoCalibrator
    cal = AutoCalibrator()
    cal.fit()
    cal.calibrate(0.15)
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_STATE_DIR = Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"
# Fit source (quant review 2026-07-01, QA-14 class): the ROOT state/brier-log.jsonl froze when
# the arms moved to state/<arm>/ (no settlements after 6/19) — fitting from it produced the
# stale "BSS -0.121" headline. Default to the SETTLING live-main book (state/paper);
# KALSHI_WEATHER_BRIER_LOG overrides for experiments. Artifacts (calibration.json etc.)
# intentionally stay at root state/ — the scanner/dashboards load them from there.
_BRIER_LOG = Path(os.environ.get("KALSHI_WEATHER_BRIER_LOG", str(_STATE_DIR / "paper" / "brier-log.jsonl")))
_CALIBRATION_PATH = _STATE_DIR / "calibration.json"
_BETA_CALIBRATION_PATH = _STATE_DIR / "beta-calibration.json"

# Golden-section search constants
_GOLDEN_RATIO = (math.sqrt(5.0) - 1.0) / 2.0  # ≈ 0.618


# ── Data loading (shared) ───────────────────────────────────────────

def _load_settled_trades(brier_path: Optional[Path] = None) -> list[dict]:
    """Load (fair_prob, actual_outcome) pairs from brier log."""
    path = brier_path or _BRIER_LOG
    if not path.exists():
        return []

    opens: dict[str, dict] = {}
    settled: list[dict] = []

    with open(path) as f:
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
            elif status == "settled":
                settled.append(rec)

    paired = []
    for s in settled:
        rid = s.get("record_id")
        o = opens.get(rid)
        if o:
            paired.append({**o, **s})
        else:
            paired.append(s)

    return [
        {
            "p": float(p["our_prob"]),
            "y": 1.0 if p.get("outcome") == "yes" else 0.0,
            "ticker": p.get("ticker", ""),
        }
        for p in paired
        if p.get("our_prob") is not None and p.get("outcome")
    ]


def _extract_city(ticker: str) -> str:
    """QA-12 (2026-07-01): delegate to the shared data.weather_data.extract_city so calibrate +
    brier city bucketing stay identical.

    REGRESSION FIX (2026-07-02, QA-all): calibrate is imported BOTH as top-level `model.calibrate`
    (repo root on sys.path → `data.weather_data`) AND as `kalshi_weather.model.calibrate` via the
    automations/ symlink (only automations/ on sys.path → `kalshi_weather.data.weather_data`). The
    bare `from data.weather_data` broke build_fair_values every cycle since 2026-07-01
    (ModuleNotFoundError: No module named 'data' → fair-values silently stale). Try both paths."""
    try:
        from data.weather_data import extract_city
    except ModuleNotFoundError:
        from kalshi_weather.data.weather_data import extract_city
    return extract_city(ticker)


def _brier_score(trades: list[dict], calibrate_fn) -> float:
    """Compute Brier score after applying calibration_fn to each prob."""
    if not trades:
        return float("inf")
    total = 0.0
    for t in trades:
        p_cal = calibrate_fn(t["p"])
        total += (p_cal - t["y"]) ** 2
    return total / len(trades)


# ══════════════════════════════════════════════════════════════════════
#  ISOTONIC CALIBRATOR (primary)
# ══════════════════════════════════════════════════════════════════════

def _pava(ys: List[float], weights: List[float]) -> List[float]:
    """Weighted Pool-Adjacent-Violators: the non-decreasing sequence closest (weighted L2) to `ys`.
    Repairs monotonicity after per-bucket Laplace regularization, which can otherwise make a higher
    raw prob map to a lower calibrated prob (2026-07-03 weather-model QA)."""
    blocks: List[List[float]] = []  # each: [weighted_sum, weight, count]
    for y, w in zip(ys, weights):
        w = w if w > 0 else 1e-9
        blocks.append([y * w, w, 1])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s2, w2, c2 = blocks.pop()
            s1, w1, c1 = blocks.pop()
            blocks.append([s1 + s2, w1 + w2, c1 + c2])
    out: List[float] = []
    for s, w, c in blocks:
        out.extend([s / w] * c)
    return out


_CV_FOLDS = 5
# A calibrator must beat identity's OOS Brier by at least this (relative) to be deployed — a noise
# guard so a live-pricing layer doesn't flip on/off cycle-to-cycle on sampling jitter near 0 gain.
_MIN_OOS_GAIN_REL = 0.005


def _kfold_oos_brier(fit_fn, trades: list, k: int = _CV_FOLDS) -> Optional[float]:
    """Pooled held-out Brier for a calibrator family. fit_fn(train_trades) -> callable(p)->p_cal.
    Deterministic folds (predicted-prob-sorted, round-robin) — no RNG, stable cycle-to-cycle so the
    deploy/skip decision doesn't jitter. Returns None if too few trades to split."""
    n = len(trades)
    if n < 2 * k:
        return None
    ordered = sorted(trades, key=lambda t: t["p"])
    folds = [ordered[i::k] for i in range(k)]
    se, m = 0.0, 0
    for j in range(k):
        test = folds[j]
        train = [t for i in range(k) if i != j for t in folds[i]]
        if len(train) < 10 or not test:
            continue
        cal = fit_fn(train)
        for t in test:
            se += (cal(t["p"]) - t["y"]) ** 2
            m += 1
    return (se / m) if m else None


class IsotonicCalibrator:
    """Non-parametric probability calibration via isotonic regression.

    Uses the Pool Adjacent Violators Algorithm (PAVA) to fit a monotonic
    calibration curve, then predicts via linear interpolation between
    adjacent bin means.

    Key advantage over BetaCalibrator: different probability ranges get
    different amounts of correction. The low-prob tail (where the model
    says 4-15% but actual is 33-40%) gets pushed up aggressively, while
    the mid-range (40-50%) gets much smaller adjustments.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = state_dir or _STATE_DIR
        self._cal_path = self._state_dir / "calibration.json"
        # Calibration curve: list of (prob_center, calibrated_value)
        self._curve: List[Tuple[float, float]] = []
        self._brier_before: Optional[float] = None
        self._brier_after: Optional[float] = None
        self._n_trades: int = 0
        self._fitted: bool = False
        self._min_prob: float = 0.01  # floor for interpolation
        self._max_prob: float = 0.99  # ceiling

    @property
    def fitted(self) -> bool:
        return self._fitted

    def calibrate(self, p: float) -> float:
        """Apply isotonic calibration to a raw probability.

        Uses linear interpolation on the calibrated curve.
        Extrapolates linearly beyond the fitted range.
        """
        if not self._fitted or not self._curve:
            return p
        if p <= 0.0:
            return 0.0
        if p >= 1.0:
            return 1.0

        curve = self._curve
        lo, hi = 0, len(curve) - 1
        # FIX 2026-07-03 (weather-model QA): CLIP to the boundary fitted value outside the trained
        # range (standard isotonic out-of-bounds behavior) instead of linear-extrapolating toward
        # 0/1. The old (0,0)-anchored linear extrapolation dragged every sub-first-bucket prob toward
        # zero, cancelling the low-prob underconfidence correction the curve exists to apply (~85% of
        # markets sit below the first fitted bucket).
        if p <= curve[lo][0]:
            return max(0.001, min(0.999, curve[lo][1]))
        if p >= curve[hi][0]:
            return max(0.001, min(0.999, curve[hi][1]))

        # Binary search
        while lo < hi:
            mid = (lo + hi) // 2
            if curve[mid][0] < p:
                lo = mid + 1
            else:
                hi = mid
        # Interpolate between curve[lo-1] and curve[lo]
        if lo == 0:
            return curve[0][1]
        x0, y0 = curve[lo - 1]
        x1, y1 = curve[lo]
        if x1 <= x0:
            return y0
        t = (p - x0) / (x1 - x0)
        result = y0 + t * (y1 - y0)
        return max(0.001, min(0.999, result))

    def fit(
        self,
        brier_path: Optional[Path] = None,
        min_trades: int = 10,
        trades: Optional[list[dict]] = None,
    ) -> "IsotonicCalibrator":
        """Fit isotonic regression from settled brier log.

        Algorithm: Pool Adjacent Violators Algorithm (PAVA)
        1. Sort trades by predicted probability
        2. Initialize each point as its own bucket
        3. While any adjacent buckets violate monotonicity (mean(y_i) > mean(y_{i+1})),
           merge them
        4. Output: [(prob_center, mean_outcome), ...] for each bucket

        `trades` (list of {"p","y",...}) overrides the file load — used by the out-of-sample
        cross-validation harness (bin/calibration_oos.py) to fit the REAL calibrator on a training
        split. Backward compatible: default None preserves the load-from-brier_path behavior.
        """
        trades = trades if trades is not None else _load_settled_trades(brier_path)
        self._n_trades = len(trades)

        if len(trades) < min_trades:
            self._curve = [(0.0, 0.0), (1.0, 1.0)]
            self._fitted = True
            self._brier_before = _brier_score(trades, lambda x: x) if trades else None
            self._brier_after = self._brier_before
            return self

        # Compute baseline Brier (identity calibration)
        self._brier_before = _brier_score(trades, lambda x: x)

        # Sort by predicted probability
        sorted_trades = sorted(trades, key=lambda t: t["p"])

        # PAVA: initialize buckets — QA-06 (2026-07-01): pre-pool trades that share an IDENTICAL
        # predicted prob into ONE bucket first. fair_prob is heavily clustered (6dp rounding, the
        # 0.12/0.03 floor, discrete climatology bins) and the strict-'>' violator merge below never
        # merges ties, so one-bucket-per-trade left tied predictions as n=1 buckets that Laplace-
        # regularized ~83% back toward the raw prob — grossly under-correcting exactly the clustered
        # low-probs isotonic exists to fix. Grouping first makes n reflect the true trade count.
        grouped: Dict[float, Dict] = {}
        for t in sorted_trades:
            g = grouped.get(t["p"])
            if g is None:
                grouped[t["p"]] = {"probs": [t["p"]], "outcomes": [t["y"]]}
            else:
                g["probs"].append(t["p"])
                g["outcomes"].append(t["y"])
        buckets: List[Dict] = [
            {"probs": g["probs"], "outcomes": g["outcomes"],
             "mean_prob": p, "mean_outcome": sum(g["outcomes"]) / len(g["outcomes"])}
            for p, g in grouped.items()
        ]

        # Merge adjacent violators
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(buckets) - 1:
                if buckets[i]["mean_outcome"] > buckets[i + 1]["mean_outcome"]:
                    # Merge i and i+1
                    merged_probs = buckets[i]["probs"] + buckets[i + 1]["probs"]
                    merged_outcomes = buckets[i]["outcomes"] + buckets[i + 1]["outcomes"]
                    buckets[i] = {
                        "probs": merged_probs,
                        "outcomes": merged_outcomes,
                        "mean_prob": sum(merged_probs) / len(merged_probs),
                        "mean_outcome": sum(merged_outcomes) / len(merged_outcomes),
                    }
                    buckets.pop(i + 1)
                    changed = True
                else:
                    i += 1

        # Build the calibration curve from the Laplace-regularized bucket outcomes.
        # FIX 2026-07-03 (weather-model QA):
        #  (A) MONOTONICITY: the per-bucket Laplace weight 5/(n+5) can BREAK the monotonicity PAVA
        #      just established (a large-n bucket followed by a small-n bucket with an equal outcome
        #      inverts), so re-pool the regularized values with a weighted PAVA. The curve MUST be
        #      non-decreasing or a higher raw prob maps to a LOWER calibrated prob (mis-ranking).
        #  (B) NO (0,0)/(1,1) ANCHORS: anchoring at (0,0) + linear interp dragged every prob below the
        #      first bucket toward 0, cancelling the low-prob underconfidence correction isotonic
        #      exists to make. calibrate() now CLIPS to the boundary outcome outside the fitted range.
        # Laplace regularization (2026-06-17): blend each bucket toward its raw prob so a small bucket
        # (n=1 → ~83% shrink; n=10 → ~33%) can't overfit; weighted PAVA then restores monotonicity.
        probs, reg_ys, weights = [], [], []
        for b in buckets:
            n = len(b["outcomes"])
            reg_weight = 5.0 / (n + 5.0)
            probs.append(round(b["mean_prob"], 6))
            reg_ys.append(reg_weight * b["mean_prob"] + (1.0 - reg_weight) * b["mean_outcome"])
            weights.append(float(n))
        self._curve = [(p, round(y, 6)) for p, y in zip(probs, _pava(reg_ys, weights))]

        # Compute calibrated Brier (must mark fitted first so calibrate() works)
        self._fitted = True
        self._brier_after = _brier_score(trades, self.calibrate)
        return self

    def save(self, path: Optional[Path] = None) -> None:
        """Persist calibration curve to disk."""
        path = path or self._cal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "method": "isotonic",
            "curve": [[round(x, 6), round(y, 6)] for x, y in self._curve],
            "brier_before": round(self._brier_before, 6) if self._brier_before else None,
            "brier_after": round(self._brier_after, 6) if self._brier_after else None,
            "n_trades": self._n_trades,
            "fitted": self._fitted,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.rename(path)

    def load(self, path: Optional[Path] = None) -> bool:
        """Load calibration curve from disk."""
        path = path or self._cal_path
        if not path.exists():
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            self._curve = [(float(x), float(y)) for x, y in data["curve"]]
            self._brier_before = data.get("brier_before")
            self._brier_after = data.get("brier_after")
            self._n_trades = data.get("n_trades", 0)
            self._fitted = data.get("fitted", False)
            return True
        except (OSError, json.JSONDecodeError, KeyError):
            return False

    def summary(self) -> dict:
        """Return human-readable calibration summary."""
        return {
            "method": "isotonic",
            "fitted": self._fitted,
            "n_trades": self._n_trades,
            "n_buckets": len(self._curve),
            "brier_before": round(self._brier_before, 6) if self._brier_before else None,
            "brier_after": round(self._brier_after, 6) if self._brier_after else None,
            "brier_improvement_pct": (
                round((1 - self._brier_after / self._brier_before) * 100, 2)
                if self._brier_before and self._brier_after and self._brier_before > 0
                else None
            ),
            "curve_sample": [
                {"prob": round(p, 4), "calibrated": round(y, 4)}
                for p, y in self._curve[::max(1, len(self._curve)//15)]
            ],
        }


# ══════════════════════════════════════════════════════════════════════
#  BETA CALIBRATOR (legacy/fallback — kept for comparison)
# ══════════════════════════════════════════════════════════════════════

def _beta_calibrate(p: float, alpha: float) -> float:
    """Beta calibration: p_cal = p^α / (p^α + (1-p)^α)"""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    if alpha == 1.0:
        return p
    num = math.pow(p, alpha)
    denom = num + math.pow(1.0 - p, alpha)
    return num / denom


def _brier_for_alpha(alpha: float, trades: list[dict]) -> float:
    return _brier_score(trades, lambda p: _beta_calibrate(p, alpha))


def _find_optimal_alpha(
    trades: list[dict],
    lo: float = 0.5,
    hi: float = 5.0,
    tol: float = 0.001,
    max_iter: int = 40,
) -> Tuple[float, float]:
    """Golden-section search for optimal Beta alpha."""
    if len(trades) < 10:
        return 1.0, _brier_for_alpha(1.0, trades)

    best = (1.0, _brier_for_alpha(1.0, trades))
    a, b = lo, hi
    c = b - _GOLDEN_RATIO * (b - a)
    d = a + _GOLDEN_RATIO * (b - a)

    for _ in range(max_iter):
        if abs(c - d) < tol:
            break
        fc = _brier_for_alpha(c, trades)
        fd = _brier_for_alpha(d, trades)
        if fc < fd:
            b = d; d = c; c = b - _GOLDEN_RATIO * (b - a)
            if fc < best[1]: best = (c, fc)
        else:
            a = c; c = d; d = a + _GOLDEN_RATIO * (b - a)
            if fd < best[1]: best = (d, fd)

    alpha_mid = (a + b) / 2.0
    for delta in [0.01, 0.02, 0.05, 0.1]:
        for sign in [-1, 1]:
            candidate = alpha_mid + sign * delta
            if lo <= candidate <= hi:
                br = _brier_for_alpha(candidate, trades)
                if br < best[1]:
                    best = (candidate, br)
    return round(best[0], 4), round(best[1], 6)


class BetaCalibrator:
    """Legacy Beta calibration (single-parameter power transform).

    Kept for comparison/fallback. IsotonicCalibrator supersedes this.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = state_dir or _STATE_DIR
        self._cal_path = self._state_dir / "beta-calibration.json"
        self._alpha_global: float = 1.0
        self._alpha_city: Dict[str, float] = {}
        self._brier_before: Optional[float] = None
        self._brier_after: Optional[float] = None
        self._n_trades: int = 0
        self._fitted: bool = False

    @property
    def alpha(self) -> float:
        return self._alpha_global

    @property
    def fitted(self) -> bool:
        return self._fitted

    def calibrate(self, p: float, city: Optional[str] = None) -> float:
        if not self._fitted:
            return p
        alpha = self._alpha_global
        if city and city.upper() in self._alpha_city:
            city_alpha = self._alpha_city[city.upper()]
            if abs(city_alpha - 1.0) > 0.05:
                alpha = city_alpha
        return _beta_calibrate(p, alpha)

    def fit(
        self,
        brier_path: Optional[Path] = None,
        min_trades: int = 10,
        min_city_trades: int = 20,
    ) -> "BetaCalibrator":
        trades = _load_settled_trades(brier_path)
        self._n_trades = len(trades)
        if len(trades) < min_trades:
            self._alpha_global = 1.0
            self._brier_before = _brier_for_alpha(1.0, trades) if trades else None
            self._brier_after = self._brier_before
            self._fitted = True
            return self

        self._brier_before = _brier_for_alpha(1.0, trades)
        alpha_opt, brier_opt = _find_optimal_alpha(trades)
        self._alpha_global = alpha_opt
        self._brier_after = brier_opt

        city_trades: Dict[str, list] = {}
        for t in trades:
            city = _extract_city(t.get("ticker", ""))
            if city:
                city_trades.setdefault(city, []).append(t)
        for city, ctrades in city_trades.items():
            if len(ctrades) >= min_city_trades:
                alpha_c, _ = _find_optimal_alpha(ctrades)
                self._alpha_city[city] = alpha_c

        self._fitted = True
        return self

    def save(self, path: Optional[Path] = None) -> None:
        path = path or self._cal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "alpha_global": self._alpha_global,
            "alpha_city": self._alpha_city,
            "brier_before": self._brier_before,
            "brier_after": self._brier_after,
            "n_trades": self._n_trades,
            "fitted": self._fitted,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.rename(path)

    def load(self, path: Optional[Path] = None) -> bool:
        path = path or self._cal_path
        if not path.exists():
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            self._alpha_global = data.get("alpha_global", 1.0)
            self._alpha_city = data.get("alpha_city", {})
            self._brier_before = data.get("brier_before")
            self._brier_after = data.get("brier_after")
            self._n_trades = data.get("n_trades", 0)
            self._fitted = data.get("fitted", False)
            return True
        except (OSError, json.JSONDecodeError):
            return False

    def summary(self) -> dict:
        return {
            "method": "beta",
            "fitted": self._fitted,
            "n_trades": self._n_trades,
            "alpha_global": self._alpha_global,
            "brier_before": round(self._brier_before, 6) if self._brier_before else None,
            "brier_after": round(self._brier_after, 6) if self._brier_after else None,
            "brier_improvement_pct": (
                round((1 - self._brier_after / self._brier_before) * 100, 2)
                if self._brier_before and self._brier_after and self._brier_before > 0
                else None
            ),
            "n_cities": len(self._alpha_city),
            "city_alphas": dict(sorted(self._alpha_city.items())),
        }


# ══════════════════════════════════════════════════════════════════════
#  AUTO CALIBRATOR — selects best method
# ══════════════════════════════════════════════════════════════════════

class AutoCalibrator:
    """Try both Beta and Isotonic, pick the one with lower Brier score."""

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = state_dir or _STATE_DIR
        self._isotonic = IsotonicCalibrator(state_dir)
        self._beta = BetaCalibrator(state_dir)
        self._active: Optional[object] = None
        self._method: str = "none"
        self._fitted: bool = False
        self._oos: dict = {}

    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def method(self) -> str:
        return self._method

    def calibrate(self, p: float, city: Optional[str] = None) -> float:
        if not self._fitted or self._active is None:
            return p
        if isinstance(self._active, BetaCalibrator):
            return self._active.calibrate(p, city)
        return self._active.calibrate(p)

    def fit(self, brier_path: Optional[Path] = None, min_trades: int = 10) -> "AutoCalibrator":
        trades = _load_settled_trades(brier_path)
        if len(trades) < min_trades:
            self._fitted = True
            self._method = "identity"
            self._oos = {"selected": "identity", "reason": "insufficient_trades", "n": len(trades)}
            return self

        # FIX 2026-07-03 (weather-model QA): select on OUT-OF-SAMPLE (k-fold CV) Brier, with IDENTITY
        # a first-class candidate. The old code fit isotonic+beta and picked the lower IN-SAMPLE
        # Brier — but isotonic overfits a few hundred trades, so it deployed a curve that
        # bin/calibration_oos.py shows is WORSE than doing nothing out-of-sample. Now a calibrator is
        # deployed only if it beats identity's OOS Brier by >= _MIN_OOS_GAIN_REL; otherwise pricing
        # stays uncalibrated (honest raw probs). Data-driven: it turns back on automatically if a
        # calibrator ever genuinely generalizes as more trades accrue.
        # Always refresh the on-disk isotonic curve: trader/scanner.py loads state/calibration.json
        # directly for DOWN-ONLY position sizing (min(raw, calibrated), which can only SHRINK a bet) —
        # a risk lever independent of whether we APPLY calibration to the probability estimate below.
        # Keeping it current (and now monotonicity-repaired) avoids silently freezing scanner's curve
        # when the OOS gate declines to calibrate the probability.
        self._isotonic.fit(brier_path, min_trades)
        self._isotonic.save()

        identity_brier = _brier_score(trades, lambda x: x)

        def _fit_iso(tr):
            c = IsotonicCalibrator(self._state_dir)
            c.fit(trades=tr, min_trades=min_trades)
            return c.calibrate

        def _fit_beta(tr):
            alpha, _ = _find_optimal_alpha(tr)
            return lambda p: _beta_calibrate(p, alpha)

        iso_oos = _kfold_oos_brier(_fit_iso, trades)
        beta_oos = _kfold_oos_brier(_fit_beta, trades)
        self._oos = {
            "identity": round(identity_brier, 6),
            "isotonic_cv": round(iso_oos, 6) if iso_oos is not None else None,
            "beta_cv": round(beta_oos, 6) if beta_oos is not None else None,
            "n": len(trades),
        }

        cands = ([("isotonic", iso_oos)] if iso_oos is not None else []) + \
                ([("beta", beta_oos)] if beta_oos is not None else [])
        winner = min(cands, key=lambda c: c[1]) if cands else None
        threshold = identity_brier * (1.0 - _MIN_OOS_GAIN_REL)

        if winner is None or winner[1] > threshold:
            # Nothing robustly beats identity out-of-sample → price uncalibrated.
            self._active = None
            self._method = "identity"
            self._oos["selected"] = "identity"
            self._fitted = True
            return self

        # Winner generalizes → apply it. Isotonic was already fit+saved above; refit beta on demand.
        if winner[0] == "isotonic":
            self._active = self._isotonic
        else:
            self._beta.fit(brier_path, min_trades)
            self._beta.save()
            self._active = self._beta
        self._method = winner[0]
        self._oos["selected"] = winner[0]
        self._fitted = True
        return self

    def save(self, path: Optional[Path] = None) -> None:
        """Delegate save to the active calibrator."""
        if self._active is not None:
            self._active.save(path)

    def summary(self) -> dict:
        if self._active is not None:
            s = self._active.summary()
            s["selected_method"] = self._method
            s["oos"] = self._oos
            return s
        # Identity path: keep the SAME keys consumers rely on (build_fair_values reads n_trades /
        # brier_*). With identity applied, calibrated Brier == raw Brier, so before == after == the
        # identity Brier from the OOS pass. Omitting n_trades here crashed the fair-value build.
        return {"method": "identity", "selected_method": self._method,
                "fitted": self._fitted, "oos": self._oos,
                "n_trades": self._oos.get("n", 0),
                "brier_before": self._oos.get("identity"),
                "brier_after": self._oos.get("identity"),
                "brier_improvement_pct": 0.0 if self._oos.get("identity") is not None else None}


# ── CLI ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  ISOTONIC vs BETA CALIBRATION COMPARISON")
    print("=" * 60)

    # Fit isotonic
    iso = IsotonicCalibrator()
    iso.fit()
    iso_summary = iso.summary()
    iso.save()

    # Fit beta for comparison
    beta = BetaCalibrator()
    beta.fit()

    print(f"\n  Trades used: {iso_summary['n_trades']}")
    print(f"\n  ── Isotonic ──")
    print(f"  Buckets: {iso_summary['n_buckets']}")
    print(f"  Brier before: {iso_summary['brier_before']}")
    print(f"  Brier after:  {iso_summary['brier_after']}")
    imp = iso_summary.get("brier_improvement_pct")
    if imp is not None:
        print(f"  Improvement:  {imp:.1f}%")

    print(f"\n  ── Beta (alpha={beta.alpha}) ──")
    bs = beta.summary()
    print(f"  Brier before: {bs['brier_before']}")
    print(f"  Brier after:  {bs['brier_after']}")
    imp_b = bs.get("brier_improvement_pct")
    if imp_b is not None:
        print(f"  Improvement:  {imp_b:.1f}%")

    # Compare at key probability points
    print(f"\n  Calibration comparison at key points:")
    print(f"  {'Raw':>8s}  {'Isotonic':>10s}  {'Beta':>10s}  {'Actual':>10s}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*10}")
    targets = {0.04: 0.342, 0.16: 0.397, 0.24: 0.430, 0.35: None, 0.44: 0.512}
    for raw in sorted(targets.keys()):
        iso_p = iso.calibrate(raw)
        beta_p = beta.calibrate(raw)
        actual = targets[raw]
        actual_str = f"{actual:.4f}" if actual is not None else "—"
        print(f"  {raw:8.2f}  {iso_p:10.4f}  {beta_p:10.4f}  {actual_str:>10s}")

    # Show calibration curve
    print(f"\n  Isotonic calibration curve:")
    for p, y in iso._curve:
        print(f"    {p:.4f} → {y:.4f}")

    print(f"\n  Winner: {'Isotonic' if (iso_summary.get('brier_after') or 1) <= (bs.get('brier_after') or 2) else 'Beta'}")

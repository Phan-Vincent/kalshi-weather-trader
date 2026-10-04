#!/usr/bin/env python3
"""bin/calibration_oos.py — out-of-sample sizing of the isotonic calibration gain.

Quant review 2026-07-01 experiment #7. The deployed isotonic calibrator reports its Brier
improvement IN-SAMPLE (fit and evaluated on the same settled trades — §2.1 [MEDIUM]: "no held-out
/ CV split"). Isotonic regression is flexible and WILL overfit on a few hundred trades, so the
in-sample gain overstates what calibration actually buys on future markets. This tool sizes that
optimism with two honest, held-out estimators on the SAME settling brier log the calibrator fits:

  - TIME-FORWARD split: fit on the earliest `frac` of trades, evaluate calibrated Brier on the
    later held-out tail (the deployment-realistic "fit on past, apply to future").
  - k-FOLD CV: pooled held-out calibrated Brier across k folds (uses all data, lower variance).

It fits the REAL model.calibrate.IsotonicCalibrator (via its new trades= hook), so the number is
the deployed calibrator's own OOS gain, not a re-implementation. Read-only; writes nothing.

Verdict: OVERFIT if OOS improvement ≤ 0 (the in-sample gain is pure optimism); PARTIAL if positive
but below in-sample (reports the optimism gap in points); ROBUST if OOS ≈ in-sample.

Usage:  python3 bin/calibration_oos.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.calibrate import IsotonicCalibrator, _brier_score, _BRIER_LOG  # noqa: E402


def _load_timed(path: Path) -> list[dict]:
    """Settled (p, y, ticker, t) trades — mirrors model.calibrate._load_settled_trades' open+settled
    pairing and p/y extraction EXACTLY, but retains a settle-time key (settled_at_utc, else the open
    timestamp) so we can order a time-forward split."""
    if not path.exists():
        return []
    opens: dict = {}
    settled: list = []
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
            if rec.get("status") == "open":
                opens[rid] = rec
            elif rec.get("status") == "settled":
                settled.append(rec)
    out = []
    for s in settled:
        o = opens.get(s.get("record_id"))
        m = {**o, **s} if o else s
        if m.get("our_prob") is None or not m.get("outcome"):
            continue
        out.append({"p": float(m["our_prob"]),
                    "y": 1.0 if m.get("outcome") == "yes" else 0.0,
                    "ticker": m.get("ticker", ""),
                    "t": m.get("settled_at_utc") or m.get("timestamp_utc") or ""})
    return out


def _fit_eval(train: list[dict], test: list[dict], min_trades: int = 10):
    """Fit isotonic on `train`, return (identity_brier_on_test, calibrated_brier_on_test)."""
    cal = IsotonicCalibrator().fit(trades=train, min_trades=min_trades)
    before = _brier_score(test, lambda x: x)
    after = _brier_score(test, cal.calibrate)
    return before, after


def _improve(before, after):
    return (1.0 - after / before) * 100.0 if (before and before > 0) else None


def time_forward(trades: list[dict], frac: float = 0.7, min_trades: int = 10) -> dict:
    ordered = sorted(trades, key=lambda t: t["t"])
    cut = int(len(ordered) * frac)
    train, test = ordered[:cut], ordered[cut:]
    if len(train) < min_trades or not test:
        return {"n_train": len(train), "n_test": len(test), "improvement_pct": None}
    before, after = _fit_eval(train, test, min_trades)
    return {"n_train": len(train), "n_test": len(test),
            "brier_before": before, "brier_after": after, "improvement_pct": _improve(before, after)}


def kfold(trades: list[dict], k: int = 5, min_trades: int = 10, seed: int = 7) -> dict:
    if len(trades) < k * 2:
        return {"k": k, "n": len(trades), "improvement_pct": None}
    import random
    idx = list(range(len(trades)))
    random.Random(seed).shuffle(idx)
    folds = [[trades[i] for i in idx[f::k]] for f in range(k)]
    # POOLED held-out Brier: accumulate squared errors across every out-of-fold prediction, so each
    # trade is scored exactly once on a model that never saw it, then divide by N (lower variance
    # than averaging per-fold rates, and the natural analogue of the in-sample Brier).
    se_before = se_after = n = 0.0
    ok = 0
    for f in range(k):
        test = folds[f]
        train = [t for g in range(k) if g != f for t in folds[g]]
        if len(train) < min_trades or not test:
            continue
        cal = IsotonicCalibrator().fit(trades=train, min_trades=min_trades)
        for t in test:
            se_before += (t["p"] - t["y"]) ** 2
            se_after += (cal.calibrate(t["p"]) - t["y"]) ** 2
            n += 1
        ok += 1
    if not n:
        return {"k": k, "n": len(trades), "improvement_pct": None}
    before, after = se_before / n, se_after / n
    return {"k": k, "folds_used": ok, "n_scored": int(n),
            "brier_before": before, "brier_after": after, "improvement_pct": _improve(before, after)}


def in_sample(trades: list[dict], min_trades: int = 10) -> dict:
    if len(trades) < min_trades:
        return {"n": len(trades), "improvement_pct": None}
    cal = IsotonicCalibrator().fit(trades=trades, min_trades=min_trades)
    before = _brier_score(trades, lambda x: x)
    after = _brier_score(trades, cal.calibrate)
    return {"n": len(trades), "brier_before": before, "brier_after": after,
            "improvement_pct": _improve(before, after)}


def evaluate(trades: list[dict] = None, path: Path = None, frac: float = 0.7, k: int = 5) -> dict:
    if trades is None:
        trades = _load_timed(path or _BRIER_LOG)
    ins = in_sample(trades)
    tf = time_forward(trades, frac=frac)
    kf = kfold(trades, k=k)
    # Decisive OOS estimate: prefer k-fold (uses all data); fall back to time-forward.
    oos = kf.get("improvement_pct")
    oos_src = "k-fold"
    if oos is None:
        oos, oos_src = tf.get("improvement_pct"), "time-forward"
    ins_imp = ins.get("improvement_pct")
    if oos is None or ins_imp is None:
        verdict = "INSUFFICIENT DATA — not enough settled trades for a held-out split"
        optimism = None
    else:
        optimism = ins_imp - oos
        if oos <= 0:
            verdict = (f"OVERFIT — no out-of-sample gain ({oos_src} OOS {oos:+.1f}% ≤ 0); the "
                       f"in-sample {ins_imp:+.1f}% is optimism")
        elif oos >= ins_imp:
            # OOS meets or beats in-sample — no optimism (also the sign-safe path when in-sample≤0)
            verdict = f"ROBUST — OOS +{oos:.1f}% ≥ in-sample {ins_imp:+.1f}% (no optimism)"
        elif oos < ins_imp * 0.6:
            verdict = (f"PARTIAL — real but inflated in-sample (OOS +{oos:.1f}% vs in-sample "
                       f"+{ins_imp:.1f}%; optimism {optimism:+.1f} pts)")
        else:
            verdict = f"ROBUST — OOS +{oos:.1f}% ≈ in-sample +{ins_imp:.1f}% (optimism {optimism:+.1f} pts)"
    return {"n_trades": len(trades), "in_sample": ins, "time_forward": tf, "kfold": kf,
            "oos_improvement_pct": oos, "oos_source": oos_src, "optimism_pts": optimism,
            "verdict": verdict}


def _pct(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def _brow(tag, d):
    b, a = d.get("brier_before"), d.get("brier_after")
    bs = f"{b:.4f}→{a:.4f}" if (b is not None and a is not None) else "—"
    return f"  {tag:>13}: {bs}  improvement {_pct(d.get('improvement_pct'))}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frac", type=float, default=0.7, help="time-forward train fraction")
    ap.add_argument("--k", type=int, default=5, help="CV folds")
    args = ap.parse_args()
    r = evaluate(frac=args.frac, k=args.k)
    print(f"[calibration-oos] isotonic in-sample vs out-of-sample Brier gain "
          f"(source: {os.path.basename(str(_BRIER_LOG))}, n={r['n_trades']} settled trades)")
    print(_brow("in-sample", r["in_sample"]))
    tf = r["time_forward"]
    print(_brow(f"time-fwd {args.frac:g}", tf) +
          f"  (train={tf.get('n_train')}, test={tf.get('n_test')})")
    kf = r["kfold"]
    print(_brow(f"{args.k}-fold CV", kf) + f"  (n_scored={kf.get('n_scored')})")
    print(f"  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Regression: the learned per-city bias (city_bias_f) must reach the KDE price.

Weather-model QA 2026-07-03 headline finding: the production pricing path is the KDE
branch in estimate_market_prob (fires for every climatology city). It recentred the
historical distribution on the RAW ensemble mean `w_mean` and never added `city_bias_f`,
which was threaded only into the Gaussian fallback (USE_FORECAST=1 + KDE-unavailable).
So the entire error-model bias correction was inert on the primary path.

These tests pin the fix by exploiting the one property it creates: with the threshold
held fixed, the KDE likelihood is STRICTLY MONOTONE in city_bias_f (a warmer bias shifts
the recenter up → higher P(above), lower P(below)). Before the fix, city_bias_f had zero
effect and lik(+3) == lik(0) == lik(-3), so the strict-monotonicity assertion fails.

Also pins the learn/apply-loop invariant: model_temp_forecast (what settle_paper records
the error against) stays RAW — unchanged by city_bias_f — so the ErrorTracker EWMA keeps
representing actual−raw_forecast rather than self-cancelling.

Runs under plain python3 and pytest.
"""
import datetime as _dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from model.fair_value import estimate_market_prob  # noqa: E402
from model.prior import Climatology  # noqa: E402

CITY = "HOU"  # coastal, always in climatology; SURFACE_BIAS 0 so it can't muddy the signal
FUTURE_CLOSE = "2099-01-01T00:00:00+00:00"


def _future_date_mean_sigma():
    """A future-dated market (past the same-day leak guard) whose threshold sits at the
    climatological mean, with the bias swing SCALED to that day's climatological σ.

    The KDE is a discrete step function over 25 years of samples, so an absolute ±N°F swing
    moves proportionally fewer samples across the median on wide (winter) days than narrow
    (summer) days. Scaling the swing to σ makes the median-crossing gap calendar-INDEPENDENT
    (≈0.6-0.7 every season) — otherwise the magnitude assertion is summer-only flaky."""
    date_iso = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
    climo = Climatology()
    climo.load(str(ROOT / "state" / "climatology.json"))
    mmdd = date_iso[5:10]
    mean = climo.get_climo_mean(CITY, mmdd, "daily_high")
    spread = climo.get_climo_spread(CITY, mmdd, "daily_high")
    assert mean is not None and spread is not None, f"no climatology for {CITY} {mmdd}"
    sigma = max(spread / 2.5631, 1.5)  # interdecile range (P90-P10) → σ; floor for tight days
    return date_iso, round(mean), sigma


def _market(date_iso, threshold_f, bin_kind="above"):
    return {
        "ticker": f"KXHIGHHOU-{date_iso}-T{int(threshold_f)}",
        "market_type": "daily_high", "city_code": CITY, "date_iso": date_iso,
        "bin_kind": bin_kind, "threshold_f": float(threshold_f),
        "bin_low": None, "bin_high": None,
        "_raw_market": {"close_time": FUTURE_CLOSE},
    }


def _om(date_iso, temp_f):
    # Constant hourly temps → every member's daily-high (max over the local day) == temp_f,
    # so w_mean == temp_f regardless of station tz / hour-window bucketing. Two members.
    hours = [f"{date_iso}T{h:02d}:00:00+00:00" for h in range(24)]
    return {
        "source": "open_meteo_ensemble",  # required by _get_ensemble_member_forecasts
        "model_temps_f": {"om1": [float(temp_f)] * 24, "om2": [float(temp_f)] * 24},
        "utc_hours": hours,
        "spreads_f": [1.0] * 24,
    }


def _price(date_iso, threshold_f, bias_f, bin_kind="above"):
    saved = os.environ.get("KALSHI_WEATHER_USE_FORECAST")
    os.environ["KALSHI_WEATHER_USE_FORECAST"] = "1"  # keep the KDE (paper) result
    try:
        return estimate_market_prob(
            _market(date_iso, threshold_f, bin_kind),
            forecast_om=_om(date_iso, threshold_f),
            city_bias_f=bias_f,
        )
    finally:
        if saved is None:
            os.environ.pop("KALSHI_WEATHER_USE_FORECAST", None)
        else:
            os.environ["KALSHI_WEATHER_USE_FORECAST"] = saved


def _assert_kde(r):
    assert r["source"] == "ensemble", f"expected ensemble path, got {r['source']}"
    assert "method=KDE" in r["rationale"], f"KDE path not taken: {r['rationale']}"
    assert r["likelihood_prob"] is not None


def test_bias_shifts_kde_above_price_monotonically():
    date_iso, thr, sigma = _future_date_mean_sigma()
    lo = _price(date_iso, thr, -sigma)
    mid = _price(date_iso, thr, 0.0)
    hi = _price(date_iso, thr, +sigma)
    for r in (lo, mid, hi):
        _assert_kde(r)
    # Warmer city bias ⇒ strictly higher P(high > threshold). Pre-fix these were all equal.
    assert lo["likelihood_prob"] < mid["likelihood_prob"] < hi["likelihood_prob"], (
        lo["likelihood_prob"], mid["likelihood_prob"], hi["likelihood_prob"])
    # A ±1σ bias swing at the median moves the KDE by ~0.6-0.7 in EVERY season (measured
    # 0.56-0.80 across cities/months); 0.4 is a robust floor that still fails any near-no-op
    # regression (e.g. a bias divided by 100 → gap ~0.01).
    assert hi["likelihood_prob"] - lo["likelihood_prob"] > 0.4, (
        lo["likelihood_prob"], hi["likelihood_prob"])
    # fair_prob (after the bias-independent prior blend) moves the same direction.
    assert lo["fair_prob"] < hi["fair_prob"], (lo["fair_prob"], hi["fair_prob"])


def test_bias_shifts_kde_below_price_opposite_direction():
    date_iso, thr, sigma = _future_date_mean_sigma()
    lo = _price(date_iso, thr, -sigma, bin_kind="below")
    hi = _price(date_iso, thr, +sigma, bin_kind="below")
    _assert_kde(lo)
    _assert_kde(hi)
    # Warmer bias ⇒ LOWER P(high < threshold).
    assert hi["likelihood_prob"] < lo["likelihood_prob"], (
        lo["likelihood_prob"], hi["likelihood_prob"])


def test_recorded_forecast_stays_raw_for_learn_loop():
    # The value settle_paper records the error against must NOT absorb city_bias_f, or the
    # ErrorTracker EWMA would self-cancel instead of tracking actual−raw_forecast.
    date_iso, thr, sigma = _future_date_mean_sigma()
    temps = [_price(date_iso, thr, b)["model_temp_forecast"] for b in (-sigma, 0.0, +sigma)]
    assert temps[0] == temps[1] == temps[2], temps
    assert abs(temps[1] - thr) < 0.6, (temps[1], thr)  # ≈ raw ensemble mean (== threshold)


if __name__ == "__main__":
    test_bias_shifts_kde_above_price_monotonically()
    test_bias_shifts_kde_below_price_opposite_direction()
    test_recorded_forecast_stays_raw_for_learn_loop()
    print("OK — city_bias_f reaches the KDE price (above ↑, below ↓) and the recorded "
          "forecast stays raw")

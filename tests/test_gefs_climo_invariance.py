#!/usr/bin/env python3
"""Regression: GEFS must NOT reach the climatology (LIVE) fair_prob.

Context (2026-07-06 follow-up, reviews/followups-gefs-cron-2026-07-06.md):
A memory stub flagged a suspected "~0.5c GEFS leak" into the climatology-only LIVE price.
An isolated in-process A/B disproved a real numerical leak: with KALSHI_WEATHER_USE_FORECAST
OFF, injecting GEFS members that are +10F vs the Open-Meteo ensemble changes fair_prob by
EXACTLY 0 — the ensemble result (where GEFS lives) is discarded at fair_value.py:636
(`fair_prob = prior_prob`). The full-build "~0.5c drift" was a measurement artifact
(live-data refetch + GEFS-download latency between the two builds), not a pricing path.

These tests pin that invariant so a future edit can't silently let GEFS into the LIVE price,
and prove the invariance is NOT vacuous (GEFS DOES move the price when USE_FORECAST=1, so the
members are genuinely plumbed into the model — they are simply, correctly, discarded on LIVE).

Also unit-tests the opt-in build#1 GEFS-skip gate (_should_fetch_gefs), whose default is the
unchanged current behavior (always fetch).

Runs under plain python3 and pytest.
"""
import datetime as _dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from kalshi_weather.model.fair_value import estimate_market_prob  # noqa: E402
from kalshi_weather.model.prior import Climatology  # noqa: E402

FUTURE_CLOSE = "2099-01-01T00:00:00+00:00"
# Cities that are always in climatology; mix of coastal (bias 0) and inland.
CITIES = ["HOU", "NYC", "CHI", "MIA", "AUS", "DEN", "PHIL", "LAX"]


def _future_date():
    # +5d clears the same-day LEAK-1 guard, so USE_FORECAST=1 is genuinely effective.
    return (_dt.date.today() + _dt.timedelta(days=5)).isoformat()


def _market(city, date_iso, threshold_f, mtype="daily_high", bin_kind="above",
            bin_low=None, bin_high=None):
    tag = "HIGH" if mtype == "daily_high" else "LOW"
    return {
        "ticker": f"KX{tag}{city}-{date_iso}-T{int(threshold_f) if threshold_f is not None else 0}",
        "market_type": mtype, "city_code": city, "date_iso": date_iso,
        "bin_kind": bin_kind, "threshold_f": (float(threshold_f) if threshold_f is not None else None),
        "bin_low": bin_low, "bin_high": bin_high,
        "_raw_market": {"close_time": FUTURE_CLOSE},
    }


def _om(date_iso, temp_f):
    # Constant hourly temps -> every member's daily-high == temp_f regardless of tz bucketing.
    hours = [f"{date_iso}T{h:02d}:00:00+00:00" for h in range(24)]
    return {
        "source": "open_meteo_ensemble",
        "model_temps_f": {"om1": [float(temp_f)] * 24, "om2": [float(temp_f)] * 24},
        "utc_hours": hours,
        "spreads_f": [1.0] * 24,
    }


def _gefs(city, date_iso, temps, mtype="daily_high"):
    key = "daily_high" if mtype == "daily_high" else "daily_low"
    return {city.upper(): {"date": "20990101", "run": "00",
                           "by_date": {date_iso: {key: [float(t) for t in temps]}}}}


def _climo_mean(city, date_iso, mtype="daily_high"):
    climo = Climatology()
    climo.load(str(ROOT / "state" / "climatology.json"))
    return climo.get_climo_mean(city, date_iso[5:10], mtype)


def _price(city, date_iso, threshold_f, gefs_temps, mtype="daily_high", bin_kind="above",
           bin_low=None, bin_high=None, om_temp=None):
    mkt = _market(city, date_iso, threshold_f, mtype, bin_kind, bin_low, bin_high)
    om = _om(date_iso, om_temp if om_temp is not None else threshold_f)
    gefs = _gefs(city, date_iso, gefs_temps, mtype) if gefs_temps is not None else None
    return estimate_market_prob(mkt, forecast_om=om, forecast_gefs=gefs)


# ─────────────────────────── invariance (the headline) ───────────────────────────

def test_gefs_does_not_change_climatology_fair_prob():
    """USE_FORECAST off: GEFS members +10F vs OM must not move fair_prob at all."""
    os.environ.pop("KALSHI_WEATHER_USE_FORECAST", None)  # LIVE / climatology-only
    os.environ.pop("KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST", None)
    date_iso = _future_date()
    checked = 0
    for city in CITIES:
        mean = _climo_mean(city, date_iso)
        if mean is None:
            continue
        thr = round(mean)
        hot = [thr + 10, thr + 11, thr + 9, thr + 12, thr + 8]  # far above OM (== thr)
        for bin_kind, kw in (("above", {}), ("below", {}),
                             ("between", {"bin_low": thr - 2, "bin_high": thr + 2})):
            no_gefs = _price(city, date_iso, thr, None, bin_kind=bin_kind, **kw)
            with_gefs = _price(city, date_iso, thr, hot, bin_kind=bin_kind, **kw)
            assert no_gefs["fair_prob"] == with_gefs["fair_prob"], (
                city, bin_kind, no_gefs["fair_prob"], with_gefs["fair_prob"])
            assert no_gefs["prior_prob"] == with_gefs["prior_prob"], (city, bin_kind)
            assert no_gefs["likelihood_prob"] == with_gefs["likelihood_prob"], (city, bin_kind)
            checked += 1
    assert checked >= 12, f"too few climatology cities exercised ({checked})"


def test_invariance_is_not_vacuous_forecast_on_moves_price():
    """USE_FORECAST=1: the SAME +10F GEFS injection MUST move fair_prob, proving the members
    are genuinely plumbed into the model and the LIVE invariance above is a real discard."""
    saved = os.environ.get("KALSHI_WEATHER_USE_FORECAST")
    os.environ["KALSHI_WEATHER_USE_FORECAST"] = "1"
    os.environ.pop("KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST", None)
    try:
        date_iso = _future_date()
        moved = 0
        for city in CITIES:
            mean = _climo_mean(city, date_iso)
            if mean is None:
                continue
            thr = round(mean)
            hot = [thr + 10, thr + 11, thr + 9, thr + 12, thr + 8]
            no_gefs = _price(city, date_iso, thr, None, bin_kind="above")
            with_gefs = _price(city, date_iso, thr, hot, bin_kind="above")
            # Warmer GEFS members lift the ensemble mean -> higher P(high > threshold).
            if with_gefs["fair_prob"] > no_gefs["fair_prob"] + 1e-6:
                moved += 1
        assert moved >= 4, f"GEFS injection did not move the forecast price (moved={moved}) — " \
                           "invariance test would be vacuous"
    finally:
        if saved is None:
            os.environ.pop("KALSHI_WEATHER_USE_FORECAST", None)
        else:
            os.environ["KALSHI_WEATHER_USE_FORECAST"] = saved


# ─────────────────────────── the opt-in build#1 skip gate ───────────────────────────

def _should_fetch_gefs():
    import build_fair_values
    return build_fair_values._should_fetch_gefs()


def test_gate_default_fetches_always():
    """Default (both env unset) = unchanged current behavior = fetch."""
    os.environ.pop("KALSHI_WEATHER_GEFS_SKIP_CLIMO", None)
    os.environ.pop("KALSHI_WEATHER_USE_FORECAST", None)
    assert _should_fetch_gefs() is True


def test_gate_skips_only_on_climatology_build_when_opted_in():
    os.environ["KALSHI_WEATHER_GEFS_SKIP_CLIMO"] = "1"
    # climatology build (USE_FORECAST off) -> skip
    os.environ.pop("KALSHI_WEATHER_USE_FORECAST", None)
    assert _should_fetch_gefs() is False
    # forecast/paper build (USE_FORECAST on) -> STILL fetches (ensemble is used there)
    os.environ["KALSHI_WEATHER_USE_FORECAST"] = "1"
    assert _should_fetch_gefs() is True


def test_gate_opt_out_default_never_skips_forecast_build():
    os.environ.pop("KALSHI_WEATHER_GEFS_SKIP_CLIMO", None)
    os.environ["KALSHI_WEATHER_USE_FORECAST"] = "1"
    assert _should_fetch_gefs() is True


if __name__ == "__main__":
    test_gefs_does_not_change_climatology_fair_prob()
    test_invariance_is_not_vacuous_forecast_on_moves_price()
    test_gate_default_fetches_always()
    test_gate_skips_only_on_climatology_build_when_opted_in()
    test_gate_opt_out_default_never_skips_forecast_build()
    print("OK — GEFS is discarded on the climatology/LIVE path (invariant), genuinely plumbed "
          "under USE_FORECAST=1, and the build#1 skip gate defaults to unchanged behavior")

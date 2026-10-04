#!/usr/bin/env python3
"""Kalshi weather market fair-value probability model (v2).

Replaces the Gaussian-on-point-forecast hack with a Bayesian blend:
  1. Climatological prior (25-year daily max/min per city/day)
  2. StudentT likelihood from Open-Meteo ensemble member forecasts
  3. Posterior blend weighted by forecast horizon
  4. City-specific bias from error model (Phase 4)

Usage (same interface as v1 for backward compat):
    from kalshi_weather.model.fair_value import estimate_market_prob
    result = estimate_market_prob(market, nws_forecast, om_forecast)
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_weather.model.prior import Climatology
from kalshi_weather.model.posterior import (
    blend_posterior_above,
    blend_posterior_below,
    blend_posterior_between,
)
from kalshi_weather.model.likelihood import (
    ensemble_peaks,
    ensemble_lows,
    resolve_scale,
    ensemble_mean_std,
    ensemble_weighted_mean_std,
    GLOBAL_SCALE_FLOOR,
)

# 2026-06-20: the floor + market-blend below were band-aids for the BROKEN model
# (climatology-only, Brier 0.308) — they clamp/dilute toward the market. Now that the
# forecast-informed model works (Brier ~0.07 vs obs in bin/deprecated/backtest_real.py [QUARANTINED, USE_FORECAST=1 paper path]), those
# band-aids dilute a GOOD model. Env-tunable so the forecast/paper build can lighten
# them while the legacy/live build keeps the defaults (live unchanged).
GLOBAL_PROB_FLOOR = float(os.environ.get("KALSHI_WEATHER_PROB_FLOOR", "0.12"))  # no event priced below this

MARKET_BLEND_BASE = float(os.environ.get("KALSHI_WEATHER_MARKET_BLEND_BASE", "0.30"))  # min market weight
MARKET_BLEND_MAX = float(os.environ.get("KALSHI_WEATHER_MARKET_BLEND_MAX", "0.50"))   # max market weight (long horizon)


def _log(record: dict) -> None:
    print(json.dumps(record), file=sys.stderr, flush=True)


# ── Local-day bucketing ─────────────────────────────────────────────
# Kalshi daily high/low markets settle on the station's LOCAL calendar day.
# Selecting forecast hours by UTC date (iso.startswith(date_iso)) shifts the
# window 4-10h and bleeds into the adjacent local day (worst for western cities).
# _in_local_day converts each UTC hour to the station's tz and compares the local
# date. Falls back to the old UTC-prefix match when tz is unknown or conversion
# fails, so behaviour degrades safely.

_TZ_CACHE: Dict[str, Any] = {}


def _station_tzname(city_code: Optional[str]) -> Optional[str]:
    if not city_code:
        return None
    try:
        from kalshi_weather.data.weather_data import STATION_TZ
    except ImportError:
        try:
            from data.weather_data import STATION_TZ
        except ImportError:
            return None
    return STATION_TZ.get(city_code.upper())


def _in_local_day(iso: str, date_iso: str, tzname: Optional[str]) -> bool:
    """True if UTC timestamp `iso` falls on local calendar day `date_iso` in `tzname`.
    Falls back to UTC-prefix matching when tzname is None or tz data is unavailable."""
    if not tzname:
        return iso.startswith(date_iso)
    try:
        from zoneinfo import ZoneInfo
        if tzname not in _TZ_CACHE:
            _TZ_CACHE[tzname] = ZoneInfo(tzname)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_TZ_CACHE[tzname]).strftime("%Y-%m-%d") == date_iso
    except Exception:
        return iso.startswith(date_iso)


def _local_today_str(tzname: Optional[str], as_of: Optional[datetime] = None) -> str:
    """Current calendar date (YYYY-MM-DD) in the station-local tz; UTC fallback."""
    now = as_of or datetime.now(timezone.utc)
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            if tzname not in _TZ_CACHE:
                _TZ_CACHE[tzname] = ZoneInfo(tzname)
            return now.astimezone(_TZ_CACHE[tzname]).strftime("%Y-%m-%d")
        except Exception:
            pass
    return now.strftime("%Y-%m-%d")


# ── Globa lazily-loaded Climatology ────────────────────────────────

_CLIMO: Optional[Climatology] = None


def _get_climo() -> Climatology:
    """Lazy-load climatology once."""
    global _CLIMO
    if _CLIMO is None:
        _CLIMO = Climatology()
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo, "state", "climatology.json")
        if os.path.exists(path):
            _CLIMO.load(path)
            _log({"event": "climatology_loaded", "path": path,
                  "cities": len(_CLIMO.city_codes())})
        else:
            _log({"event": "climatology_not_found", "path": path,
                  "note": "Climatology unavailable; falling back to likelihood only"})
    return _CLIMO


# ── Helpers ─────────────────────────────────────────────────────────

def _hours_until_close(close_iso: Optional[str]) -> Optional[float]:
    """Hours until market close from now (UTC)."""
    if not close_iso:
        return None
    try:
        close = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (close - now).total_seconds() / 3600.0
    except Exception:
        return None


def _get_ensemble_member_forecasts(
    forecast_om: Optional[Dict[str, Any]],
    mtype: str,
    date_iso: str,
    tzname: Optional[str] = None,
) -> Tuple[List[float], float]:
    """Extract per-member forecasts for the day from Open-Meteo ensemble.

    For daily_high: each member contributes max(hourly_temps[day])
    For daily_low:  each member contributes min(hourly_temps[day])
    For hourly:     each member contributes temp at that hour

    `day` is the station's LOCAL calendar day when tzname is given (matching how
    Kalshi settles), else the UTC day.

    Returns (list_of_per_member_forecasts, ensemble_spread).
    """
    if not forecast_om or forecast_om.get("source") != "open_meteo_ensemble":
        return [], 0.0

    model_temps = forecast_om.get("model_temps_f", {})
    utc_hours = forecast_om.get("utc_hours", [])

    if not model_temps or not utc_hours:
        return [], 0.0

    # Get the spread_f values for the day to compute ensemble spread
    spreads = forecast_om.get("spreads_f", [])
    day_spreads = [
        spreads[i] for i, iso in enumerate(utc_hours)
        if _in_local_day(iso, date_iso, tzname) and i < len(spreads) and spreads[i] is not None
    ]
    avg_spread = float(sum(day_spreads)) / max(len(day_spreads), 1) if day_spreads else 0.0

    # For each model, extract the relevant forecast for the day
    member_forecasts = []
    for model_name, hourly_temps in model_temps.items():
        if not hourly_temps:
            continue

        # Get temps for the day
        day_temps = [
            hourly_temps[i] for i, iso in enumerate(utc_hours)
            if _in_local_day(iso, date_iso, tzname) and i < len(hourly_temps) and hourly_temps[i] is not None
        ]

        if not day_temps:
            continue

        if mtype == "daily_high":
            member_forecasts.append(max(day_temps))
        elif mtype == "daily_low":
            member_forecasts.append(min(day_temps))
        else:
            # hourly_temp — use midpoint of the day's range as rough estimate
            member_forecasts.append((max(day_temps) + min(day_temps)) / 2.0)

    return member_forecasts, avg_spread


def _get_nbm_daily_value(
    forecast_nws: Optional[Dict[str, Any]],
    mtype: str,
    date_iso: str,
    tzname: Optional[str] = None,
) -> Optional[float]:
    """Extract NWS NBM daily max/min for a specific date.

    NWS NBM provides hourly temps. We compute daily max/min from them,
    since the gridpoint endpoint may not include maxTemperature/minTemperature
    directly (varies by WFO).

    Returns temperature in °F, or None if unavailable.

    FIX 2026-06-15: NBM daily extremes are professionally calibrated
    and more accurate than our 3-member Open-Meteo ensemble mean.
    Using them as the primary point forecast should improve discrimination.
    """
    if not forecast_nws:
        return None
    nws_temps = forecast_nws.get("temps_f", [])
    utc_hours = forecast_nws.get("utc_hours", [])
    if not nws_temps or not utc_hours:
        return None

    day_temps = [
        nws_temps[i] for i, iso in enumerate(utc_hours)
        if _in_local_day(iso, date_iso, tzname) and i < len(nws_temps) and nws_temps[i] is not None
    ]
    if not day_temps:
        return None

    if mtype == "daily_high":
        return max(day_temps)
    elif mtype == "daily_low":
        return min(day_temps)
    else:
        return sum(day_temps) / len(day_temps)


def _confidence_label(
    hours_to_close: Optional[float],
    ensemble_size: int,
    scale: float,
    prior_weight: float,
) -> str:
    """Assign confidence label: high / med / low."""
    if hours_to_close is None:
        return "low"
    if hours_to_close <= 48 and ensemble_size >= 2 and scale <= 3.0 and prior_weight <= 0.4:
        return "high"
    if hours_to_close <= 72 and ensemble_size >= 1:
        return "med"
    return "low"


# ── Main estimator ──────────────────────────────────────────────────

def estimate_market_prob(
    market: Dict[str, Any],
    forecast_nws: Optional[Dict[str, Any]] = None,
    forecast_om: Optional[Dict[str, Any]] = None,
    forecast_gefs: Optional[Dict[str, Any]] = None,
    city_bias_f: float = 0.0,
    city_error_std_f: Optional[float] = None,
    market_mid: Optional[float] = None,
) -> Dict[str, Any]:
    # FIX 2026-06-18: market_mid added for model↔market blend (#1 priority fix)
    """Estimate fair probability for a single Kalshi weather market.

    Parameters
    ----------
    market : dict
        Must contain: ticker, market_type, city_code, date_iso, threshold_f,
        bin_kind, bin_low, bin_high. May also have _raw_market with close_time.
    forecast_nws : dict or None
        NWS NBM forecast (used as reference for temp display).
    forecast_om : dict or None
        Open-Meteo ensemble forecast (used for likelihood).
    city_bias_f : float
        Bias adjustment from error model (+ = add to forecast).
    city_error_std_f : float or None
        Empirical error std from error model (overrides spread-based scale).

    Returns
    -------
    dict with keys:
      ticker, fair_prob, source, confidence, model_temp_forecast,
      model_temp_std, rationale, asof_utc, prior_weight,
      prior_prob, likelihood_prob, ensemble_forecasts, ensemble_spread
    """
    ticker = market.get("ticker", "UNKNOWN")
    mtype = market["market_type"]
    city_code = market["city_code"]
    date_iso = market["date_iso"]
    bin_kind = market["bin_kind"]
    threshold_f = market["threshold_f"]
    bin_low = market["bin_low"]
    bin_high = market["bin_high"]

    close_time = market.get("_raw_market", {}).get("close_time")
    hours_to_close = _hours_until_close(close_time)

    # Station-local timezone, for bucketing forecast hours by the local day Kalshi
    # settles on (see _in_local_day). None → falls back to UTC-day bucketing.
    tzname = _station_tzname(city_code)

    # Derive MM-DD from date_iso (YYYY-MM-DD)
    mmdd = date_iso[5:10] if len(date_iso) >= 10 else None

    # ── LEAK-1 guard (2026-06-29 audit) ──────────────────────────────
    # The forecast/ensemble path takes max/min over the resolving LOCAL day's
    # Open-Meteo/NWS hours. For a SAME-DAY (or past) market those hours are already
    # ELAPSED → Open-Meteo returns ≈the observed temps, so max/min ≈ the actual high/low
    # = look-ahead. ~50% of forecast records were same-day, all 'ensemble'-sourced, which
    # inflated the model-edge arms' forward P&L (the "+73%" main arm). When set, route
    # same-day markets through the climatology-only path (identical to LIVE) so no elapsed
    # data reaches the fair value. Future-dated markets are unaffected (all their hours are
    # genuine forecast). Research override: KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST=1.
    _leak_blocked = (
        os.environ.get("KALSHI_WEATHER_USE_FORECAST", "0") == "1"
        and os.environ.get("KALSHI_WEATHER_ALLOW_SAMEDAY_FORECAST", "0") != "1"
        and date_iso <= _local_today_str(tzname)
    )

    # ── 1. Climatological prior ──────────────────────────────────────
    climo = _get_climo()
    prior_prob: Optional[float] = None
    # Clamp to [0,1]: it is a convex-combination weight. A stale/past close_time
    # (negative hours_to_close) could otherwise return an out-of-range weight that
    # makes the blend below explode (observed fair_prob ~600).
    prior_weight = max(0.0, min(1.0, Climatology.prior_weight(hours_to_close)))

    if climo.is_loaded() and mmdd and climo.has_city(city_code):
        if mtype in ("daily_high", "daily_low"):
            if bin_kind == "above":
                prior_prob = climo.exceedance_prob(city_code, mmdd, mtype, threshold_f)
            elif bin_kind == "below":
                prior_prob = climo.below_prob(city_code, mmdd, mtype, threshold_f)
            elif bin_kind == "between":
                prior_prob = climo.between_prob(city_code, mmdd, mtype, bin_low, bin_high)
    else:
        prior_weight = 0.0  # no prior available

    # ── 1b. Persistence prior blend (Phase 2: weather-state signal) ──
    # FIX 2026-06-15: climatology alone gives the same prior every year
    # for the same calendar date. Persistence adds a weather-state-
    # dependent signal: if yesterday was hot, today is more likely hot.
    # This improves discrimination by making the prior responsive to
    # actual weather conditions.
    persistence_applied = False
    # Persistence A/B gate (quant review 2026-07-01 experiment #8): default ON (no behavior change).
    # Set KALSHI_WEATHER_USE_PERSISTENCE=0 to run a persistence-OFF variant arm so compare_variants
    # can test whether the persistence prior is net-beneficial once its same-day leak (LEAK-2) is closed.
    if (prior_prob is not None and mtype in ("daily_high", "daily_low")
            and os.environ.get("KALSHI_WEATHER_USE_PERSISTENCE", "1") == "1"):
        try:
            from kalshi_weather.model.persistence import (
                fetch_yesterday_temp, persistence_exceedance_prob,
                persistence_below_prob, persistence_between_prob,
                persistence_weight as persist_weight_fn, yesterday_local_date,
            )
            from kalshi_weather.data.weather_data import STATIONS
            station_meta = STATIONS.get(city_code.upper())
            if station_meta:
                # LEAK-2 fix (quant review 2026-07-01): "yesterday" must be the STATION-LOCAL
                # yesterday, not UTC's — in the 00-06Z window UTC-yesterday IS the resolution
                # day for US stations, so the old code blended the market day's own observed
                # temp into the prior (same-day look-ahead OUTSIDE the LEAK-1 guard).
                _ydate = yesterday_local_date(tzname)
                if date_iso and _ydate >= date_iso:
                    # Belt-and-braces: never feed an observation from the market's own (or a
                    # later) day — covers same-day/past-dated markets on any tz edge.
                    _log({"event": "persistence_skipped_sameday_guard",
                          "ticker": market.get("ticker"), "yesterday": _ydate,
                          "date_iso": date_iso})
                    yesterday_f = None
                else:
                    yesterday_f = fetch_yesterday_temp(
                        station_meta["lat"], station_meta["lon"], mtype, date_str=_ydate
                    )
                if yesterday_f is not None:
                    if bin_kind == "above":
                        persist_p = persistence_exceedance_prob(
                            threshold_f, yesterday_f, mtype
                        )
                    elif bin_kind == "below":
                        persist_p = persistence_below_prob(
                            threshold_f, yesterday_f, mtype
                        )
                    elif bin_kind == "between":
                        persist_p = persistence_between_prob(
                            bin_low, bin_high, yesterday_f, mtype
                        )
                    else:
                        persist_p = None

                    if persist_p is not None:
                        # Clamp to [0,1]; negative hours_to_close otherwise yields an
                        # out-of-range weight that blows up prior_prob.
                        p_weight = max(0.0, min(1.0, persist_weight_fn(hours_to_close)))
                        prior_prob = p_weight * persist_p + (1.0 - p_weight) * prior_prob
                        persistence_applied = True
        except Exception as _pe:
            # Persistence is best-effort: degrade to climatology, but LOG the failure —
            # the silent pass hid a systematic outage from monitoring (QA silent-failure class).
            _log({"event": "persistence_degraded", "ticker": market.get("ticker"),
                  "error": f"{type(_pe).__name__}: {_pe}"})

    # ── 2. Ensemble likelihood ───────────────────────────────────────
    member_forecasts, ensemble_spread = _get_ensemble_member_forecasts(
        forecast_om, mtype, date_iso, tzname=tzname
    )

    # ── 2a. Inject GEFS ensemble members — ONLY the market's LOCAL day (audit #1/#3) ──
    if forecast_gefs:
        gefs_city_data = forecast_gefs.get(city_code.upper(), {})
        gefs_day = gefs_city_data.get('by_date', {}).get(date_iso, {})
        gefs_members = gefs_day.get('daily_high' if mtype == 'daily_high' else 'daily_low', [])
        if gefs_members:
            member_forecasts.extend(gefs_members)
            _log({"event": "gefs_injected", "city": city_code, "date": date_iso,
                  "gefs_members": len(gefs_members),
                  "gefs_run": f"{gefs_city_data.get('date','?')}/{gefs_city_data.get('run','?')}",
                  "total": len(member_forecasts)})

    error_std = city_error_std_f if city_error_std_f is not None else 2.0
    # Scale resolved later (line ~404) with clim_spread from climatology

    # ── 2b. Ensemble dressing: expand with calibrated pseudo-members ──
    if member_forecasts and len(member_forecasts) >= 3:
        try:
            from kalshi_weather.model.ensemble_dresser import get_dresser
            dresser = get_dresser()
            if dresser.can_dress(city_code):
                dressed = dresser.dress(city_code, member_forecasts)
                if dressed:
                    member_forecasts.extend(dressed)
                    _log({"event": "ensemble_dressed", "city": city_code,
                          "real": len(member_forecasts)-len(dressed),
                          "dressed": len(dressed), "total": len(member_forecasts)})
        except Exception:
            pass  # Dressing is optional; degrade gracefully

    # ── 2c. Compute summary stats from the (possibly dressed) ensemble
    # This is the key improvement over v1: each member contributes
    # a plausible daily peak, not a blended hourly mean
    if mtype == "daily_high" and not member_forecasts:
        # Fallback: use NWS daily max if no ensemble
        if forecast_nws:
            nws_temps = forecast_nws.get("temps_f", [])
            utc_hours = forecast_nws.get("utc_hours", [])
            day_temps = [t for i, t in enumerate(nws_temps)
                        if i < len(utc_hours) and _in_local_day(utc_hours[i], date_iso, tzname)]
            if day_temps:
                member_forecasts = [max(day_temps)]
        if not member_forecasts:
            if bin_kind == "above":
                member_forecasts = [threshold_f + 2.0]  # rough midpoint
            elif bin_kind == "below":
                member_forecasts = [threshold_f - 2.0]

    elif mtype == "daily_low" and not member_forecasts:
        if forecast_nws:
            nws_temps = forecast_nws.get("temps_f", [])
            utc_hours = forecast_nws.get("utc_hours", [])
            day_temps = [t for i, t in enumerate(nws_temps)
                        if i < len(utc_hours) and _in_local_day(utc_hours[i], date_iso, tzname)]
            if day_temps:
                member_forecasts = [min(day_temps)]
        if not member_forecasts:
            if bin_kind == "above":
                member_forecasts = [threshold_f + 2.0]
            elif bin_kind == "below":
                member_forecasts = [threshold_f - 2.0]

    # ── 2b. NBM daily forecast blend (Phase 2: per-city horizon-weighted) ──
    # FIX 2026-06-18 (#2 priority): Replace hardcoded horizon weights with
    # per-city optimal weights from error tracker. Cities where our forecasts
    # are accurate (low error_std) get higher NBM weight; cities where we
    # struggle (high error_std) lean more toward ensemble + market blend.
    # Formula: nbm_weight = clamp(0.50, 1.0 - error_std/10.0, 0.85)
    # Falls back to horizon-based weights for cold-start cities.
    nbm_daily_f: Optional[float] = _get_nbm_daily_value(forecast_nws, mtype, date_iso, tzname=tzname)
    if nbm_daily_f is not None and member_forecasts:
        ens_mean = sum(member_forecasts) / len(member_forecasts)
        # Per-city NBM weight from error model, with horizon adjustment
        if city_error_std_f is not None and city_error_std_f > 0:
            base_nbm = max(0.50, min(0.85, 1.0 - city_error_std_f / 10.0))
        else:
            base_nbm = 0.70  # default for cold-start
        # Horizon discount: reduce NBM weight at longer horizons
        if hours_to_close is not None:
            if hours_to_close <= 12:
                nbm_weight = base_nbm
            elif hours_to_close <= 24:
                nbm_weight = max(0.55, base_nbm - 0.05)
            elif hours_to_close <= 48:
                nbm_weight = max(0.50, base_nbm - 0.10)
            else:
                nbm_weight = max(0.50, base_nbm - 0.15)
        else:
            nbm_weight = base_nbm
        blended_forecast = nbm_weight * nbm_daily_f + (1.0 - nbm_weight) * ens_mean
        # Shift all ensemble members to center on the blended forecast
        # while preserving their relative spread for uncertainty estimation
        shift = blended_forecast - ens_mean
        member_forecasts = [f + shift for f in member_forecasts]
        _log({"event": "nbm_blend", "ticker": ticker,
              "nbm": round(nbm_daily_f, 1),
              "ens_mean": round(ens_mean, 1),
              "blended": round(blended_forecast, 1),
              "shift": round(shift, 1),
              "nbm_weight": round(nbm_weight, 2),
              "hours_to_close": round(hours_to_close, 1) if hours_to_close else None})

    # ── 2c. Climatological spread + weighted ensemble (#5, #6) ──
    # Get climatological spread for scale prior and KDE
    clim_spread = None
    climo_mean = None
    if climo.is_loaded() and mmdd and climo.has_city(city_code):
        clim_spread = climo.get_climo_spread(city_code, mmdd, mtype)
        climo_mean = climo.get_climo_mean(city_code, mmdd, mtype)

    # Pass clim_spread to resolve_scale for #6 (clim spread prior)
    scale = resolve_scale(ensemble_spread, error_std, hours_to_close,
                          ticker=ticker, clim_spread=clim_spread)
    # High/low differentiation after clim-aware scale
    if mtype == "daily_high":
        scale = max(GLOBAL_SCALE_FLOOR, scale - 0.5)
    elif mtype == "daily_low":
        scale = scale + 0.5

    # Weighted ensemble mean using NBM as reference (#5)
    nbm_daily_f = _get_nbm_daily_value(forecast_nws, mtype, date_iso, tzname=tzname)
    if member_forecasts:
        w_mean, w_std = ensemble_weighted_mean_std(
            member_forecasts, nbm_value=nbm_daily_f, error_std=error_std
        )
    else:
        w_mean, w_std = None, None

    # ── 3. Posterior blend / KDE ───────────────────────────────────
    # FIX 2026-06-18 (#4 priority): Use KDE from climatology when
    # available, falling back to Gaussian posterior blend when not.
    # The KDE preserves actual tail behavior from 25 years of history.
    fair_prob: Optional[float] = None
    likelihood_prob: Optional[float] = None

    # Prefer KDE over Gaussian when climatology is loaded for this city/date
    kd_used = False
    # 2026-06-20 model work: KDE anomaly shrink toward the forecast mean. The
    # shift-only KDE kept full climatological width; KALSHI_WEATHER_KDE_SHRINK<1
    # compresses it toward forecast-error width (keeps skew). Default 1.0 = original.
    _kde_shrink = float(os.environ.get("KALSHI_WEATHER_KDE_SHRINK", "1.0"))
    if (member_forecasts and climo.is_loaded() and mmdd
            and climo.has_city(city_code) and w_mean is not None):
        # FIX 2026-07-03 (weather-model QA): apply the learned per-city bias to the KDE
        # recenter location. city_bias_f was previously threaded ONLY into the Gaussian
        # fallback (blend_posterior_*, `f + city_bias_f`), so this primary KDE path priced
        # on the RAW ensemble mean and the entire error-model bias correction was inert for
        # every climatology city (i.e. every tradeable city). Same sign convention as the
        # Gaussian path (+ = forecast too cold → warm it). The bias is applied EXACTLY once,
        # here. The dresser adds spread-only synthetic members centered on the raw mean (fixed
        # 2026-07-06), so w_mean and model_temp_forecast stay ~raw and the settle_paper →
        # ErrorTracker loop keeps learning actual−raw_forecast, re-applied here at price time.
        kde_center = w_mean + city_bias_f
        if bin_kind == "above":
            likelihood_prob = climo.kde_exceedance_prob(
                city_code, mmdd, mtype, threshold_f, kde_center, shrink=_kde_shrink
            )
        elif bin_kind == "below":
            likelihood_prob = climo.kde_below_prob(
                city_code, mmdd, mtype, threshold_f, kde_center, shrink=_kde_shrink
            )
        elif bin_kind == "between":
            likelihood_prob = climo.kde_between_prob(
                city_code, mmdd, mtype, bin_low, bin_high, kde_center, shrink=_kde_shrink
            )
        if likelihood_prob is not None:
            kd_used = True
            # Blend KDE likelihood with climatological prior (same formula as posterior)
            if prior_prob is not None and prior_weight > 0:
                fair_prob = prior_weight * prior_prob + (1.0 - prior_weight) * likelihood_prob
            else:
                fair_prob = likelihood_prob

    # Fall back to Gaussian posterior blend when KDE unavailable — FORECAST/PAPER ONLY.
    # Audit 2026-06-25 (live-invariance): this Gaussian-ensemble fallback ran regardless of
    # USE_FORECAST, leaking the forecast ensemble (GEFS #1, error_std #7, dressed members #8)
    # into the LIVE arm for climatology-unavailable markets. Gated behind USE_FORECAST so LIVE
    # is truly climatology-only (its documented contract). Reversible: USE_FORECAST=1 (Stage 2).
    # _leak_blocked (same-day, see top of fn) forces the climatology-only branch below,
    # so the elapsed-hours ensemble never reaches fair_prob — same as USE_FORECAST off.
    _use_forecast = (os.environ.get("KALSHI_WEATHER_USE_FORECAST", "0") == "1") and not _leak_blocked
    if not kd_used and member_forecasts and _use_forecast:
        if bin_kind == "above":
            likelihood_prob = _ensemble_likelihood_above(
                threshold_f, member_forecasts, scale, city_bias_f, error_std
            )
            fair_prob = blend_posterior_above(
                threshold_f, prior_prob,
                member_forecasts, scale,
                hours_to_close, city_bias_f, error_std, ticker=ticker,
            )
        elif bin_kind == "below":
            likelihood_prob = _ensemble_likelihood_below(
                threshold_f, member_forecasts, scale, city_bias_f, error_std
            )
            fair_prob = blend_posterior_below(
                threshold_f, prior_prob,
                member_forecasts, scale,
                hours_to_close, city_bias_f, error_std, ticker=ticker,
            )
        elif bin_kind == "between":
            from kalshi_weather.model.likelihood import prob_between_studentt
            probs = [
                prob_between_studentt(bin_low, bin_high, f + city_bias_f, scale)
                for f in member_forecasts
            ]
            likelihood_prob = sum(probs) / len(probs)
            fair_prob = blend_posterior_between(
                bin_low, bin_high, prior_prob,
                member_forecasts, scale,
                hours_to_close, city_bias_f, error_std, ticker=ticker,
            )
    else:
        # Fires when the KDE path SUCCEEDED (kd_used=True) OR the Gaussian fallback was skipped
        # because USE_FORECAST is off (live). In both LIVE cases we take the climatology prior,
        # NOT the forecast-informed result — so LIVE prices on climatology alone (audit
        # 2026-06-25). Flag ON + kd_used keeps the forecast-informed KDE result (paper).
        if not (_use_forecast and kd_used):
            fair_prob = prior_prob
            likelihood_prob = prior_prob

    if not kd_used and not member_forecasts:
        # No forecast available — use only prior
        fair_prob = prior_prob
        likelihood_prob = prior_prob

    # ── 3b. Global probability floor ──────────────────────────────
    # 2026-06-18: Calibration shows model assigns 0-10% to events that
    # resolve YES 18-55% of the time. A 12% floor prevents the worst
    # calibration errors while still allowing differentiation below 50%.
    # This also acts as a price floor: contracts below 12¢ can't clear
    # fees + edge anyway.
    # Clamp to a valid probability: lower floor (calibration) AND a hard 1.0 ceiling.
    # The ceiling is a safety net — an out-of-range blend weight (e.g. a stale/past
    # close_time) could otherwise emit fair_prob >> 1, which scanner.py collapses to
    # 99¢ and reads as a near-certain YES (a dangerous false max-confidence signal).
    if fair_prob is not None:
        fair_prob = min(1.0, max(GLOBAL_PROB_FLOOR, fair_prob))
    if likelihood_prob is not None:
        likelihood_prob = min(1.0, max(GLOBAL_PROB_FLOOR, likelihood_prob))

    # ── 3c. Model↔Market blend ───────────────────────────────────
    # 2026-06-18 (#1 priority fix): Blend model probability with the
    # market mid-price. Market Brier=0.177 vs model Brier=0.308 — the
    # crowd aggregates information we miss (news, private weather data,
    # trader intuition). A 30-50% market weight closes ~half the gap.
    # Weight increases with horizon: at 6h, model is fine (NBM accurate);
    # at 96h, the market's implied probability is more reliable.
    market_blend_applied = False
    if market_mid is not None and fair_prob is not None and 0.0 < market_mid < 1.0:
        if hours_to_close is not None and hours_to_close > 0:
            # Linear ramp: 30% at 0h → 50% at 72h+
            blend_w = min(MARKET_BLEND_MAX,
                         MARKET_BLEND_BASE + 0.20 * min(1.0, hours_to_close / 72.0))
        else:
            blend_w = MARKET_BLEND_BASE
        fair_prob = (1.0 - blend_w) * fair_prob + blend_w * market_mid
        market_blend_applied = True

    # ── 4. Model temp display ──────────────────────────────────────
    model_temp_forecast: Optional[float] = None
    model_temp_std: Optional[float] = None
    if member_forecasts and not _leak_blocked:
        m_mean, m_std = ensemble_mean_std(member_forecasts)
        model_temp_forecast = round(m_mean, 2)
        model_temp_std = round(max(m_std, scale), 2)
        source = "ensemble"
    elif forecast_nws and not _leak_blocked:
        # Use NWS point forecast as fallback display
        nws_temps = forecast_nws.get("temps_f", [])
        utc_hours = forecast_nws.get("utc_hours", [])
        day_temps = [t for i, t in enumerate(nws_temps)
                    if i < len(utc_hours) and _in_local_day(utc_hours[i], date_iso, tzname)]
        if day_temps:
            if mtype == "daily_high":
                model_temp_forecast = round(max(day_temps), 2)
            elif mtype == "daily_low":
                model_temp_forecast = round(min(day_temps), 2)
            else:
                model_temp_forecast = round(sum(day_temps) / len(day_temps), 2)
        model_temp_std = round(scale, 2)
        source = "nws_only"
    else:
        source = "prior_only"

    # ── 5. Rationale ────────────────────────────────────────────────
    rationale_parts = []
    if prior_prob is not None and prior_weight > 0:
        rationale_parts.append(f"prior={prior_prob:.3f} (w={prior_weight:.2f})")
        if persistence_applied:
            rationale_parts[-1] += f"+persist"
    if likelihood_prob is not None:
        rationale_parts.append(f"likelihood={likelihood_prob:.3f}")
    if kd_used:
        rationale_parts.append("method=KDE")
    if member_forecasts:
        rationale_parts.append(f"ens_members={len(member_forecasts)}")
        rationale_parts.append(f"scale={scale:.1f}°F")
    if city_bias_f != 0:
        rationale_parts.append(f"bias={city_bias_f:+.1f}°F")
    if _leak_blocked:
        rationale_parts.append("sameday_climo(leak-guard)")
    rationale = " | ".join(rationale_parts) if rationale_parts else "no data"

    # ── 6. Confidence ──────────────────────────────────────────────
    confidence = _confidence_label(
        hours_to_close, len(member_forecasts), scale, prior_weight
    )

    # ── 6b. Append market blend to rationale ─────────────────────
    if market_blend_applied:
        rationale += f" | mkt_blend={blend_w:.0%}"

    result: Dict[str, Any] = {
        "ticker": ticker,
        "fair_prob": round(fair_prob, 4) if fair_prob is not None else None,
        "source": source,
        "confidence": confidence,
        "model_temp_forecast": model_temp_forecast,
        "model_temp_std": model_temp_std,
        "rationale": rationale,
        "asof_utc": datetime.now(timezone.utc).isoformat(),
        # Debug info
        "prior_weight": round(prior_weight, 3),
        "prior_prob": round(prior_prob, 4) if prior_prob is not None else None,
        "likelihood_prob": round(likelihood_prob, 4) if likelihood_prob is not None else None,
        "ensemble_forecasts": [round(f, 2) for f in member_forecasts],
        "ensemble_spread": round(ensemble_spread, 2),
        "scale_used": round(scale, 2),
        # Market blend metadata
        "market_blend_applied": market_blend_applied,
        "market_blend_weight": round(blend_w, 3) if market_blend_applied else 0.0,
    }
    return result


# ── Internal likelihood wrappers (avoids circular imports) ──────────

def _ensemble_likelihood_above(
    threshold_f, member_forecasts, scale, bias_f, error_std
) -> float:
    from kalshi_weather.model.likelihood import ensemble_likelihood_above
    return ensemble_likelihood_above(
        threshold_f, member_forecasts, scale,
        bias_f=bias_f, error_std_f=error_std,
    )


def _ensemble_likelihood_below(
    threshold_f, member_forecasts, scale, bias_f, error_std
) -> float:
    from kalshi_weather.model.likelihood import ensemble_likelihood_below
    return ensemble_likelihood_below(
        threshold_f, member_forecasts, scale,
        bias_f=bias_f, error_std_f=error_std,
    )


# ── Standalone smoke test ───────────────────────────────────────────

if __name__ == "__main__":
    print("=== Fair Value v2 Smoke Tests ===\n")

    # Simulate a PHX daily high market
    market_phx = {
        "ticker": "KXHIGHTPHX-26JUN08-B104.5",
        "market_type": "daily_high",
        "city_code": "PHX",
        "date_iso": "2026-06-08",
        "hour_utc": None,
        "threshold_f": 104.5,
        "bin_kind": "above",
        "bin_low": 103.5,
        "bin_high": 105.5,
        "_raw_market": {"close_time": "2026-06-08T20:00:00Z"},
    }

    # Simulate a SEA daily low market
    market_sea = {
        "ticker": "KXLOWTSEA-26JUN08-B50.5",
        "market_type": "daily_low",
        "city_code": "SEA",
        "date_iso": "2026-06-08",
        "hour_utc": None,
        "threshold_f": 50.5,
        "bin_kind": "above",
        "bin_low": 49.5,
        "bin_high": 51.5,
        "_raw_market": {"close_time": "2026-06-08T20:00:00Z"},
    }

    # Simulate ensemble data
    om_forecast = {
        "source": "open_meteo_ensemble",
        "utc_hours": [f"2026-06-08T{h:02d}:00:00Z" for h in range(24)],
        "model_temps_f": {
            "gfs_seamless": [97.0 + h * 0.5 for h in range(24)],
            "icon_seamless": [98.0 + h * 0.5 for h in range(24)],
            "ecmwf_ifs025": [96.0 + h * 0.5 for h in range(24)],
        },
        "spreads_f": [1.5 + (h / 24) for h in range(24)],
        "mean_temps_f": [97.0 + h * 0.5 for h in range(24)],
    }

    result = estimate_market_prob(market_phx, forecast_om=om_forecast)
    print(f"PHX daily_high > 104.5°F:")
    for k in ["fair_prob", "source", "confidence", "prior_prob",
              "likelihood_prob", "prior_weight", "ensemble_forecasts",
              "ensemble_spread", "scale_used", "rationale"]:
        print(f"  {k}: {result.get(k)}")
    print()

    # With bias
    result_b = estimate_market_prob(market_phx, forecast_om=om_forecast, city_bias_f=-1.0)
    print(f"PHX daily_high > 104.5°F (bias=-1.0°F):")
    print(f"  fair_prob: {result_b['fair_prob']:.4f} (vs {result['fair_prob']:.4f} unbias)")
    print(f"  rationale: {result_b['rationale']}")

    # SEA daily low
    om_sea = {
        "source": "open_meteo_ensemble",
        "utc_hours": [f"2026-06-08T{h:02d}:00:00Z" for h in range(24)],
        "model_temps_f": {
            "gfs_seamless": [55.0 - h * 0.3 for h in range(24)],
            "icon_seamless": [56.0 - h * 0.3 for h in range(24)],
            "ecmwf_ifs025": [54.0 - h * 0.3 for h in range(24)],
        },
        "spreads_f": [2.0 + (h / 24) for h in range(24)],
        "mean_temps_f": [55.0 - h * 0.3 for h in range(24)],
    }
    result_sea = estimate_market_prob(market_sea, forecast_om=om_sea)
    print(f"\nSEA daily_low > 50.5°F:")
    for k in ["fair_prob", "source", "confidence", "prior_prob",
              "likelihood_prob", "ensemble_forecasts", "rationale"]:
        print(f"  {k}: {result_sea.get(k)}")

    print("\nDone.")
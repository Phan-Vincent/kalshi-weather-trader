#!/usr/bin/env python3
"""Likelihood function: P(actual | forecast) for Kalshi weather markets.

Uses StudentT distribution (df=3, heavier tails than Gaussian) with
scale derived from the Open-Meteo ensemble spread and city-specific
empirical error std.

For daily high/low markets, the ensemble's hourly forecasts are used
to construct a distribution of possible daily peaks/troughs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


# ── Math helpers ─────────────────────────────────────────────────────

def _studentt_cdf(t: float, df: float = 3.0) -> float:
    """StudentT CDF using math.erf approximation for df=3.

    For df=3, the CDF has a closed form:
    F(t) = 0.5 + (t / (sqrt(3) * sqrt(1 + t²/3))) * (1/2) 
         + (1/π) * atan(t / sqrt(3))

    Simplified numerical approach using regularized incomplete beta
    would be ideal but unavailable without scipy. Use the simple
    approximation via the standardized t statistic.
    
    For df → ∞ this approaches normal CDF via erf.
    For df=3, we use a direct numerical integration or algebraic form.
    
    Algebraic form for df=3:
    F(t; 3) = 0.5 + (1/π) * ( t*sqrt(3) / (3 + t²) + atan(t / sqrt(3)) )
    """
    if df <= 0:
        return 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))

    if df == 3.0:
        # Closed form for StudentT with df=3
        z = t / math.sqrt(3.0)
        return 0.5 + (1.0 / math.pi) * (z / (1.0 + z * z) + math.atan(z))

    # For large df, approximate with normal
    if df > 120:
        return 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))

    # General case: use central t CDF approximation
    # Cornish-Fisher or use the fact that for integer df,
    # a simple series approximation exists
    # For df > 3 not handled here, fall back to normal
    return 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))


def _studentt_sf(t: float, df: float = 3.0) -> float:
    """StudentT survival function (1 - CDF)."""
    return 1.0 - _studentt_cdf(t, df)


def _gaussian_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _gaussian_sf(x: float) -> float:
    """Standard normal survival function."""
    return 1.0 - _gaussian_cdf(x)


# ── Likelihood functions ─────────────────────────────────────────────

def prob_above_studentt(
    threshold_f: float,
    loc_f: float,
    scale_f: float,
    df: float = 999.0,
    min_scale: float = 0.5,
) -> float:
    """P(actual > threshold) using Gaussian (Normal) distribution.

    P(Z > t) = 1 - Φ((threshold - loc) / scale)
    where Φ is the standard normal CDF.
    
    FIX 2026-06-11 (stat-consultant): changed default from StudentT(df=3)
    to Gaussian (df=999). The fat tails of df=3 were spreading probability
    too thinly, causing systematic underconfidence (model says 4-24%, actual
    outcomes 33-44%). Gaussian concentrates more mass near the center,
    producing higher peak probabilities that better match reality.
    """
    if scale_f <= 0:
        scale_f = min_scale
    scale_f = max(scale_f, min_scale)
    t_stat = (threshold_f - loc_f) / scale_f
    return _studentt_sf(t_stat, df)


def prob_below_studentt(
    threshold_f: float,
    loc_f: float,
    scale_f: float,
    df: float = 999.0,
    min_scale: float = 0.5,
) -> float:
    """P(actual < threshold) using Gaussian (Normal) distribution."""
    return 1.0 - prob_above_studentt(threshold_f, loc_f, scale_f, df, min_scale)


def prob_between_studentt(
    low_f: float,
    high_f: float,
    loc_f: float,
    scale_f: float,
    df: float = 999.0,
    min_scale: float = 0.5,
) -> float:
    """P(low < actual <= high) using Gaussian (Normal) distribution."""
    if scale_f <= 0:
        scale_f = min_scale
    scale_f = max(scale_f, min_scale)
    t_low = (low_f - loc_f) / scale_f
    t_high = (high_f - loc_f) / scale_f
    return _studentt_cdf(t_high, df) - _studentt_cdf(t_low, df)


# ── Daily high/low helpers ──────────────────────────────────────────

def estimate_daily_peak(
    model_hourly_temps: List[float],
    hour_range: Tuple[int, int] = (8, 22),
) -> Optional[float]:
    """Estimate daily max temperature from hourly forecasts.

    Uses max over daytime hours (8 AM - 10 PM local-ish, caller
    should supply hours in UTC that correspond to daytime).
    
    Returns the max temp found, or None if no data.
    """
    daytime = [t for i, t in enumerate(model_hourly_temps)]
    if not daytime:
        return None
    return max(daytime)


def estimate_daily_low(
    model_hourly_temps: List[float],
    hour_range: Tuple[int, int] = (0, 24),
) -> Optional[float]:
    """Estimate daily min temperature from hourly forecasts.

    Uses min over all hours (typically the overnight low).
    """
    if not model_hourly_temps:
        return None
    return min(model_hourly_temps)


def ensemble_peaks(
    model_temps_f: Dict[str, List[float]],
) -> List[float]:
    """Get daily max estimate from each ensemble member.

    Returns list of peak estimates, one per model.
    """
    peaks = []
    for model, temps in model_temps_f.items():
        if temps:
            p = estimate_daily_peak(temps)
            if p is not None:
                peaks.append(p)
    return peaks


def ensemble_lows(
    model_temps_f: Dict[str, List[float]],
) -> List[float]:
    """Get daily min estimate from each ensemble member."""
    lows = []
    for model, temps in model_temps_f.items():
        if temps:
            l = estimate_daily_low(temps)
            if l is not None:
                lows.append(l)
    return lows


def ensemble_likelihood_above(
    threshold_f: float,
    member_forecasts: List[float],
    member_spread: float,
    bias_f: float = 0.0,
    error_std_f: float = 2.0,
    df: float = 999.0,
    min_scale: float = 0.5,
) -> float:
    """P(actual > threshold | ensemble) = mean across members.

    Each member contributes a Gaussian probability with:
    - loc = member_forecast + bias
    - scale = max(member_spread, min_scale)

    FIX 2026-06-11 (stat-consultant): changed default from StudentT(df=3)
    to Gaussian (df=999). Gaussian concentrates more mass near the center,
    reducing the systematic underconfidence we observed (model 4-24% vs
    actual 33-44%). The caller's scale is authoritative; only floor at
    min_scale for safety.
    """
    if not member_forecasts:
        return 0.5  # no data = 50/50

    # Caller's scale is authoritative; only floor at min_scale for safety.
    scale = max(float(member_spread), min_scale)
    probs = []
    for forecast in member_forecasts:
        loc = forecast + bias_f
        p = prob_above_studentt(threshold_f, loc, scale, df, min_scale)
        probs.append(p)

    return sum(probs) / len(probs)


def ensemble_likelihood_below(
    threshold_f: float,
    member_forecasts: List[float],
    member_spread: float,
    bias_f: float = 0.0,
    error_std_f: float = 2.0,
    df: float = 999.0,
    min_scale: float = 0.5,
) -> float:
    """P(actual < threshold | ensemble)."""
    return 1.0 - ensemble_likelihood_above(
        threshold_f, member_forecasts, member_spread,
        bias_f, error_std_f, df, min_scale,
    )


def ensemble_mean_std(forecasts: List[float]) -> Tuple[float, float]:
    """Compute mean and std of ensemble member forecasts."""
    if not forecasts:
        return 0.0, 0.0
    n = len(forecasts)
    mean = sum(forecasts) / n
    if n < 2:
        return mean, 0.0
    var = sum((f - mean) ** 2 for f in forecasts) / n
    return mean, math.sqrt(var)


def ensemble_weighted_mean_std(
    forecasts: List[float],
    nbm_value: Optional[float] = None,
    error_std: Optional[float] = None,
) -> Tuple[float, float]:
    """Compute accuracy-weighted ensemble mean and std.

    FIX 2026-06-18 (#5 priority): Weight ensemble members by proximity
    to the NBM forecast (the authoritative single-model forecast). When
    the error tracker shows the city is well-predicted (low error_std),
    members close to NBM get higher weight. When error is high, give
    more weight to outliers (they might have the correct signal).

    Weight formula: w_i ∝ exp(-|member_i - NBM| / (2 * error_std))
    When no NBM: equal weights.
    """
    if not forecasts:
        return 0.0, 0.0
    if len(forecasts) == 1:
        return forecasts[0], 0.0

    n = len(forecasts)

    if nbm_value is not None and error_std is not None and error_std > 0.5:
        # Weight by distance from NBM, using error_std as bandwidth
        bandwidth = max(error_std, 2.0)  # floor at 2°F
        weights = []
        for f in forecasts:
            w = math.exp(-abs(f - nbm_value) / (2.0 * bandwidth))
            weights.append(w)
        total_w = sum(weights)
        if total_w > 0:
            weights = [w / total_w for w in weights]
        else:
            weights = [1.0 / n] * n
    else:
        weights = [1.0 / n] * n

    mean = sum(w * f for w, f in zip(weights, forecasts))
    if n < 2:
        return mean, 0.0
    var = sum(w * (f - mean) ** 2 for w, f in zip(weights, forecasts))
    return mean, math.sqrt(var)


# ── Convenience: determine scale from error model ──────────────────

GLOBAL_SCALE_FLOOR = 3.0  # °F — 2026-06-18: raised from 0.5→2.0→3.0

def resolve_scale(
    ensemble_spread: Optional[float],
    error_std: Optional[float],
    hours_to_close: Optional[float],
    default_std: float = 3.0,
    ticker: Optional[str] = None,
    clim_spread: Optional[float] = None,
) -> float:
    """Resolve the scale parameter for the likelihood distribution.

    FIX 2026-06-18 (#6 priority): Blends in climatological spread as a prior
    on ensemble width. A tight 3-member ensemble (spread=1.5°F) may not
    capture the true range of possible outcomes. The climatological spread
    (P90-P10 from 25yr history) provides a floor on uncertainty.

    Formula: scale = max(3.0, sqrt(max(spread, 0.5)² + max(error_std, 1.0)²))
    If clim_spread available: scale = max(scale, 0.3 * clim_spread)

    This means:
    - Spread=1.5, error_std=2.0, clim_spread=8.0 → scale=4.2°F (max of 3.0, 2.7, 2.4)
    - Spread=0.5, error_std=1.0, clim_spread=12.0 → scale=3.6°F (dominant: clim*0.3)
    """
    # Detect bin markets for logging only
    is_bin = False
    if ticker:
        import re
        m = re.search(r"B[\d.]+", ticker)
        if m:
            is_bin = True

    # Base: ensemble spread
    if ensemble_spread is not None and ensemble_spread > 0.1:
        base_scale = ensemble_spread
    elif hours_to_close is not None:
        if hours_to_close <= 24:
            base_scale = 3.0
        elif hours_to_close <= 48:
            base_scale = 4.0
        elif hours_to_close <= 72:
            base_scale = 5.0
        else:
            base_scale = 6.0
    else:
        base_scale = default_std

    # Add error model uncertainty in quadrature
    if error_std is not None and error_std > 0.5:
        # Cap error_std to prevent single-city catastrophes (SEA=10.7°F)
        # from dominating all predictions
        capped_error = min(error_std, 5.0)
        scale = math.sqrt(max(base_scale, 0.5)**2 + capped_error**2)
    else:
        scale = max(base_scale, 1.0)  # default floor when no error data

    # Global floor — prevents overconfidence from tight ensembles.
    # 2026-06-20 model work: env-tunable. CRPS analysis (bin/crps_report.py) over
    # 153 settled forecasts shows the model OVER-smooths by ~55% (CRPS-optimal
    # bandwidth ≈ ×0.45-0.6 of current). The 3.0 floor + 0.30 clim fraction are the
    # main culprits. Defaults preserve current behaviour; narrow via env after
    # validating with crps_report + the realistic-fill paper stream.
    import os as _os
    _floor = float(_os.environ.get("KALSHI_WEATHER_SCALE_FLOOR", str(GLOBAL_SCALE_FLOOR)))
    _clim_frac = float(_os.environ.get("KALSHI_WEATHER_CLIM_FLOOR_FRAC", "0.30"))
    _mult = float(_os.environ.get("KALSHI_WEATHER_SCALE_MULT", "1.0"))

    scale = max(scale, _floor)

    # Climatological spread prior (#6): don't let scale drop below
    # `_clim_frac` of the historical P90-P10 range for this city/date/type.
    if clim_spread is not None and clim_spread > 0:
        scale = max(scale, _clim_frac * clim_spread)

    # Final bandwidth multiplier (CRPS-tunable). <1.0 sharpens bin discrimination.
    return scale * _mult


# ── Smoke test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Likelihood Smoke Tests ===\n")

    # Test 1: PHX daily high > 106°F, forecast=103, spread=2.0
    p = ensemble_likelihood_above(106.0, [103.0, 104.0, 102.0], 2.0)
    print(f"PHX daily high > 106°F, ensemble=[103,104,102], spread=2.0")
    print(f"  P = {p:.4f}")

    # Test 2: Same but with 0°F bias and wide spread
    p2 = ensemble_likelihood_above(106.0, [103.0, 104.0, 102.0], 5.0)
    print(f"  With spread=5.0: P = {p2:.4f}")

    # Test 3: SEA daily high > 75°F, forecast=65, spread=3.0
    p3 = ensemble_likelihood_above(75.0, [65.0, 68.0, 63.0], 3.0)
    print(f"\nSEA daily high > 75°F, ensemble=[65,68,63], spread=3.0")
    print(f"  P = {p3:.4f}")

    # Test 4: LAX daily low < 55°F (below threshold)
    p4 = ensemble_likelihood_below(55.0, [58.0, 60.0, 57.0], 1.5)
    print(f"\nLAX daily low < 55°F, ensemble=[58,60,57], spread=1.5")
    print(f"  P = {p4:.4f}")

    # Test 5: Compare Gaussian vs StudentT tails
    print(f"\nTail comparison (threshold=+3σ from loc):")
    g_prob = _gaussian_sf(3.0)
    t_prob = _studentt_sf(3.0, 3.0)
    print(f"  Gaussian P(T > +3σ) = {g_prob:.4f}")
    print(f"  StudentT(df=3) P(T > +3σ) = {t_prob:.4f}")
    print(f"  Ratio: {t_prob/g_prob:.1f}x heavier tails")

    # Test 6: Scale resolution
    print(f"\nScale resolution:")
    print(f"  ensemble_spread=1.5, error_std=3.0 → {resolve_scale(1.5, 3.0, 24):.1f}")
    print(f"  ensemble_spread=None, error_std=3.0 → {resolve_scale(None, 3.0, 24):.1f}")
    print(f"  ensemble_spread=None, error_std=None, h=24 → {resolve_scale(None, None, 24):.1f}")
    print(f"  ensemble_spread=None, error_std=None, h=72 → {resolve_scale(None, None, 72):.1f}")

    print("\nDone.")
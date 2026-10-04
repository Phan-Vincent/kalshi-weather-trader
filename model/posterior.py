#!/usr/bin/env python3
"""Bayesian posterior: blend climatological prior with forecast likelihood.

P(actual > T | forecast, history) = 
    w_prior * P(actual > T | history) 
    + (1 - w_prior) * P(actual > T | ensemble forecast)

where w_prior depends on forecast horizon (shorter horizon = less prior weight).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


def blend_posterior(
    prior_prob: Optional[float],
    likelihood_prob: Optional[float],
    prior_weight: float = 0.3,
) -> Optional[float]:
    """Bayesian-style blend of prior and likelihood.

    Simple linear blend: posterior = w * prior + (1-w) * likelihood

    This is a pragmatic approximation to a full Bayesian update.
    A proper conjugate-prior update would require closed-form StudentT
    posteriors, which aren't analytically tractable. For weather markets,
    the linear blend with horizon-dependent weight works well because:
    1. Prior and likelihood are both on [0,1] probability space
    2. The weight encodes our relative trust in each source
    3. As horizon shrinks, likelihood dominates (good)
    4. As horizon grows, prior dominates (no overconfident long-range bets)
    """
    if prior_prob is None and likelihood_prob is None:
        return None
    if prior_prob is None:
        return likelihood_prob
    if likelihood_prob is None:
        return prior_prob

    return prior_weight * prior_prob + (1.0 - prior_weight) * likelihood_prob


def blend_posterior_above(
    threshold_f: float,
    prior_exceedance: Optional[float],
    ensemble_forecasts: List[float],
    ensemble_spread: float,
    hours_to_close: Optional[float] = None,
    bias_f: float = 0.0,
    error_std_f: float = 2.0,
    ticker: Optional[str] = None,
) -> Optional[float]:
    """P(actual > threshold) blended from prior + ensemble likelihood.

    High-level convenience: resolves prior weight from horizon,
    computes likelihood from ensemble, blends.
    """
    from kalshi_weather.model.prior import Climatology
    from kalshi_weather.model.likelihood import (
        ensemble_likelihood_above,
        resolve_scale,
    )

    # Resolve scale
    scale = resolve_scale(ensemble_spread, error_std_f, hours_to_close, ticker=ticker)

    # Compute likelihood
    likelihood = ensemble_likelihood_above(
        threshold_f, ensemble_forecasts, scale,
        bias_f=bias_f, error_std_f=error_std_f,
    )

    # Resolve prior weight from horizon
    prior_weight = Climatology.prior_weight(hours_to_close)

    # Blend
    return blend_posterior(prior_exceedance, likelihood, prior_weight)


def blend_posterior_below(
    threshold_f: float,
    prior_below: Optional[float],
    ensemble_forecasts: List[float],
    ensemble_spread: float,
    hours_to_close: Optional[float] = None,
    bias_f: float = 0.0,
    error_std_f: float = 2.0,
    ticker: Optional[str] = None,
) -> Optional[float]:
    """P(actual < threshold) blended."""
    exceed = blend_posterior_above(
        threshold_f, 
        1.0 - prior_below if prior_below is not None else None,
        ensemble_forecasts, ensemble_spread,
        hours_to_close, bias_f, error_std_f, ticker=ticker,
    )
    if exceed is None:
        return None
    return 1.0 - exceed


def blend_posterior_between(
    low_f: float,
    high_f: float,
    prior_between: Optional[float],
    ensemble_forecasts: List[float],
    ensemble_spread: float,
    hours_to_close: Optional[float] = None,
    bias_f: float = 0.0,
    error_std_f: float = 2.0,
    ticker: Optional[str] = None,
) -> Optional[float]:
    """P(low < actual <= high) blended."""
    from kalshi_weather.model.likelihood import (
        prob_between_studentt,
        resolve_scale,
    )
    from kalshi_weather.model.prior import Climatology

    # Likelihood for between
    scale = resolve_scale(ensemble_spread, error_std_f, hours_to_close, ticker=ticker)
    scale = max(scale, 0.5)

    # Mean likelihood across ensemble members
    if ensemble_forecasts:
        probs = []
        for fcast in ensemble_forecasts:
            p = prob_between_studentt(low_f, high_f, fcast + bias_f, scale)
            probs.append(p)
        likelihood = sum(probs) / len(probs)
    else:
        likelihood = None

    # Blend
    prior_weight = Climatology.prior_weight(hours_to_close)
    return blend_posterior(prior_between, likelihood, prior_weight)


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


# ── Smoke test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from kalshi_weather.model.prior import Climatology

    climo = Climatology()
    repo = os.path.dirname(os.path.dirname(__file__))
    climo.load(os.path.join(repo, "state", "climatology.json"))

    print("=== Posterior Smoke Tests ===\n")

    # Test 1: PHX Jun 8, daily high > 106°F
    city, mmdd, mtype = "PHX", "06-08", "daily_high"
    threshold = 106.0

    prior_p = climo.exceedance_prob(city, mmdd, mtype, threshold)
    print(f"Prior:     P(PHX Jun8 daily high > 106°F) = {prior_p:.4f}")

    # Ensemble says: [104, 105, 103], spread=2.0, 24h out
    posterior = blend_posterior_above(
        threshold, prior_p,
        ensemble_forecasts=[104.0, 105.0, 103.0],
        ensemble_spread=2.0,
        hours_to_close=24,
    )
    print(f"Posterior: P(PHX Jun8 daily high > 106°F | ens=[104,105,103]) = {posterior:.4f}")

    # Test 2: Same market, 72h out → prior dominated
    posterior_72 = blend_posterior_above(
        threshold, prior_p,
        ensemble_forecasts=[104.0, 105.0, 103.0],
        ensemble_spread=3.5,
        hours_to_close=72,
    )
    print(f"Posterior (72h): same = {posterior_72:.4f} (closer to prior {prior_p:.4f})")

    # Test 3: SEA daily high > 75°F
    prior_sea = climo.exceedance_prob("SEA", "06-08", "daily_high", 75.0)
    posterior_sea = blend_posterior_above(
        75.0, prior_sea,
        ensemble_forecasts=[68.0, 70.0, 66.0],
        ensemble_spread=3.0,
        hours_to_close=36,
    )
    print(f"\nSEA Jun8: Prior P(T>75°F)={prior_sea:.4f}, Posterior={posterior_sea:.4f}")

    # Test 4: With city bias (Phase 4 error model)
    posterior_bias = blend_posterior_above(
        75.0, prior_sea,
        ensemble_forecasts=[68.0, 70.0, 66.0],
        ensemble_spread=3.0,
        hours_to_close=36,
        bias_f=-2.0,  # model underestimates SEA temps → bias +2°F
    )
    print(f"With bias +2°F: Posterior={posterior_bias:.4f}")

    # Test 5: PHX daily high > 104.5 (current model says ~34%)
    prior_phx = climo.exceedance_prob("PHX", "06-08", "daily_high", 104.5)
    post_phx = blend_posterior_above(
        104.5, prior_phx,
        ensemble_forecasts=[103.0, 104.0, 102.0],
        ensemble_spread=1.5,
        hours_to_close=12,
    )
    print(f"\nPHX Jun8 >104.5°F: Prior={prior_phx:.4f}, Posterior={post_phx:.4f}")
    print(f"Old model (Gaussian std=1.2): ~0.34% (wildly wrong)")

    print("\nDone.")
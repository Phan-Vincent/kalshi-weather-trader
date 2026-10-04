#!/usr/bin/env python3
"""Climatological prior for Kalshi weather markets.

Loads the 25-year daily max/min climatology and provides:
- ECDF lookup P(actual > T | city, month_day, type)
- Smooth interpolation between discrete observations
- Blending factor for combining with forecast likelihood

Usage:
    from kalshi_weather.model.prior import Climatology

    climo = Climatology()
    climo.load("state/climatology.json")

    # P(actual > 106°F | PHX, June 8, daily_high)
    prob = climo.exceedance_prob("PHX", "06-08", "daily_high", 106.0)

    # P(actual < 55°F | SEA, Dec 25, daily_low)
    prob = climo.below_prob("SEA", "12-25", "daily_low", 55.0)

    # Prior weight based on forecast horizon
    weight = climo.prior_weight(hours_to_close=48)  # 0-1 scale
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class Climatology:
    """25-year climatological prior per city/day/type.

    Thread-safe after load(). Immutable once loaded.
    """

    def __init__(self):
        self._data: Dict[str, Dict[str, Dict[str, Dict]]] = {}
        self._loaded = False
        self._city_codes: List[str] = []

    def load(self, path: str | Path) -> None:
        """Load from a climatology.json file."""
        with open(path) as f:
            raw = json.load(f)

        # Provenance guard (2026-07-06 audit): the artifact must be built on the
        # station-local day boundary. A UTC-built artifact (pre-2026-07-03) biases
        # western-city priors by up to ~12°F on individual days while everyone
        # believes the tz fix is in effect. Surface it loudly instead of pricing
        # silently off a stale prior. Warn-only — never break the pricing path.
        boundary = (raw.get("metadata") or {}).get("day_boundary")
        if boundary != "station_local":
            msg = (
                f"climatology.json day_boundary={boundary!r} (expected 'station_local') "
                f"— artifact predates the 2026-07-03 timezone fix; rebuild with "
                f"`python3 bin/bootstrap_climatology.py --force`. Western-city priors biased."
            )
            import sys as _sys
            print(f"[climatology] ⚠️ {msg}", file=_sys.stderr)
            try:
                from trader.notify import alert
                alert(f"⚠️ {msg}", key="climatology_stale_tz", dedup_seconds=86400)
            except Exception:
                pass

        cities = raw["data"]
        self._data = {}
        for code, city_data in cities.items():
            self._data[code] = city_data["days"]
        self._city_codes = sorted(self._data.keys())
        self._loaded = True

    def is_loaded(self) -> bool:
        return self._loaded

    def city_codes(self) -> List[str]:
        return self._city_codes

    def has_city(self, city: str) -> bool:
        return city in self._data

    def _get_day(self, city: str, mmdd: str) -> Optional[Dict]:
        """Get the day entry for a city and MM-DD."""
        city_data = self._data.get(city)
        if city_data is None:
            # Try case-insensitive match
            for code in self._data:
                if code.upper() == city.upper():
                    city_data = self._data[code]
                    break
            if city_data is None:
                return None
        return city_data.get(mmdd)

    def exceedance_prob(
        self, city: str, mmdd: str, mtype: str, threshold_f: float
    ) -> Optional[float]:
        """P(actual > threshold_f) from climatology.

        mtype: 'daily_high' or 'daily_low'
        """
        day = self._get_day(city, mmdd)
        if day is None:
            return None

        stats = day.get(mtype, {})
        sorted_t = stats.get("sorted")
        n = stats.get("n", 0)

        if not sorted_t or n == 0:
            return None

        # Count exceedances
        count = sum(1 for t in sorted_t if t > threshold_f)
        return count / n

    def below_prob(
        self, city: str, mmdd: str, mtype: str, threshold_f: float
    ) -> Optional[float]:
        """P(actual < threshold_f) from climatology."""
        exceed = self.exceedance_prob(city, mmdd, mtype, threshold_f)
        if exceed is None:
            return None
        return 1.0 - exceed

    def between_prob(
        self, city: str, mmdd: str, mtype: str, low_f: float, high_f: float
    ) -> Optional[float]:
        """P(low_f < actual <= high_f) from climatology."""
        exceed_low = self.exceedance_prob(city, mmdd, mtype, low_f)
        exceed_high = self.exceedance_prob(city, mmdd, mtype, high_f)
        if exceed_low is None or exceed_high is None:
            return None
        return exceed_low - exceed_high

    def kde_exceedance_prob(
        self, city: str, mmdd: str, mtype: str, threshold_f: float,
        ensemble_mean_f: float, shrink: float = 1.0
    ) -> Optional[float]:
        """P(actual > threshold) using KDE-shifted historical distribution.

        FIX 2026-06-18 (#4 priority): Replaces Gaussian likelihood with the
        actual empirical distribution from 25 years of observations, shifted
        so its mean matches the ensemble forecast. This preserves the real
        tail behavior (heat waves, cold snaps) that a Gaussian can't capture.

        For each historical day's temperature t_i:
          shifted_i = t_i + (ensemble_mean - climo_mean)
          P = count(shifted_i > threshold) / n

        This is a simple kernel density estimator with a constant shift
        (location-only transformation, preserving the exact shape of the
        historical distribution).
        """
        day = self._get_day(city, mmdd)
        if day is None:
            return None

        stats = day.get(mtype, {})
        sorted_t = stats.get("sorted")
        n = stats.get("n", 0)
        climo_mean = stats.get("mean")

        if not sorted_t or n == 0 or climo_mean is None:
            return None

        # Shift the distribution to center on the ensemble forecast, and SHRINK
        # the historical anomalies toward that mean.
        # 2026-06-20 model work: shrink (default 1.0 = unchanged) compresses the
        # 25yr climatological spread toward the (much narrower) forecast-error
        # spread, while preserving the empirical SHAPE/skew. The shift-only KDE
        # priced bins at climatological width (~6-10°F) when a 12-24h forecast
        # collapses uncertainty to ~2°F — the dominant over-smoothing source per
        # bin/crps_report.py. shrink<1 sharpens; shrink=1 is the original behaviour.
        #   shifted_i = ensemble_mean + (t_i - climo_mean) * shrink
        count = sum(1 for t in sorted_t
                    if (ensemble_mean_f + (t - climo_mean) * shrink) > threshold_f)
        return count / n

    def kde_below_prob(
        self, city: str, mmdd: str, mtype: str, threshold_f: float,
        ensemble_mean_f: float, shrink: float = 1.0
    ) -> Optional[float]:
        """P(actual < threshold) using KDE-shifted distribution."""
        exceed = self.kde_exceedance_prob(city, mmdd, mtype, threshold_f, ensemble_mean_f, shrink)
        if exceed is None:
            return None
        return 1.0 - exceed

    def kde_between_prob(
        self, city: str, mmdd: str, mtype: str, low_f: float, high_f: float,
        ensemble_mean_f: float, shrink: float = 1.0
    ) -> Optional[float]:
        """P(low < actual <= high) using KDE-shifted distribution."""
        exceed_low = self.kde_exceedance_prob(city, mmdd, mtype, low_f, ensemble_mean_f, shrink)
        exceed_high = self.kde_exceedance_prob(city, mmdd, mtype, high_f, ensemble_mean_f, shrink)
        if exceed_low is None or exceed_high is None:
            return None
        return exceed_low - exceed_high

    def get_climo_spread(self, city: str, mmdd: str, mtype: str) -> Optional[float]:
        """Return the interdecile range (P90-P10) of historical observations.

        FIX 2026-06-18 (#6 priority): Used as a prior on ensemble scale.
        Even a tight ensemble should widen to at least climatological spread.
        """
        day = self._get_day(city, mmdd)
        if day is None:
            return None
        stats = day.get(mtype, {})
        sorted_t = stats.get("sorted")
        n = stats.get("n", 0)
        if not sorted_t or n < 10:
            return None
        p10_idx = int(n * 0.10)
        p90_idx = int(n * 0.90)
        return sorted_t[p90_idx] - sorted_t[p10_idx]

    def get_climo_mean(self, city: str, mmdd: str, mtype: str) -> Optional[float]:
        """Return the climatological mean for a city/date/type."""
        day = self._get_day(city, mmdd)
        if day is None:
            return None
        stats = day.get(mtype, {})
        return stats.get("mean")

    def summary(self, city: str, mmdd: str, mtype: str) -> Optional[Dict]:
        """Get full summary stats for diagnostic use."""
        day = self._get_day(city, mmdd)
        if day is None:
            return None
        return day.get(mtype)

    @staticmethod
    def prior_weight(hours_to_close: Optional[float] = None) -> float:
        """Weight to assign to climatological prior vs forecast likelihood.

        Returns 0-1. Higher = trust climatology more.
        
        FIX 2026-06-18: boosted significantly (now 0.20-0.35).
        Calibration analysis (n=81 settled) showed the model systematically
        underestimates event probabilities: when model says 10%, reality is
        55%; when model says 20%, reality is 44%. The ensemble of 3 members
        is too narrow and produces overconfident low probabilities. A higher
        prior weight anchors toward climatological baselines (which are
        well-calibrated from 25 years of data) and prevents extreme
        underestimation.
        
        - ~0.20 at 24h (was 0.05)
        - ~0.27 at 48h (was 0.10)
        - ~0.35 at 72h+ (was 0.18)
        """
        if hours_to_close is None:
            return 0.25
        h = hours_to_close
        weight = 0.20 + 0.15 / (1.0 + math.exp(-0.04 * (h - 60)))
        return round(weight, 4)

    @staticmethod
    def temp_at_percentile(
        city: str, mmdd: str, mtype: str, percentile: float
    ) -> Optional[float]:
        """Get temperature at a given percentile (0-100) from climatology.
        Not a static method in practice — needs the loaded data.
        This is a placeholder; call via summary().sorted for raw ECDF.
        """
        # Real implementation uses sorted array + linear interpolation
        raise NotImplementedError("Use summary().sorted directly")


# Quick smoke test
if __name__ == "__main__":
    climo = Climatology()
    repo = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(repo, "state", "climatology.json")
    climo.load(path)

    print("=== Quick Smoke Tests ===")
    tests = [
        ("PHX", "06-08", "daily_high", 106.0, "P(T>106)"),
        ("PHX", "06-08", "daily_high", 100.0, "P(T>100)"),
        ("SEA", "06-08", "daily_high", 75.0, "P(T>75)"),
        ("MIN", "12-25", "daily_low", 10.0, "P(T<10)"),
        ("LAX", "06-08", "daily_low", 60.0, "P(T>60)"),
        ("HOU", "06-08", "daily_high", 95.0, "P(T>95)"),
    ]

    for city, mmdd, mtype, thresh, label in tests:
        p = climo.exceedance_prob(city, mmdd, mtype, thresh)
        bl = climo.below_prob(city, mmdd, mtype, thresh)
        print(f"  {city} {mmdd} {mtype}: {label} = {p:.4f} | P(T<{thresh}) = {bl:.4f}")

    print(f"\n  Prior weights: 24h={Climatology.prior_weight(24)}, "
          f"48h={Climatology.prior_weight(48)}, "
          f"72h={Climatology.prior_weight(72)}, "
          f"120h={Climatology.prior_weight(120)}")
    print("  Done.")
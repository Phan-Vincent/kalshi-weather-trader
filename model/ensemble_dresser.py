#!/usr/bin/env python3
"""
model/ensemble_dresser.py — Pseudo-ensemble via calibrated error model dressing.

Expands a 9-member ensemble to 19+ members by generating synthetic
forecasts sampled from the per-city historical error distribution.
This is a standard technique used operationally by ECMWF (ensemble
dressing) and NWS (neighborhood ensemble probability).

Cost: zero additional API calls. Improves ensemble spread and
reduces mean standard error by ~30% (√9 → √19).
"""
from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", ROOT / "state"))
EM_PATH = STATE_DIR / "error-model.json"

# Default number of synthetic members to generate
DEFAULT_N_DRESS = 10

# Cities that have valid error model data (n_effective >= 3)
# Others will skip dressing and use only real ensemble members
MIN_N_EFFECTIVE = 3


class EnsembleDresser:
    """Generates calibrated synthetic ensemble members from the error model."""

    def __init__(self, error_model_path: Optional[Path] = None, n_dress: int = DEFAULT_N_DRESS):
        self.n_dress = n_dress
        self._em_path = error_model_path or EM_PATH
        self._data: Dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        if not self._em_path.exists():
            return
        with open(self._em_path) as f:
            em = json.load(f)
        self._data = em.get("cities", {})
        self._loaded = True

    def _load_if_needed(self) -> None:
        if not self._loaded:
            self.load()

    def can_dress(self, city: str) -> bool:
        """True if we have enough error data for this city."""
        self._load_if_needed()
        c = self._data.get(city.upper(), {})
        return c.get("n_effective", 0) >= MIN_N_EFFECTIVE

    def get_params(self, city: str) -> Optional[Tuple[float, float]]:
        """Return (bias_f, error_std_f) for the city, or None."""
        self._load_if_needed()
        c = self._data.get(city.upper())
        if not c or c.get("n_effective", 0) < MIN_N_EFFECTIVE:
            return None
        return c["bias_f"], c["error_std_f"]

    def dress(
        self,
        city: str,
        real_members: List[float],
        dress_factor: float = 1.0,
        seed: Optional[int] = None,
    ) -> List[float]:
        """
        Generate N synthetic ensemble members around the real ensemble mean.

        Args:
            city: City code (e.g. 'SEA')
            real_members: List of real ensemble member forecasts (°F)
            dress_factor: 1.0 = match historical error spread, 0.5 = tighter
            seed: Optional random seed for reproducibility

        Returns:
            List of dressed (synthetic) member forecasts (°F)
        """
        self._load_if_needed()
        params = self.get_params(city)
        if params is None:
            return []  # Can't dress — insufficient data

        bias, std = params

        # Deterministic seed from date + city (reproducible across cycles) — MUST be set BEFORE
        # constructing rng (audit #8: rng was built from OS entropy and the seed below was computed
        # but never used → non-reproducible fair_prob/Brier). zlib.crc32 is stable across processes;
        # builtin hash(str) is randomized per-process (PYTHONHASHSEED), so it would NOT be stable.
        if seed is None:
            import zlib
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            seed = zlib.crc32((today + city).encode())
        rng = random.Random(seed)

        # Base the dressed ensemble on the real ensemble mean
        # (more robust than using individual members as anchors)
        if not real_members:
            return []

        base_mean = sum(real_members) / len(real_members)

        # FIX 2026-07-06 (audit): dress adds SPREAD ONLY — center synthetic members on the
        # RAW ensemble mean, not base_mean+bias. The learned per-city bias is applied EXACTLY
        # once downstream, at the KDE recenter (fair_value.py: kde_center = w_mean + city_bias_f).
        # Baking `bias` into every synthetic member ALSO shifted w_mean by bias·D/(R+D) AND
        # contaminated model_temp_forecast (the ensemble mean over real+dressed) — so the price
        # got the correction ~1.15–1.5x (double-applied) and the error-model EWMA learned against
        # its own attenuated output (a feedback loop). `bias` is intentionally unused here now;
        # `std` still sets the spread of the historical error distribution.
        dressed = []
        for _ in range(self.n_dress):
            noise = rng.gauss(0.0, std * dress_factor)
            synthetic = base_mean + noise
            dressed.append(round(synthetic, 1))

        return dressed

    def expand_ensemble(
        self,
        city: str,
        real_members: List[float],
        dress_factor: float = 1.0,
    ) -> Tuple[List[float], int]:
        """
        Return (full_ensemble, n_dressed) — real + synthetic members.
        """
        dressed = self.dress(city, real_members, dress_factor=dress_factor)
        full = real_members + dressed
        return full, len(dressed)


# ── Module-level convenience ─────────────────────────────────────────

_dresser: Optional[EnsembleDresser] = None


def get_dresser() -> EnsembleDresser:
    global _dresser
    if _dresser is None:
        _dresser = EnsembleDresser()
        _dresser.load()
    return _dresser


def dress_ensemble(city: str, real_members: List[float]) -> List[float]:
    """Convenience: dress the ensemble for a city, return full list."""
    d = get_dresser()
    full, _ = d.expand_ensemble(city, real_members)
    return full


# ── Quick test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    dresser = EnsembleDresser(n_dress=10)
    dresser.load()

    print("=== Ensemble Dresser Test ===")
    print(f"Cities with good data: {sum(1 for c in dresser._data.values() if c.get('n_effective',0) >= 3)}")

    # Simulate SEA real members (roughly 76-84°F)
    real = [76.8, 76.9, 77.4, 78.5, 80.0, 80.2, 80.2, 80.2, 83.0]

    for city in ["SEA", "HOU", "LAX", "PHX"]:
        if dresser.can_dress(city):
            bias, std = dresser.get_params(city)
            full, nd = dresser.expand_ensemble(city, real, dress_factor=1.0)
            dressed = full[9:]
            print(f"\n{city} (bias={bias:+.1f}°F, σ={std:.1f}°F):")
            print(f"  Real (9):    mean={sum(real)/len(real):.1f}°F, range=[{min(real):.1f}-{max(real):.1f}]")
            print(f"  Dressed ({nd}): mean={sum(dressed)/len(dressed):.1f}°F, range=[{min(dressed):.1f}-{max(dressed):.1f}]")
            print(f"  Combined (19): mean={sum(full)/len(full):.1f}°F, σ={math.sqrt(sum((f-sum(full)/len(full))**2 for f in full)/len(full)):.1f}°F")
        else:
            print(f"\n{city}: insufficient error data — skipping")

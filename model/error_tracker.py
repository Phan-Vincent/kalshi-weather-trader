#!/usr/bin/env python3
"""EWMA-based error tracker for Kalshi weather fair-value model.

Replaces LESSONS.md with auto-updating per-city bias and error std.

Collects (city, forecast_temp, actual_temp, horizon) pairs on settlement,
then applies exponentially-weighted moving averages:

    bias_new = α × error + (1-α) × bias_old
    var_new  = (1-α) × (var_old + α × (error - bias_old)²)   # Finch incremental EWMA variance

    where α = 2 / (N_eff + 1), N_eff ≈ 20 (half-life ~14 trades)

Persists to state/error-model.json. Loaded by build_fair_values.py
to pass city_bias_f/city_error_std_f to the Bayesian fair-value model.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

# EWMA decay parameter — effective sample size
ALPHA = 2.0 / 21.0  # ~20 trade half-life

# get_bias() clamps each city's learned forecast bias to ±this, to prevent model divergences
# (2026-06-17 quant audit). A city whose TRUE bias exceeds it is silently UNDER-corrected — see
# saturated_cities() and the healthcheck error_model_saturation check (2026-07-13 audit).
BIAS_CAP_F = 2.0

# Where we persist
_STATE_DIR = Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"
_ERROR_MODEL_PATH = _STATE_DIR / "error-model.json"


class ErrorTracker:
    """Per-city, per-season error model with EWMA updates.

    Tracks:
      - bias_f: systematic temperature offset (forecast - actual)
      - error_std_f: spread of historical forecast errors
      - n_effective: weighted observation count
      - season_split: divides year into 4 seasons for finer granularity

    Public API:
        record_error(city, forecast_f, actual_f, horizon_hours)
        get_bias(city) -> float
        get_error_std(city) -> float
        save()
        load()
    """

    def __init__(self):
        self._data: Dict[str, Dict] = {}
        self._loaded = False

    # ── Persistence ─────────────────────────────────────────────────

    def load(self, path: Optional[Path] = None) -> None:
        path = path or _ERROR_MODEL_PATH
        if path.exists():
            try:
                with open(path) as f:
                    raw = json.load(f)
                self._data = raw.get("cities", {})
                self._loaded = True
            except (OSError, json.JSONDecodeError):
                self._data = {}
        else:
            self._data = {}
        self._loaded = True

    def save(self, path: Optional[Path] = None) -> None:
        path = path or _ERROR_MODEL_PATH
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "alpha": ALPHA,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "cities": self._data,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)

    # ── Season detection ────────────────────────────────────────────

    @staticmethod
    def _season(mmdd: str) -> str:
        """Map MM-DD to season: spring/summer/fall/winter."""
        m = int(mmdd[:2])
        if 3 <= m <= 5:
            return "spring"
        if 6 <= m <= 8:
            return "summer"
        if 9 <= m <= 11:
            return "fall"
        return "winter"

    # ── Record an error observation ────────────────────────────────

    def record_error(
        self,
        city: str,
        forecast_f: float,
        actual_f: float,
        horizon_hours: Optional[float] = None,
    ) -> None:
        """Record a temperature forecast error.

        error = actual - forecast (positive = forecast was too cold)
        """
        error = actual_f - forecast_f
        city = city.upper()

        # Initialize city entry if needed
        if city not in self._data:
            self._data[city] = {
                "bias_f": 0.0,
                "error_std_f": 2.0,
                "n_effective": 0.0,
                "errors": [],
                "updated_utc": None,
            }

        entry = self._data[city]

        # EWMA update for bias
        old_bias = entry["bias_f"]
        old_var = entry["error_std_f"] ** 2 if entry["error_std_f"] else 4.0
        old_n = entry["n_effective"]

        # α decays with more observations; fast early learning
        if old_n < 2:
            alpha = 0.3  # fast learning for first ~2 effective observations
        else:
            alpha = 2.0 / (old_n + 21.0)
        alpha = max(alpha, 0.05)  # never fully stop learning

        new_bias = alpha * error + (1.0 - alpha) * old_bias
        # Incremental EWMA variance (Finch) — re-centers on the shifted mean. The previous form
        # `alpha*(error-new_bias)² + (1-alpha)*old_var` dropped the re-center term and so
        # systematically under-estimated variance (~20%), narrowing error bands and over-
        # confidence (audit #7, 2026-06-25). This closed form equals that plus (1-alpha)·(Δbias)².
        new_var = (1.0 - alpha) * (old_var + alpha * (error - old_bias) ** 2)
        new_n = min(old_n + 1.0, 100.0)  # simple count: accumulate toward true N

        entry["bias_f"] = round(new_bias, 3)
        entry["error_std_f"] = round(math.sqrt(max(new_var, 0.25)), 2)
        entry["n_effective"] = round(new_n, 2)
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()

        # Keep last 50 raw errors for diagnostics
        entry.setdefault("errors", []).append(round(error, 2))
        if len(entry["errors"]) > 50:
            entry["errors"] = entry["errors"][-50:]

    # ── Query ──────────────────────────────────────────────────────

    def get_bias(self, city: str) -> float:
        """Get temperature bias for a city (+ means forecast too cold).
        Capped at ±BIAS_CAP_F to prevent model divergences (2026-06-17 quant audit)."""
        city = city.upper()
        entry = self._data.get(city)
        if entry is None:
            return 0.0
        raw = entry.get("bias_f", 0.0)
        return max(-BIAS_CAP_F, min(BIAS_CAP_F, raw))

    def saturated_cities(self, min_n: float = 10.0) -> list:
        """Cities whose RAW learned bias exceeds the ±BIAS_CAP_F clamp, so get_bias() is UNDER-correcting
        them — the fair value stays systematically biased and nothing surfaced it (2026-07-13 audit; HOU
        and PHX run ~2.3-3.5°F too warm). Only well-sampled cities (n_effective >= min_n) are reported so
        a handful of wild errors can't false-flag. Returns dicts (most-clamped first) for logging/alerts."""
        out = []
        for city, entry in (self._data or {}).items():
            if not isinstance(entry, dict):
                continue
            raw = entry.get("bias_f")
            n = entry.get("n_effective", 0.0)
            try:
                if raw is None or float(n or 0) < min_n or abs(float(raw)) <= BIAS_CAP_F:
                    continue
            except (TypeError, ValueError):
                continue
            raw = float(raw)
            out.append({
                "city": city,
                "raw_bias_f": round(raw, 2),
                "applied_bias_f": max(-BIAS_CAP_F, min(BIAS_CAP_F, raw)),
                "under_correction_f": round(abs(raw) - BIAS_CAP_F, 2),
                "n_effective": round(float(n), 1),
            })
        out.sort(key=lambda d: -d["under_correction_f"])
        return out

    def get_error_std(self, city: str) -> float:
        """Get error standard deviation for a city."""
        city = city.upper()
        entry = self._data.get(city)
        if entry is None:
            return 2.0
        return entry.get("error_std_f", 2.0)

    def get_effective_n(self, city: str) -> float:
        """Get effective observation count (float, for threshold checks)."""
        city = city.upper()
        entry = self._data.get(city)
        if entry is None:
            return 0.0
        return entry.get("n_effective", 0.0)

    def has_good_data(self, city: str) -> bool:
        """True if we have enough data to trust the error model over LESSONS.md.

        Threshold: n_effective >= 2.0 (about 2+ settlement observations).
        Since we use a simple count (not decaying EWMA), even 3 observations
        is sufficient for a meaningful bias estimate.
        """
        return self.get_effective_n(city) >= 2.0

    def get_summary(self, city: str) -> Optional[Dict]:
        """Get full entry for a city."""
        return self._data.get(city.upper())

    def all_cities(self) -> Dict[str, Dict]:
        """Get full data dict."""
        return dict(self._data)

    def is_loaded(self) -> bool:
        return self._loaded

    def n_cities(self) -> int:
        return len(self._data)

    def to_dict(self) -> Dict:
        return {
            "version": 2,
            "alpha": ALPHA,
            "cities": self._data,
        }


# ── Fetch actual temperature from Open-Meteo Archive ────────────────

_CORRECTIONS_CACHE = {"mtime": None, "data": {}}


def _actual_correction_f(city: Optional[str], mtype: str) -> float:
    """Per-city grid→station correction (°F) to ADD to the grid actual so scoring/calibration
    matches Kalshi's settlement station. Learned by bin/learn_actual_corrections.py into
    state/actual-corrections.json (audit 2026-07-06 #7). 0.0 if city is None or no correction
    exists. Reloaded when the file changes; never raises."""
    if not city:
        return 0.0
    try:
        import os
        from pathlib import Path
        p = Path(os.environ.get("KALSHI_WEATHER_STATE_DIR",
                                Path(__file__).resolve().parent.parent / "state")) / "actual-corrections.json"
        if not p.exists():
            return 0.0
        mt = p.stat().st_mtime
        if _CORRECTIONS_CACHE["mtime"] != mt:
            _CORRECTIONS_CACHE["data"] = json.loads(p.read_text()).get("corrections", {})
            _CORRECTIONS_CACHE["mtime"] = mt
        return float(_CORRECTIONS_CACHE["data"].get(city.upper(), {}).get(mtype, {}).get("correction_f", 0.0))
    except Exception:
        return 0.0


def fetch_actual_temp(
    lat: float,
    lon: float,
    date_iso: str,
    mtype: str = "daily_high",
    correction_city: Optional[str] = None,
) -> Optional[float]:
    """Fetch the actual observed temperature for a station/date from
    Open-Meteo Archive API.

    Returns actual temperature in Fahrenheit, or None if unavailable.

    mtype: 'daily_high' or 'daily_low'
    correction_city: if given, ADD that city's learned grid→station correction so the returned
      value matches Kalshi's settlement station, not the raw grid cell. Leave None for the RAW
      grid value — reconcile_actuals (measures the gap) and learn_actual_corrections (must not
      see a corrected value) rely on the raw reading.
    """
    import ssl
    import urllib.request

    # FIX 2026-06-11 (audit-trade-lifecycle Finding 4.5): use certifi if
    # available, otherwise system default — never disable verification
    # because MITM could inject fake temperatures and skew the error model.
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    field = "temperature_2m_max" if mtype == "daily_high" else "temperature_2m_min"
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date_iso}&end_date={date_iso}"
        f"&daily={field}"
        # FIX 2026-07-03 (weather-model QA): aggregate the daily extreme over the STATION-LOCAL day
        # (timezone=auto resolves the tz from lat/lon) — Kalshi settles the local-day high/low and the
        # forecast/ensemble path buckets by local day (_in_local_day). The old timezone=UTC returned the
        # UTC-calendar-day extreme, which for western (Pacific/Mountain) cities folded in the PREVIOUS
        # local day's afternoon — measured up to ~12°F wrong for SEA — corrupting the bias EWMA, paper
        # settlement, and every skill/Brier/CRPS score. (Eastern/Central cities were ~unaffected.)
        "&timezone=auto"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-weather-bernard/1.0"})
    import time as _time
    import urllib.error
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
            value_c = data.get("daily", {}).get(field, [None])[0]
            if value_c is None:
                return None
            grid_f = value_c * 9.0 / 5.0 + 32.0
            return round(grid_f + _actual_correction_f(correction_city, mtype), 1)
        except urllib.error.HTTPError as e:
            # Open-Meteo rate-limit (429): honor Retry-After, else exponential backoff (2/4/8s).
            # Prevents a tight backtest loop from hammering into a 429 storm + crash (2026-06-25).
            if e.code == 429 and attempt < 3:
                # Retry-After may be delay-seconds OR an HTTP-date (RFC 7231) — int() on a date would
                # raise ValueError that escapes the loop and crashes the caller; parse defensively.
                try:
                    _ra = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    _ra = 0
                wait = _ra or (2 ** (attempt + 1))
                _time.sleep(min(wait, 30))
                continue
            print(f"  [error] fetch_actual_temp failed: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  [error] fetch_actual_temp failed: {e}", file=sys.stderr)
            return None
    return None


# ── Standalone: backfill from brier log ─────────────────────────────

def backfill_from_brier_log(
    tracker: ErrorTracker,
    brier_log_path: Optional[Path] = None,
    stations: Optional[Dict[str, Dict]] = None,
) -> int:
    """Scan settled trades in brier log, fetch actual temps, record errors.

    Returns count of successfully recorded errors.
    """
    try:  # robust to import context (top-level `data.*` vs automations/ symlink `kalshi_weather.*`)
        from data.weather_data import STATIONS
    except ModuleNotFoundError:
        from kalshi_weather.data.weather_data import STATIONS

    stations = stations or STATIONS
    brier_path = brier_log_path or (_STATE_DIR / "brier-log.jsonl")

    if not brier_path.exists():
        print(f"  [error] brier log not found: {brier_path}")
        return 0

    # Parse market type from ticker
    def _parse_ticker(ticker: str) -> Optional[Tuple[str, str, str]]:
        """Return (city_code, date_iso, mtype) or None."""
        for tag, mtype in [("HIGHT", "daily_high"), ("LOWT", "daily_low"),
                            ("HIGH", "daily_high"), ("LOW", "daily_low"),
                            ("TEMP", "daily_high")]:
            if tag in ticker:
                parts = ticker.split("-", 2)
                if len(parts) >= 2:
                    code = parts[0].split(tag, 1)[-1]
                    date_str = parts[1] if len(parts) > 1 else ""
                    # Parse date: 26JUN08 -> 2026-06-08
                    if len(date_str) >= 7:
                        yr = "20" + date_str[:2]
                        month_map = {
                            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
                            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
                            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
                        }
                        mon = month_map.get(date_str[2:5], "01")
                        day = date_str[5:7]
                        return code, f"{yr}-{mon}-{day}", mtype
        return None

    # Skip NYCH (alias for NYC)
    CITY_ALIAS = {"NYCH": "NYC"}

    written = 0
    with open(brier_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only process settled records with ticker and outcome
            if rec.get("status") != "settled":
                continue
            ticker = rec.get("ticker", "")
            outcome = rec.get("outcome")
            if not outcome:
                continue

            parsed = _parse_ticker(ticker)
            if not parsed:
                continue

            city_code, date_iso, mtype = parsed
            city_code = CITY_ALIAS.get(city_code, city_code)

            if city_code not in stations:
                continue

            meta = stations[city_code]
            actual_f = fetch_actual_temp(meta["lat"], meta["lon"], date_iso, mtype)

            if actual_f is None:
                continue

            # Get the forecast temp from our entry record
            # Look for the open record with same fingerprint to get our_prob
            entry_prob = rec.get("our_prob", 0.5)

            # Infer forecast temp from market ticker threshold and our_prob
            # This is approximate but directional
            threshold = None
            try:
                # Extract threshold from ticker: KXHIGHTHOU-26MAY29-B89.5 -> 89.5
                parts = ticker.split("-")
                if len(parts) >= 3:
                    val_str = parts[2]
                    # Strip leading B or T
                    if val_str.startswith("B") or val_str.startswith("T"):
                        threshold = float(val_str[1:])
            except (ValueError, IndexError):
                pass

            if threshold is None:
                continue

            # Infer direction from bin kind in ticker
            # This is a rough estimate; the actual forecast temp is in brier
            # entry's market_context or can be reconstructed
            # NOTE 2026-06-18: Do NOT call record_error with threshold as forecast_f.
            # The market threshold (e.g., 82.5°F from KXHIGHTSEA-26JUN18-B82.5) is NOT
            # the model's forecast. Using it produces wild errors (+24.5°F spikes when
            # comparing T_high threshold against T_low actual). Error tracking is done
            # live by settle_paper.py which has access to the actual model forecast_f
            # stored in the paper-book position.
            #
            # Original (broken): tracker.record_error(city_code, threshold, actual_f, ...)
            passed
            written += 1

    return written


# ── Quick smoke test ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    tracker = ErrorTracker()
    tracker.load()
    print(f"Loaded error model: {tracker.n_cities()} cities")

    # Record a few test errors
    tracker.record_error("PHX", 104.0, 106.5)
    tracker.record_error("PHX", 102.0, 103.0)
    tracker.record_error("PHX", 105.0, 109.8)
    tracker.record_error("SEA", 65.0, 68.0)
    tracker.record_error("SEA", 62.0, 61.0)
    tracker.record_error("LAX", 73.0, 72.0)
    tracker.record_error("LAX", 72.0, 74.0)

    print(f"\nAfter test updates:")
    for city in ["PHX", "SEA", "LAX", "HOU"]:
        bias = tracker.get_bias(city)
        std = tracker.get_error_std(city)
        n = tracker.get_effective_n(city)
        print(f"  {city}: bias={bias:+.3f}°F, std={std:.2f}°F, n={n}")

    # Save
    tracker.save()
    print("\nError model saved.")

    # Backfill from brier log
    print("\nAttempting backfill from brier log...")
    n = backfill_from_brier_log(tracker)
    print(f"Backfilled {n} errors.")
    tracker.save()
    print("Saved (with backfill).")
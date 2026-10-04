"""Kalshi weather market data fetchers.

Sources:
  * NWS National Blend of Models (NBM) via api.weather.gov (gridpoints)
  * Open-Meteo ensemble (GFS, ICON, ECMWF)
"""

import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Ticker parsing (shared, single source of truth)
# Imported by trader.brier, model.calibrate, bin/sync_live_positions, bin/triangulate_balance
# so weather-scoping and per-city bucketing can never diverge between modules.
# ---------------------------------------------------------------------------
_MONTH_CODES = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
# Anchored on the full KX<type>[T]<CITY>-<YYMONDD>- structure so series that merely CONTAIN
# HIGH/LOW/TEMP (KXUSAIRANAGREEMENT, KXALLTIMEHIGH, KXFEDLOWER, KXHIGHESTGROSSINGMOVIE) are
# excluded. QA-10 (2026-07-01): validated to match all 3883 real weather tickers in state and
# exclude the 2 non-weather ones — replaces divergent substring/startswith matchers.
_WEATHER_TICKER_RE = re.compile(r"^KX(?:HIGH|LOW|TEMP)T?[A-Z]{2,4}-\d{2}[A-Z]{3}\d{2}-")


def is_weather_ticker(tk: str) -> bool:
    """True iff `tk` is a Kalshi HIGH/LOW/TEMP weather market (see _WEATHER_TICKER_RE)."""
    return bool(_WEATHER_TICKER_RE.match(tk or ""))


def extract_city(ticker: str) -> str:
    """City code (2-4 alpha) from a weather ticker, else ''. Handles both KXHIGHT<CITY> and
    bare KXHIGH<CITY> forms. QA-12 (2026-07-01): single source of truth for city bucketing —
    trader.brier previously omitted the bare-KXHIGH form and dropped NY/MIA/LAX from reports."""
    for tag in ("HIGHT", "LOWT", "HIGH", "LOW", "TEMP"):
        if tag in ticker:
            city = ticker.split("-", 1)[0].split(tag, 1)[-1]
            if city and city not in _MONTH_CODES and city.isalpha() and 2 <= len(city) <= 4:
                return city
    return ""


def append_jsonl_atomic(path, records) -> None:
    """Append dict record(s) to a JSONL file as ONE flock-guarded write, so concurrent appenders
    (e.g. a manual settle run racing the cron) can't interleave/tear a line — the settlement rows
    exceed macOS PIPE_BUF (512B), so a plain buffered append is not atomic. QA-17 (2026-07-01).
    Best-effort: an flock failure degrades to a plain append (never raises on the money path)."""
    import fcntl
    if isinstance(records, dict):
        records = [records]
    rows = [r for r in records if r is not None]
    if not rows:
        return
    blob = "".join(json.dumps(r) + "\n" for r in rows)
    p = str(path)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(blob)
            f.flush()
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Station catalogue
# ---------------------------------------------------------------------------

STATIONS: Dict[str, Dict[str, Any]] = {
    "HOU": {"station": "KHOU", "name": "Houston Hobby", "lat": 29.645, "lon": -95.279},
    "CHI": {"station": "KMDW", "name": "Chicago Midway", "lat": 41.786, "lon": -87.752},
    "NYC": {"station": "KNYC", "name": "New York Central Park", "lat": 40.779, "lon": -73.969},
    "NYCH": {"station": "KNYC", "name": "New York Central Park", "lat": 40.779, "lon": -73.969},
    "BOS": {"station": "KBOS", "name": "Boston Logan", "lat": 42.365, "lon": -71.011},
    "LAX": {"station": "KLAX", "name": "Los Angeles Intl", "lat": 33.942, "lon": -118.408},
    "MIA": {"station": "KMIA", "name": "Miami Intl", "lat": 25.795, "lon": -80.287},
    "AUS": {"station": "KAUS", "name": "Austin Bergstrom", "lat": 30.194, "lon": -97.670},
    "DEN": {"station": "KDEN", "name": "Denver Intl", "lat": 39.856, "lon": -104.673},
    # FIX 2026-07-06 (audit #8): Kalshi settles the DAL series on CLIDFW / Dallas-Fort Worth
    # (KDFW), NOT Love Field (KDAL) — confirmed in the market rules_secondary. The two airports
    # are ~15mi apart and settle several °F apart, so pricing on Love Field was a permanent
    # directional error the model could never learn away. Point it at the settlement station.
    "DAL": {"station": "KDFW", "name": "Dallas-Fort Worth", "lat": 32.8998, "lon": -97.0403},
    "PHX": {"station": "KPHX", "name": "Phoenix Sky Harbor", "lat": 33.434, "lon": -112.008},
    "PHIL": {"station": "KPHL", "name": "Philadelphia Intl", "lat": 39.872, "lon": -75.237},
    "ATL": {"station": "KATL", "name": "Atlanta Hartsfield", "lat": 33.637, "lon": -84.428},
    "SFO": {"station": "KSFO", "name": "San Francisco Intl", "lat": 37.621, "lon": -122.379},
    "NOLA": {"station": "KMSY", "name": "New Orleans Louis Armstrong", "lat": 29.993, "lon": -90.258},
    "OKC": {"station": "KOKC", "name": "Oklahoma City Will Rogers", "lat": 35.393, "lon": -97.601},
    "LV": {"station": "KLAS", "name": "Las Vegas Harry Reid", "lat": 36.084, "lon": -115.154},
    "SATX": {"station": "KSAT", "name": "San Antonio Intl", "lat": 29.534, "lon": -98.470},
    "MIN": {"station": "KMSP", "name": "Minneapolis St Paul", "lat": 44.884, "lon": -93.222},
    "DC": {"station": "KDCA", "name": "Washington Reagan National", "lat": 38.852, "lon": -77.037},
    "SEA": {"station": "KSEA", "name": "Seattle Tacoma", "lat": 47.450, "lon": -122.309},
}


# IANA timezone per city, for bucketing hourly forecasts by the station's LOCAL
# calendar day (Kalshi weather markets settle on the local-day high/low, not the
# UTC day). PHX is America/Phoenix (no DST). Residual nuance: the NWS climate day
# is technically local *standard* time; local civil time here is a close and large
# improvement over the previous UTC-day bucketing.
STATION_TZ: Dict[str, str] = {
    "HOU": "America/Chicago",
    "CHI": "America/Chicago",
    "NYC": "America/New_York",
    "NYCH": "America/New_York",
    "BOS": "America/New_York",
    "LAX": "America/Los_Angeles",
    "MIA": "America/New_York",
    "AUS": "America/Chicago",
    "DEN": "America/Denver",
    "DAL": "America/Chicago",
    "PHX": "America/Phoenix",
    "PHIL": "America/New_York",
    "ATL": "America/New_York",
    "SFO": "America/Los_Angeles",
    "NOLA": "America/Chicago",
    "OKC": "America/Chicago",
    "LV": "America/Los_Angeles",
    "SATX": "America/Chicago",
    "MIN": "America/Chicago",
    "DC": "America/New_York",
    "SEA": "America/Los_Angeles",
}


# ---------------------------------------------------------------------------
# Surface-type temperature bias adjustments
# ---------------------------------------------------------------------------

# Inland airports (asphalt/concrete heat islands) read 1-3°F higher than
# the grid-cell average used by ensemble models. Coastal airports are
# near-neutral (±0.5°F).
# These adjustments are applied to ensemble_mean_f before fair-value computation.
SURFACE_BIAS: Dict[str, float] = {
    # Inland — hot pavement bias
    "OKC": -1.5,   # Oklahoma City: large tarmac, heat island
    "DAL": -2.0,   # Dallas-Fort Worth (KDFW, the settlement station as of 2026-07-06); revisit — this bias was tuned for Love Field
    "SATX": -1.5,  # San Antonio: inland, paved
    "MIN": -1.5,   # Minneapolis: inland, asphalt-heavy
    "STL": -1.5,   # St. Louis: inland (not in station list but may appear)
    "ATL": -1.0,   # Atlanta: some tree canopy, still inland bias
    "AUS": -1.5,   # Austin: inland, paved
    "DEN": -1.0,   # Denver: high altitude, but still dry inland bias
    "PHX": -1.5,   # Phoenix: desert heat sink on tarmac
    "LV":  -1.5,   # Las Vegas: desert + pavement
    "CHI": -1.0,   # Chicago: urban heat island, but water moderates
    # Coastal — near-neutral
    "LAX": 0.0,
    "SEA": 0.0,
    "SFO": 0.0,
    "NYC": 0.0,
    "NYCH": 0.0,
    "BOS": 0.0,
    "MIA": 0.0,
    "NOLA": 0.0,
    "DC": 0.0,
    "PHIL": 0.0,
    "HOU": 0.0,   # Houston is near coast, humidity moderates
}


def apply_surface_bias(city_code: str, ensemble_mean_f: float) -> float:
    """Adjust ensemble mean temperature for surface-type bias.

    Returns the adjusted mean (bias is subtracted since airports read higher).
    """
    bias = SURFACE_BIAS.get(city_code.upper(), 0.0)
    return round(ensemble_mean_f + bias, 2)


def _log(record: dict) -> None:
    print(json.dumps(record), file=sys.stderr, flush=True)


def _get_ssl_context() -> ssl.SSLContext:
    """Create an SSL context using certifi (system CA fallback)."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return ctx


class _BodyReadTimeout(Exception):
    """Total body-read deadline exceeded (server trickling the response). Non-retryable in
    _http_json: retrying a trickling server just burns another deadline, and the caller's
    failure path (e.g. the Open-Meteo fail-cache/breaker) is what should engage instead."""


def _http_read_deadline_sec() -> float:
    try:
        return float(os.environ.get("KALSHI_WEATHER_HTTP_READ_DEADLINE_SEC", "60"))
    except Exception:
        return 60.0


def _read_body_bounded(resp, deadline_sec: float, url: str) -> bytes:
    """Read the full response body with a TOTAL wall-clock bound.

    2026-07-10 incident: urlopen(timeout=30) only bounds each socket OPERATION; resp.read()
    loops recv() until EOF, so a server that trickles bytes (each recv < 30s apart) holds the
    read open indefinitely — an Open-Meteo trickle hung fair-value builds 28-88 min, starved
    the 15:20/17:20/19:20 PT live slots behind the cycle lock, and (because the hung process
    was SIGKILLed by the lock reclaim before returning) the failure was never recorded, so the
    OM fail-cache/breaker never opened and every subsequent cycle re-hung.

    read1() does at most ONE underlying recv, so each loop iteration blocks at most the socket
    timeout; the deadline check between iterations bounds the whole body read by
    ~deadline_sec + socket-timeout. 0 (or a resp without read1) falls back to plain read()."""
    if deadline_sec <= 0:
        return resp.read()
    read1 = getattr(resp, "read1", None)
    if read1 is None:
        return resp.read()
    start = time.monotonic()
    chunks = []
    while True:
        if time.monotonic() - start > deadline_sec:
            try:
                resp.close()
            except Exception:
                pass
            raise _BodyReadTimeout(
                f"body read exceeded {deadline_sec:.0f}s total deadline (trickling response): {url}")
        chunk = read1(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _http_json(url: str, headers: Optional[Dict[str, str]] = None, retries: int = 3, timeout: float = 30.0) -> Any:
    """Fetch JSON via urllib with simple retry/back-off.

    `timeout` bounds each socket operation (connect/recv); the TOTAL body read is additionally
    bounded by KALSHI_WEATHER_HTTP_READ_DEADLINE_SEC (default 60, 0 disables) — see
    _read_body_bounded. A body-deadline trip is NOT retried."""
    default_headers = {
        "User-Agent": "kalshi-weather-trader/0.1",
        "Accept": "application/json",
    }
    if headers:
        default_headers.update(headers)

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=default_headers)
        try:
            ctx = _get_ssl_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = _read_body_bounded(resp, _http_read_deadline_sec(), url)
                return json.loads(raw.decode("utf-8"))
        except _BodyReadTimeout as e:
            # Trickling body: retrying burns another full deadline for a server in a known-bad
            # state — fail NOW so the caller records the failure (OM fail-cache/breaker) and the
            # build moves on instead of stalling the cycle (and the live slot behind the lock).
            _log({"event": "http_body_deadline", "url": url, "attempt": attempt, "error": str(e)})
            raise
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt < retries:
                wait = 2 ** attempt
                _log({"event": "http_retry", "url": url, "attempt": attempt, "status": e.code, "wait_sec": wait})
                time.sleep(wait)
                continue
            _log({"event": "http_error", "url": url, "status": e.code, "reason": str(e.reason)})
            raise
        except Exception as e:
            _log({"event": "http_exception", "url": url, "attempt": attempt, "error": str(e)})
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


# ---------------------------------------------------------------------------
# NWS NBM (National Blend of Models)
# ---------------------------------------------------------------------------

def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def fetch_nws_nbm(station: str) -> Dict[str, Any]:
    """Fetch NWS NBM forecast from api.weather.gov for a METAR station.

    Returns a dict with per-hour temperature in Fahrenheit.
    Keys:
      station, source, utc_hours (List[str]), temps_f (List[float]),
      std_f (Optional[List[float]]), updated_at_iso, raw.
    NWS NBM gridpoints endpoint returns hourly temperature values
    but does not expose ensemble spread directly.  We infer a very
    conservative spread from the min/max temp range when available.
    """
    _log({"event": "nws_fetch_start", "station": station})

    # Step 1: get station metadata to resolve lat/lon
    station_url = f"https://api.weather.gov/stations/{station.upper()}/observations/latest"
    obs = _http_json(station_url)
    if obs is None:
        raise RuntimeError(f"NWS station metadata failed for {station}")

    # FIX 2026-06-11 (audit-data-integrity Finding 2): `geometry` lives at the
    # ROOT of the NWS station response, not inside `properties`. Reading from
    # the wrong level was returning lat=lon=0 and silently falling back to
    # the STATIONS catalogue for everything.
    props = obs.get("properties", {})
    geometry = obs.get("geometry") or props.get("geometry", {})
    coords = geometry.get("coordinates", [None, None])
    lat = float(coords[1] or 0) if len(coords) >= 2 else 0
    lon = float(coords[0] or 0) if len(coords) >= 1 else 0

    if lat == 0 or lon == 0:
        # Fallback: try gridpoints from lat/lon if we have it in STATIONS
        for code, meta in STATIONS.items():
            if meta["station"] == station.upper():
                lat, lon = meta["lat"], meta["lon"]
                break

    # Step 2: get gridpoint forecast
    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    points = _http_json(points_url)
    grid = points.get("properties", {}).get("forecastGridData")
    if not grid:
        _log({"event": "nws_no_grid", "station": station, "lat": lat, "lon": lon})
        raise RuntimeError(f"No forecastGridData for {station}")

    grid_data = _http_json(grid)
    gprops = grid_data.get("properties", {})

    hourly_temps = gprops.get("temperature", {}).get("values", [])
    hourly_max = gprops.get("maxTemperature", {}).get("values", [])
    hourly_min = gprops.get("minTemperature", {}).get("values", [])

    if not hourly_temps:
        raise RuntimeError(f"No hourly temperature values for {station}")

    utc_hours: List[str] = []
    temps_f: List[float] = []
    std_f: List[float] = []

    for entry in hourly_temps:
        # NWS format: "2026-05-28T12:00:00+00:00/PT1H"
        valid_time = entry.get("validTime", "")
        match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})", valid_time)
        if not match:
            continue
        iso = match.group(1)
        temp_c = float(entry.get("value"))
        temp_f = round(_celsius_to_fahrenheit(temp_c), 2)
        utc_hours.append(iso)
        temps_f.append(temp_f)
        std_f.append(None)

    # Build a min/max lookup by date to infer conservative spread
    min_by_date: Dict[str, float] = {}
    max_by_date: Dict[str, float] = {}
    for entry in hourly_min:
        vt = entry.get("validTime", "")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", vt)
        if m:
            min_by_date[m.group(1)] = _celsius_to_fahrenheit(float(entry.get("value", 0)))
    for entry in hourly_max:
        vt = entry.get("validTime", "")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", vt)
        if m:
            max_by_date[m.group(1)] = _celsius_to_fahrenheit(float(entry.get("value", 0)))

    for i, iso in enumerate(utc_hours):
        date_key = iso[:10]
        lo = min_by_date.get(date_key)
        hi = max_by_date.get(date_key)
        if lo is not None and hi is not None:
            inferred_std = round((hi - lo) / 4.0, 2)  # crude heuristic: assume min/max ~ +/- 2 sigma
            std_f[i] = inferred_std if inferred_std > 0 else 1.5

    result = {
        "station": station.upper(),
        "source": "nws_nbm",
        "utc_hours": utc_hours,
        "temps_f": temps_f,
        "std_f": [s if s is not None else None for s in std_f],
        "updated_at_iso": grid_data.get("properties", {}).get("updateTime"),
        "raw": grid_data,
    }
    _log({"event": "nws_fetch_ok", "station": station, "hours": len(utc_hours)})
    return result


# ---------------------------------------------------------------------------
# Open-Meteo ensemble
# ---------------------------------------------------------------------------

_OM_CACHE: Dict[str, Dict[str, Any]] = {}
_OM_CACHE_HOUR: str = ""

# Open-Meteo failure handling. Success is cached per (lat,lon) per hour above, but
# failure must be remembered too: _http_json costs ~94s per dead URL (3x30s timeouts
# + backoff), the fair-value build calls fetch_openmeteo per TICKER, and run-cycle.sh
# runs 3 builds — on 2026-07-02 and again 2026-07-03 an Open-Meteo outage turned that
# into a 46min-2.5h cycle stall that held .cycle.lock and starved LIVE trading slots.
# The NBM blend degrades gracefully without the ensemble, so failing FAST loses nothing.
# Two layers, both keyed to the current UTC hour so recovery is automatic:
#   1. negative cache: a (lat,lon) that failed this hour is not retried this hour;
#   2. circuit breaker: after _OM_BREAKER_N consecutive fetch failures (any station)
#      the API itself is considered down and ALL Open-Meteo fetches are skipped for
#      the rest of the hour. Persisted to cache/om-breaker-<UTChour>.json (same idiom
#      as the GEFS hour cache) so the cycle's 2nd/3rd build processes skip instantly.
_OM_FAIL_CACHE: Dict[str, str] = {}          # cache_key -> hour it failed
_OM_CONSEC_FAILS: int = 0
_OM_BREAKER_N: int = int(os.environ.get("KALSHI_WEATHER_OM_BREAKER_N", "3"))
# TIME-based trip (2026-07-08): the count breaker only fires on 3 consecutive FAILURES. A DEGRADED
# (slow-but-succeeding) Open-Meteo instead accumulates a 30-47min build across ~20 cities x 8-9
# models, holding .cycle.lock (root cause of the 09:20 PDT live-slot starvation — no station failed
# 3x in a row, so the count breaker never tripped). Once cumulative SUCCESSFUL-fetch wall-clock this
# hour exceeds the budget, trip the breaker and skip the rest of the hour (NBM degrades gracefully
# without the ensemble — failing fast loses nothing). 0 disables. (KALSHI_WEATHER_OM_TIME_BUDGET_SEC)
_OM_TIME_BUDGET_SEC: float = float(os.environ.get("KALSHI_WEATHER_OM_TIME_BUDGET_SEC", "600"))
_OM_TIME_SPENT_SEC: float = 0.0
_OM_TIME_HOUR: str = ""
_OM_BREAKER_DIR = Path(__file__).resolve().parent.parent / "cache"


class OpenMeteoSkipped(RuntimeError):
    """Raised when a fetch is skipped by the negative cache / circuit breaker."""


def _om_breaker_marker(hour: str) -> Path:
    return _OM_BREAKER_DIR / f"om-breaker-{hour}.json"


def _om_breaker_open(hour: str) -> bool:
    global _OM_CONSEC_FAILS
    if _OM_CONSEC_FAILS >= _OM_BREAKER_N:
        return True
    try:
        if _om_breaker_marker(hour).exists():
            # Another build process this hour already tripped it.
            _OM_CONSEC_FAILS = _OM_BREAKER_N
            return True
    except Exception:
        pass
    return False


def _om_record_failure(cache_key: str, hour: str) -> None:
    global _OM_CONSEC_FAILS
    _OM_FAIL_CACHE[cache_key] = hour
    _OM_CONSEC_FAILS += 1
    if _OM_CONSEC_FAILS >= _OM_BREAKER_N:
        try:
            marker = _om_breaker_marker(hour)
            if marker.exists():
                return
            _log({"event": "openmeteo_breaker_open", "hour": hour, "consecutive": _OM_CONSEC_FAILS})
            _OM_BREAKER_DIR.mkdir(parents=True, exist_ok=True)
            tmp = marker.with_suffix(".tmp")
            tmp.write_text(json.dumps({"opened_at": time.time(), "consecutive": _OM_CONSEC_FAILS}))
            os.replace(tmp, marker)
        except Exception:
            pass  # in-process breaker still holds; the marker is cross-process best-effort


def _om_note_elapsed(hour: str, elapsed_sec: float) -> None:
    """Accumulate SUCCESSFUL Open-Meteo fetch wall-clock for the hour and, once it crosses the
    budget, trip the breaker (a slow-but-succeeding API that the consecutive-failure breaker can't
    catch). Reuses the SAME hour-keyed marker so sibling build processes skip instantly, and sets
    the in-process fail count to the threshold so this process skips too. Best-effort; never raises."""
    global _OM_TIME_SPENT_SEC, _OM_TIME_HOUR, _OM_CONSEC_FAILS
    if hour != _OM_TIME_HOUR:                 # new hour → reset the accumulator (auto-recovery)
        _OM_TIME_HOUR = hour
        _OM_TIME_SPENT_SEC = 0.0
    _OM_TIME_SPENT_SEC += max(0.0, elapsed_sec)
    if not (_OM_TIME_BUDGET_SEC > 0 and _OM_TIME_SPENT_SEC >= _OM_TIME_BUDGET_SEC):
        return
    if _OM_CONSEC_FAILS < _OM_BREAKER_N:
        _OM_CONSEC_FAILS = _OM_BREAKER_N      # hold the in-process breaker for the rest of the hour
    try:
        marker = _om_breaker_marker(hour)
        if marker.exists():
            return
        _log({"event": "openmeteo_breaker_open", "hour": hour, "reason": "time_budget_exceeded",
              "time_spent_sec": round(_OM_TIME_SPENT_SEC, 1)})
        _OM_BREAKER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"opened_at": time.time(), "reason": "time_budget_exceeded",
                                   "time_spent_sec": round(_OM_TIME_SPENT_SEC, 1)}))
        os.replace(tmp, marker)
    except Exception:
        pass  # in-process breaker still holds; the marker is cross-process best-effort


def fetch_openmeteo(lat: float, lon: float) -> Dict[str, Any]:
    """Pull multi-model ensemble from Open-Meteo.

    6 free models: GFS, ICON, ECMWF, GEM, JMA, MeteoFrance.
    Results cached per lat/lon for 1 hour to avoid redundant API calls.
    Failures are also remembered for the hour (negative cache + circuit
    breaker, see above) — raises OpenMeteoSkipped without any network I/O
    when this station or the whole API is known-bad this hour.

    Returns per-hour mean temperature and per-model spread.
    Keys:
      lat, lon, source, utc_hours, mean_temps_f, spreads_f,
      model_temps_f (dict model -> List[float]),
      updated_at_iso.
    """
    global _OM_CACHE, _OM_CACHE_HOUR, _OM_CONSEC_FAILS

    # Check per-hour cache (avoids 6x API calls on repeat fetches)
    from datetime import datetime, timezone
    current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    cache_key = f"{lat:.4f}_{lon:.4f}"
    if current_hour == _OM_CACHE_HOUR and cache_key in _OM_CACHE:
        cached = _OM_CACHE[cache_key]
        _log({"event": "openmeteo_cache_hit", "lat": lat, "lon": lon})
        return cached

    if _om_breaker_open(current_hour):
        _log({"event": "openmeteo_skipped_breaker", "lat": lat, "lon": lon, "hour": current_hour})
        raise OpenMeteoSkipped(f"open-meteo breaker open for hour {current_hour}")
    if _OM_FAIL_CACHE.get(cache_key) == current_hour:
        _log({"event": "openmeteo_skipped_failcache", "lat": lat, "lon": lon, "hour": current_hour})
        raise OpenMeteoSkipped(f"open-meteo already failed this hour for {cache_key}")

    _log({"event": "openmeteo_fetch_start", "lat": lat, "lon": lon})
    _t0 = time.monotonic()   # time-budget breaker: charge this fetch's wall-clock on success

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m"
        "&models=gfs_seamless,icon_seamless,ecmwf_ifs025,gem_seamless,jma_seamless,meteofrance_seamless,ukmo_seamless,gem_regional"
        "&temperature_unit=fahrenheit"
        "&forecast_days=7"
    )
    try:
        data = _http_json(url)
    except Exception:
        _om_record_failure(cache_key, current_hour)
        raise

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    mean_temps = hourly.get("temperature_2m", [])

    # Open-Meteo does not return a simple ensemble mean in the basic endpoint
    # when multiple models are requested; it gives the mean of the *first* model.
    # We request each model individually, compute our own mean/spread.
    models = ["gfs_seamless", "icon_seamless", "ecmwf_ifs025", "gem_seamless", "jma_seamless", "meteofrance_seamless", "ukmo_seamless", "gem_regional"]
    # HRRR: CONUS high-res rapid refresh (3km, 48h).
    if 24.0 <= lat <= 49.5 and -125.0 <= lon <= -66.5:
        models.append("gfs_hrrr")
    model_temps: Dict[str, List[float]] = {}

    for model in models:
        murl = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m"
            f"&models={model}"
            "&temperature_unit=fahrenheit"
            "&forecast_days=7"
        )
        try:
            mdata = _http_json(murl)
        except Exception:
            _om_record_failure(cache_key, current_hour)
            raise
        mhourly = mdata.get("hourly", {})
        mtimes = mhourly.get("time", [])
        mtemps = mhourly.get("temperature_2m", [])
        if len(mtimes) != len(times):
            _log({"event": "openmeteo_model_mismatch", "model": model, "expected": len(times), "got": len(mtimes)})
        model_temps[model] = mtemps

    # Compute mean + spread per hour across models
    mean_temps_f: List[float] = []
    spreads_f: List[float] = []
    for i in range(len(times)):
        vals = [model_temps[m][i] for m in models if i < len(model_temps[m])]
        if not vals:
            mean_temps_f.append(None)
            spreads_f.append(None)
            continue
        arr = [v for v in vals if v is not None]
        if not arr:
            mean_temps_f.append(None)
            spreads_f.append(None)
            continue
        import numpy as np
        mean_f = float(np.mean(arr))
        std_f = float(np.std(arr, ddof=0))
        mean_temps_f.append(round(mean_f, 2))
        spreads_f.append(round(std_f, 2))

    result = {
        "lat": lat,
        "lon": lon,
        "source": "open_meteo_ensemble",
        "utc_hours": times,
        "mean_temps_f": mean_temps_f,
        "spreads_f": spreads_f,
        "model_temps_f": model_temps,
        "updated_at_iso": data.get("generation_time_ms"),
    }
    _log({"event": "openmeteo_fetch_ok", "lat": lat, "lon": lon, "hours": len(times)})
    _OM_CONSEC_FAILS = 0  # a full success proves the API is up — close the FAILURE breaker window
    _om_note_elapsed(current_hour, time.monotonic() - _t0)  # …but a slow-yet-succeeding API still trips on total time

    # Cache result for 1 hour
    if current_hour != _OM_CACHE_HOUR:
        _OM_CACHE = {}
        _OM_CACHE_HOUR = current_hour
    _OM_CACHE[cache_key] = result

    return result


# ---------------------------------------------------------------------------
# City / station helpers
# ---------------------------------------------------------------------------

CITY_ALIASES = {
    "NY": "NYC",   # KXHIGHNY uses "NY" not "NYC"
}


def get_station_for_city(city_code: str) -> Tuple[str, float, float]:
    """Return (metar_station, lat, lon) for a Kalshi city code."""
    city_code = CITY_ALIASES.get(city_code.upper(), city_code.upper())
    meta = STATIONS.get(city_code.upper())
    if not meta:
        raise ValueError(f"Unknown city code: {city_code}")
    return meta["station"], meta["lat"], meta["lon"]


def get_all_city_codes() -> List[str]:
    return list(STATIONS.keys())


# ---------------------------------------------------------------------------
# Market ticker parser
# ---------------------------------------------------------------------------

RE_TICKER = re.compile(
    r"^K"
    r"(XHIGHT|XLOWT|XTEMP|XHIGH|XLOW)"
    r"([A-Z]{2,5})"       # city code (e.g. DC, HOU, NYCH, PHIL, SATX)
    r"-"
    r"(\d{2}[A-Z]{3}\d{2,4})"  # date string, e.g. 26MAY28 or 26MAY2816
    r"-"
    r"([TB])([\d.]+)$"    # T = threshold (above), B = between
)


def _parse_date(date_str: str) -> Tuple[str, Optional[int]]:
    """Parse Kalshi date strings.
    Returns (YYYY-MM-DD, hour_utc_or_None).
    Format: YY + MMM + DD, e.g. 26MAY28 -> 2026-05-28, None
             26MAY2816 -> 2026-05-28, 16
    """
    date_str = date_str.upper()
    # With hour: e.g. 26MAY2816
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})$", date_str)
    if m:
        year_short = int(m.group(1))
        mon_str = m.group(2)
        day = int(m.group(3))
        hour = int(m.group(4))
        year = 2000 + year_short
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        month = month_map[mon_str]
        return f"{year:04d}-{month:02d}-{day:02d}", hour

    # Without hour: e.g. 26MAY28
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2,4})$", date_str)
    if m:
        year_short = int(m.group(1))
        mon_str = m.group(2)
        day = int(m.group(3))
        year = 2000 + year_short
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        month = month_map[mon_str]
        return f"{year:04d}-{month:02d}-{day:02d}", None

    raise ValueError(f"Cannot parse date string: {date_str}")


def _infer_direction_from_title(title: str, threshold_f: float) -> Optional[str]:
    """Infer whether YES means above/below the threshold from the market title.

    Returns "above", "below", or None when the title gives no usable direction cue.
    Callers decide what None means: the trading/pricing path (parse_market_ticker with
    strict_direction=True) SKIPS the market rather than guess, because a silent wrong
    guess flips the whole tail and posts max-size wrong-side orders (audit 2026-07-06).
    Diagnostic callers may fall back to a default."""
    title_lower = (title or "").lower()
    # Most reliable: the explicit inequality against the exact threshold, e.g. "<93°".
    t_str = f"{threshold_f:g}°"
    if f">{t_str}" in title_lower or f">= {t_str}" in title_lower or f"≥{t_str}" in title_lower:
        return "above"
    if f"<{t_str}" in title_lower or f"<= {t_str}" in title_lower or f"≤{t_str}" in title_lower:
        return "below"
    # Generic phrasings. Cover the "S° or lower/higher" / "at most/least" families that the
    # old code missed and silently mis-defaulted to "above".
    below_kw = ("below", "less than", "under", "or lower", "or less", "at most",
                "no more than", "no higher than", "cooler")
    above_kw = ("above", "greater than", "over", "or higher", "or more", "at least",
                "no less than", "no lower than", "warmer")
    has_below = any(k in title_lower for k in below_kw)
    has_above = any(k in title_lower for k in above_kw)
    if has_below and not has_above:
        return "below"
    if has_above and not has_below:
        return "above"
    # No cue, or contradictory cues → undetermined. Do NOT guess.
    return None


def parse_market_ticker(ticker: str, raw_market: Optional[Dict[str, Any]] = None,
                        strict_direction: bool = False) -> Dict[str, Any]:
    """Decode a Kalshi weather market ticker.

    Examples:
      KXHIGHTHOU-26MAY28-T91     -> above 91 F daily high
      KXHIGHTHOU-26MAY29-B90.5   -> between (89.5, 91.5] daily high
      KXTEMPNYCH-26MAY2816-T82.99 -> above 82.99 F at 16 UTC

    Returns dict with:
      market_type, city_code, date_iso, hour_utc,
      threshold_f, bin_kind, bin_low, bin_high
    """
    ticker = ticker.strip().upper()
    m = RE_TICKER.match(ticker)
    if not m:
        raise ValueError(f"Ticker regex did not match: {ticker}")

    market_type_raw = m.group(1)
    # Normalize the ticker city to the canonical station/climatology key (KXHIGHNY → "NY" → "NYC").
    # Without this the live/climatology lookup misses NYC (climatology is keyed "NYC") and the live
    # arm skips every NYC market. ticker is already upper()'d above. (2026-06-25)
    city_code = CITY_ALIASES.get(m.group(2), m.group(2))
    date_str = m.group(3)
    kind_char = m.group(4)
    value_str = m.group(5)

    market_type_map = {"XHIGHT": "daily_high", "XLOWT": "daily_low", "XTEMP": "hourly_temp", "XHIGH": "daily_high", "XLOW": "daily_low"}
    market_type = market_type_map[market_type_raw]

    date_iso, hour_utc = _parse_date(date_str)
    threshold_f = float(value_str)

    if kind_char == "T":
        # T = threshold, direction from the title. Infer with the RAW integer strike (the title
        # reads ">96°"/"<98°"), THEN shift to the settlement boundary.
        _title = raw_market.get("title", "") if raw_market else ""
        bin_kind = _infer_direction_from_title(_title, threshold_f)
        if bin_kind is None:
            # No usable direction cue. In the TRADING/pricing path (strict_direction=True)
            # refuse to guess — a wrong tail posts max-size wrong-side orders across the whole
            # band (audit 2026-07-06). Skip the market (builder catches ValueError) + alert.
            if strict_direction:
                try:
                    from trader.notify import alert
                    alert(f"⚠️ undetermined T-market direction, SKIPPED: {ticker} "
                          f"title={_title!r} — title phrasing changed? check parser",
                          key="undetermined_direction", dedup_seconds=3600)
                except Exception:
                    pass
                raise ValueError(f"undetermined T-market direction for {ticker}: title={_title!r}")
            # Non-trading (diagnostic/settlement) fallback: preserve the documented default.
            bin_kind = "above"
        # FIX 2026-07-03 (weather-model QA): Kalshi T-markets settle the INTEGER extreme one step past
        # the strike — ">S" pays at S+1 (subtitle "(S+1)° or above"), "<S" pays at S-1 ("(S-1)° or
        # below") — so the continuous YES boundary is strike ± 0.5, not the raw integer. Both the
        # pricing (exceedance/KDE/gaussian read threshold_f) and the Brier scoring (shadow_score.outcome
        # `actual > thr`, on the continuous Open-Meteo actual) consume this, so a raw-integer threshold
        # over-counted ~0.5°F of mass on the priced side of EVERY T-market (~5-7¢). Strict >/< convention
        # confirmed against the live Kalshi API for both high and low series. (B-markets already carry
        # ±1.0 half-integer edges; half-integer threshold_f is thus already handled system-wide.)
        if bin_kind == "above":
            threshold_f += 0.5
            bin_low = threshold_f
            bin_high = float("inf")
        else:
            threshold_f -= 0.5
            bin_low = float("-inf")
            bin_high = threshold_f
    elif kind_char == "B":
        bin_kind = "between"
        bin_low = threshold_f - 1.0
        bin_high = threshold_f + 1.0
    else:
        raise ValueError(f"Unknown kind char {kind_char}")

    return {
        "ticker": ticker,
        "market_type": market_type,
        "city_code": city_code,
        "date_iso": date_iso,
        "hour_utc": hour_utc,
        "threshold_f": threshold_f,
        "bin_kind": bin_kind,
        "bin_low": bin_low,
        "bin_high": bin_high,
    }

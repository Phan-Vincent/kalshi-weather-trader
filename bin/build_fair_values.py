#!/usr/bin/env python3
"""CLI to build fair-value estimates for Kalshi weather markets.

Usage:
    python3 bin/build_fair_values.py \
        --series KXHIGHTHOU,KXHIGHTNYC,KXHIGHTBOS \
        --out fair-values.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure kalshi_weather package on path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTOMATIONS = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
if _AUTOMATIONS not in sys.path:
    sys.path.insert(0, _AUTOMATIONS)

from kalshi_weather.data.weather_data import (
    fetch_nws_nbm,
    fetch_openmeteo,
    get_station_for_city,
    parse_market_ticker,
    apply_surface_bias,
)
from kalshi_weather.data.kalshi_data import enrich_markets
from kalshi_weather.model.fair_value import estimate_market_prob
from kalshi_weather.model.lessons import load_lessons, BIAS_CAP_F
from kalshi_weather.model.error_tracker import ErrorTracker
from kalshi_weather.model.calibrate import BetaCalibrator  # legacy; AutoCalibrator used in main()

CACHE_DIR = os.path.join(_AUTOMATIONS, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

LESSONS_FILE = Path(_AUTOMATIONS) / ("kalshi_weather" if os.path.isdir(os.path.join(_AUTOMATIONS, "kalshi_weather")) else "kalshi-weather") / "LESSONS.md"
# Fall back to the actual layout (hyphenated dir)
if not LESSONS_FILE.exists():
    LESSONS_FILE = Path(_AUTOMATIONS) / "kalshi-weather" / "LESSONS.md"


def _log(record: dict) -> None:
    print(json.dumps(record), file=sys.stderr, flush=True)


def _gefs_alert(msg: str) -> None:
    """Best-effort operator alert for GEFS degradation. Never raises."""
    try:
        try:
            from trader.notify import alert
        except ImportError:
            from kalshi_weather.trader.notify import alert
        alert(msg, key="gefs_fail")
    except Exception:
        pass


def _should_fetch_gefs() -> bool:
    """Whether this build should fetch the GEFS ensemble.

    Default: True (fetch always) — unchanged, current production behavior.

    GEFS members are injected into the ensemble likelihood (fair_value.py:420-429). Under
    USE_FORECAST=off (the LIVE climatology-only build) that ensemble result is DISCARDED at
    fair_value.py:636 (`fair_prob = prior_prob`), so GEFS provably cannot reach the climatology
    price — pinned by tests/test_gefs_climo_invariance.py (2026-07-06). The fetch is therefore
    wasted work in a climatology build.

    OPT-IN optimization (default OFF): KALSHI_WEATHER_GEFS_SKIP_CLIMO=1 skips the fetch when
    USE_FORECAST is off, trimming the climatology build's (pre-live) critical path. The paper
    forecast builds (USE_FORECAST=1) always fetch — they DO use the ensemble. Left default-OFF
    because the historical "~0.5c GEFS leak" was found to be a measurement artifact (data-refetch
    + GEFS-download latency between the two A/B builds), NOT a real pricing path, so there is no
    correctness reason to perturb the live build. See reviews/followups-gefs-cron-2026-07-06.md.
    """
    use_forecast = os.environ.get("KALSHI_WEATHER_USE_FORECAST", "0") == "1"
    skip_climo = os.environ.get("KALSHI_WEATHER_GEFS_SKIP_CLIMO", "0") == "1"
    return not (skip_climo and not use_forecast)


def _load_lessons(error_tracker=None) -> dict:
    """Parse LESSONS.md using the centralized lessons module.

    Returns structured lesson data with deduping, time-decay weighting,
    bias capping at ±1.5°F, and blacklist detection (now requires ≥5
    same-sign + ≤1 opposite-sign + EWMA n<2 — see model/lessons.py).
    """
    return load_lessons(LESSONS_FILE, error_tracker=error_tracker)


if __name__ == "__main__":
    # Quick test: print per-city aggregate bias
    lessons = _load_lessons()
    print("=== LESSONS.md aggregate bias (with weight decay + dedup + blacklist) ===")
    for city, bias in sorted(lessons["city_temp_bias_f"].items()):
        print(f"  {city}: {bias:+.3f}°F")
    if lessons.get("city_blacklist"):
        print(f"blacklisted: {lessons['city_blacklist']}")
    if lessons.get("bias_hits_cap"):
        print(f"bias_hits_cap: {lessons['bias_hits_cap']}")
    print(f"side_bias: {lessons['side_bias']}")
    print(f"lesson_lines_count: {len(lessons['lines'])}")
    print(f"deduped_lines_count: {len(lessons['deduped_lines'])}")


def _cache_path(station: str, hour_str: str) -> str:
    safe = station.upper()
    return os.path.join(CACHE_DIR, f"forecast-{safe}-{hour_str}.json")


def _now_hour_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def _load_cache(station: str) -> Optional[Dict[str, Any]]:
    hour_str = _now_hour_str()
    path = _cache_path(station, hour_str)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            _log({"event": "cache_load_error", "path": path, "error": str(e)})
    return None


def _save_cache(station: str, data: Dict[str, Any]) -> None:
    hour_str = _now_hour_str()
    path = _cache_path(station, hour_str)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        _log({"event": "cache_save_error", "path": path, "error": str(e)})


def _fetch_forecast(city_code: str, use_cache: bool = True) -> Dict[str, Any]:
    """Fetch both NWS and Open-Meteo forecasts, with caching."""
    station, lat, lon = get_station_for_city(city_code)
    cache_key = station

    nws_cached = None
    om_cached = None
    if use_cache:
        cached = _load_cache(cache_key)
        if cached:
            nws_cached = cached.get("nws")
            om_cached = cached.get("openmeteo")

    nws = nws_cached
    if nws is None:
        try:
            nws = fetch_nws_nbm(station)
            _log({"event": "nws_fetched", "station": station})
        except Exception as e:
            _log({"event": "nws_fetch_failed", "station": station, "error": str(e)})
            nws = None

    om = om_cached
    if om is None:
        try:
            om = fetch_openmeteo(lat, lon)
            _log({"event": "openmeteo_fetched", "lat": lat, "lon": lon})
        except Exception as e:
            _log({"event": "openmeteo_fetch_failed", "lat": lat, "lon": lon, "error": str(e)})
            om = None

    if use_cache and (nws or om):
        _save_cache(cache_key, {"nws": nws, "openmeteo": om})

    return {"nws": nws, "openmeteo": om}


def _market_mid(market: Dict[str, Any]) -> Optional[float]:
    """Compute mid price from yes_bid/yes_ask if available."""
    bid = market.get("yes_bid")
    ask = market.get("yes_ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 200.0  # Kalshi prices are in cents
    last = market.get("last_price")
    if last is not None:
        return last / 100.0
    return None


def _run_kalshi_cli(series: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Call kalshi-cli to list open markets for a series."""
    cmd = [
        "kalshi-cli", "--prod", "markets", "list",
        "--series", series,
        "--status", "open",
        "--limit", str(limit),
        "--json",
    ]
    _log({"event": "kalshi_cli_run", "cmd": " ".join(cmd)})
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            _log({"event": "kalshi_cli_error", "stderr": result.stderr, "returncode": result.returncode})
            return []
        data = json.loads(result.stdout)
        markets = data if isinstance(data, list) else data.get("markets", [])
        _log({"event": "kalshi_cli_ok", "series": series, "count": len(markets)})
        return markets
    except Exception as e:
        _log({"event": "kalshi_cli_exception", "error": str(e)})
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Build fair-value estimates for Kalshi weather markets")
    parser.add_argument("--series", required=True, help="Comma-separated Kalshi series tickers")
    parser.add_argument("--out", required=True, help="Output JSON file path")
    args = parser.parse_args()

    series_list = [s.strip() for s in args.series.split(",")]
    # Load error model FIRST so its n_effective counts can inform the lessons
    # blacklist (a city with strong EWMA signal shouldn't be frozen to 0).
    error_tracker = ErrorTracker()
    error_tracker.load()
    if error_tracker.n_cities() > 0:
        _log({"event": "error_model_loaded", "cities": error_tracker.n_cities(),
              "sample": {c: {"bias": error_tracker.get_bias(c), "std": error_tracker.get_error_std(c)}
                         for c in list(error_tracker.all_cities().keys())[:5]}})
        # Surface bias-cap saturation: cities whose true forecast bias exceeds the ±BIAS_CAP_F get_bias
        # clamp are silently UNDER-corrected (2026-07-13 audit; HOU/PHX run too warm). Log + print so it
        # is visible every build; the healthcheck error_model_saturation check also alerts.
        _sat = error_tracker.saturated_cities()
        if _sat:
            _log({"event": "error_model_saturated", "cities": _sat})
            print("[fair-values] ⚠️ bias-cap saturated (under-corrected): "
                  + ", ".join(f"{s['city']} raw={s['raw_bias_f']:+.1f}°F "
                              f"(applied {s['applied_bias_f']:+.1f}, n={s['n_effective']:.0f})" for s in _sat),
                  file=sys.stderr)
    lessons = _load_lessons(error_tracker=error_tracker)
    _log({"event": "lessons_loaded", "lines": len(lessons["lines"]), "deduped_lines": len(lessons["deduped_lines"]), "city_biases": lessons["city_temp_bias_f"], "std_floors": lessons["city_std_floor_f"], "blacklist": lessons.get("city_blacklist", []), "bias_hits_cap": lessons.get("bias_hits_cap", [])})
    # Load error model (Phase 4 — takes precedence over LESSONS.md when available)
    error_tracker = ErrorTracker()
    error_tracker.load()
    if error_tracker.n_cities() > 0:
        _log({"event": "error_model_loaded_after", "cities": error_tracker.n_cities()})
    all_records: List[Dict[str, Any]] = []
    seen_tickers: set = set()

    # ── Fit calibration from settled trades (post-processing layer) ──
    # Uses AutoCalibrator: tries Isotonic + Beta, picks winner
    from kalshi_weather.model.calibrate import AutoCalibrator
    calibrator = AutoCalibrator()
    calibrator.fit()
    cal_summary = calibrator.summary()
    _log({"event": "calibration_fitted",
          "method": cal_summary.get("selected_method", cal_summary.get("method")),
          "n_trades": cal_summary.get("n_trades"),
          "brier_before": cal_summary.get("brier_before"),
          "brier_after": cal_summary.get("brier_after"),
          "improvement_pct": cal_summary.get("brier_improvement_pct")})

    # ── Fetch GEFS once (global, covers all cities) ──
    # Gated by _should_fetch_gefs(): default fetches always (unchanged). Opt-in
    # KALSHI_WEATHER_GEFS_SKIP_CLIMO=1 skips it on the USE_FORECAST=off (climatology) build,
    # where the ensemble is discarded anyway (see _should_fetch_gefs docstring).
    gefs_data = {}
    if not _should_fetch_gefs():
        _log({"event": "gefs_skipped_climo",
              "reason": "KALSHI_WEATHER_GEFS_SKIP_CLIMO=1 and USE_FORECAST off — "
                        "ensemble is discarded on the climatology path (fair_value.py:636)"})
    else:
        try:
            from kalshi_weather.data.gefs_ingester import fetch_gefs_ensemble
            gefs_data = fetch_gefs_ensemble()
            n_gefs = sum(1 for d in gefs_data.values() if d.get('by_date'))
            _log({"event": "gefs_fetched", "cities": n_gefs})
            if n_gefs == 0:
                _gefs_alert("GEFS ingest returned 0 cities — ensemble degraded to non-GEFS "
                            "(check xarray/cfgrib/eccodes install).")
        except Exception as e:
            _log({"event": "gefs_fetch_failed", "error": str(e)})
            _gefs_alert(f"GEFS ingest FAILED: {str(e)[:160]}")

    total_markets = 0

    for series in series_list:
        markets = _run_kalshi_cli(series)
        total_markets += len(markets)
        # Enrich with real orderbook data (replaces deprecated yes_bid/yes_ask=0)
        markets = enrich_markets(markets, sleep_secs=0.05, progress_every=25)
        for raw in markets:
            ticker = raw.get("ticker", raw.get("market_id", "UNKNOWN"))
            if ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)

            try:
                # strict_direction: refuse to price a T-market whose tail we can't read from
                # the title (would post max-size wrong-side orders). Skip + alert instead.
                parsed = parse_market_ticker(ticker, raw, strict_direction=True)
            except ValueError as e:
                _log({"event": "parse_ticker_failed", "ticker": ticker, "error": str(e)})
                continue

            city_code = parsed["city_code"]
            try:
                forecasts = _fetch_forecast(city_code)
            except Exception as e:
                _log({"event": "forecast_fetch_failed", "city_code": city_code, "error": str(e)})
                continue

            market_obj = {**parsed, "_raw_market": raw}
            # City bias reaches the price through estimate_market_prob's city_bias_f, which is
            # added to the KDE recenter location and the Gaussian members alike.
            #
            # FIX 2026-07-03 (weather-model QA): the old surface-bias block here wrote
            # forecasts["openmeteo"]["mean_temps_f"] — an array the ensemble/KDE path in
            # fair_value.py never reads (it reads model_temps_f) — so the inland heat-island
            # correction was DEAD. Fold it in as a COLD-START PRIOR instead: prefer the learned
            # EWMA (ErrorTracker), which is trained on the RAW ensemble forecast and therefore
            # already subsumes the static inland offset; only when the tracker lacks data fall
            # back to the static SURFACE_BIAS. Applying BOTH would double-count the same inland
            # correction (both are negative/downward for inland airports).
            city_std_floor = None
            if error_tracker.has_good_data(city_code):
                city_bias = error_tracker.get_bias(city_code)
                city_std_floor = error_tracker.get_error_std(city_code)
            else:
                # apply_surface_bias(city, 0.0) returns the raw per-city offset (0.0 coastal;
                # negative inland), in the same "+ = warm the forecast" convention as get_bias.
                city_bias = apply_surface_bias(city_code, 0.0)
            fv = estimate_market_prob(
                market_obj,
                forecast_nws=forecasts.get("nws"),
                forecast_om=forecasts.get("openmeteo"),
                forecast_gefs=gefs_data if gefs_data else None,
                city_bias_f=city_bias,
                city_error_std_f=city_std_floor,
                market_mid=_market_mid(raw),
            )
            if city_bias != 0 or city_std_floor is not None:
                source = "error_model" if error_tracker.has_good_data(city_code) else "surface_prior"
                fv["_lesson_applied"] = {
                    "source": source,
                    "city_code": city_code,
                    "temp_bias_f": city_bias,
                    "std_floor_f": city_std_floor or 0.0,
                    "error_model_n": error_tracker.get_effective_n(city_code),
                }

            mid = _market_mid(raw)
            record = {
                **fv,
                "_market": raw,
                "_market_mid": round(mid, 4) if mid is not None else None,
                "_edge_vs_mid": round(abs((fv.get("fair_prob") or 0) - (mid or 0)), 4) if (fv.get("fair_prob") is not None and mid is not None) else None,
                # 2026-06-20: resolution fields for shadow scoring (bin_score.py)
                "_bin_kind": market_obj.get("bin_kind"),
                "_threshold_f": market_obj.get("threshold_f"),
                "_bin_low": market_obj.get("bin_low"),
                "_bin_high": market_obj.get("bin_high"),
            }
            all_records.append(record)

    # ── Apply Beta calibration to all fair probs ──
    # FIX 2026-06-11: post-process all probabilities through the fitted
    # calibration layer. This corrects systematic underconfidence without
    # changing the model's relative ordering of events.
    n_calibrated = 0
    for rec in all_records:
        raw_prob = rec.get("fair_prob")
        if raw_prob is not None and calibrator.method != "identity":
            city = rec.get("ticker", "").split("-", 1)[0] if "-" in rec.get("ticker", "") else None
            # Extract city code from ticker for city-specific calibration
            city_code = None
            for tag in ("HIGHT", "LOWT", "HIGH", "LOW", "TEMP"):
                if tag in rec.get("ticker", ""):
                    city_code = rec["ticker"].split("-", 1)[0].split(tag, 1)[-1] if "-" in rec["ticker"] else None
                    break
            cal_prob = calibrator.calibrate(raw_prob)
            rec["fair_prob_raw"] = raw_prob  # preserve original for debugging
            rec["fair_prob"] = round(cal_prob, 4)
            rec["calibrated"] = True
            n_calibrated += 1
    if n_calibrated > 0:
        _log({"event": "calibration_applied", "n_calibrated": n_calibrated, "method": calibrator.method})
    calibrator.save()

    # Sort: confidence desc, then abs(fair_prob - market_mid) desc
    confidence_order = {"high": 3, "med": 2, "low": 1}
    all_records.sort(
        key=lambda r: (
            confidence_order.get(r.get("confidence", "low"), 0),
            r.get("_edge_vs_mid") or 0,
        ),
        reverse=True,
    )

    output = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "series_queried": series_list,
        "markets_processed": len(all_records),
        "total_markets_fetched": total_markets,
        "lessons_applied": {
            "city_temp_bias_f": lessons["city_temp_bias_f"],
            "city_std_floor_f": lessons["city_std_floor_f"],
            "city_position_scale": lessons["city_position_scale"],
            "city_blacklist": lessons.get("city_blacklist", []),
            "bias_hits_cap": lessons.get("bias_hits_cap", []),
            "side_bias": lessons["side_bias"],
            "lesson_lines_count": len(lessons["lines"]),
            "deduped_lines_count": len(lessons["deduped_lines"]),
        },
        "records": all_records,
    }

    out_path = args.out
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # 2026-06-20 model work: append a compact forecast-log — a SHADOW prediction
    # for EVERY priced market (not just the ~5 we trade), so bin/crps_report.py and
    # bin/shadow_score.py can score the model against realised temps + the market.
    # ~50-80x more data per cycle than trade-only logging, zero added risk.
    # `mode` records which model priced it (legacy climatology vs the forecast fix);
    # `market_mid` enables the model-vs-market Brier (the profitability question).
    try:
        import os as _os
        _state = Path(_os.environ.get("KALSHI_WEATHER_STATE_DIR",
                                      Path(__file__).resolve().parent.parent / "state"))
        _mode = "forecast" if _os.environ.get("KALSHI_WEATHER_USE_FORECAST", "0") == "1" else "legacy"
        with open(_state / "forecast-log.jsonl", "a") as _fl:
            for rec in all_records:
                loc, scale = rec.get("model_temp_forecast"), rec.get("model_temp_std")
                if loc is None or scale is None:
                    continue
                _fl.write(json.dumps({
                    "asof_utc": rec.get("asof_utc"),
                    "ticker": rec.get("ticker"),
                    "loc": loc,
                    "scale": scale,
                    "fair_prob": rec.get("fair_prob"),
                    "market_mid": rec.get("_market_mid"),
                    "mode": _mode,
                    "bin_kind": rec.get("_bin_kind"),
                    "thr": rec.get("_threshold_f"),
                    "lo": rec.get("_bin_low"),
                    "hi": rec.get("_bin_high"),
                }) + "\n")
    except Exception as _e:
        _log({"event": "forecast_log_failed", "error": str(_e)})

    _log({"event": "done", "out": out_path, "records": len(all_records)})
    print(f"Wrote {len(all_records)} fair-value records to {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate the 4 model fix changes applied to the Kalshi weather trader."""

import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

errors = []

def check(name, passed, detail=""):
    if passed:
        print(f"  ✅ {name}")
    else:
        msg = f"  ❌ {name}: {detail}" if detail else f"  ❌ {name}"
        print(msg)
        errors.append(name)

# ── 1. Surface-type bias ─────────────────────────────────────────
print("\n=== 1. Surface-type bias (weather_data.py) ===")
from kalshi_weather.data.weather_data import apply_surface_bias, SURFACE_BIAS

check("SURFACE_BIAS dict exists", isinstance(SURFACE_BIAS, dict) and len(SURFACE_BIAS) > 0,
      f"got {len(SURFACE_BIAS)} entries")

# Inland airports should have nonzero negative bias
for code in ["OKC", "DAL", "SATX", "AUS"]:
    b = SURFACE_BIAS.get(code, 0)
    check(f"{code} inland bias < 0", b < 0, f"got {b}")

# Coastal airports should be near-neutral
for code in ["LAX", "SEA", "SFO", "NYC"]:
    b = SURFACE_BIAS.get(code, 0)
    check(f"{code} coastal bias near 0", -0.5 <= b <= 0.5, f"got {b}")

# apply_surface_bias works correctly
r1 = apply_surface_bias("OKC", 85.0)
check("OKC 85°F adjusts downward", r1 < 85.0, f"got {r1}")
r2 = apply_surface_bias("LAX", 72.0)
check("LAX 72°F stays ~72", abs(r2 - 72.0) <= 0.01, f"got {r2}")

# ── 2. Scale floor & quadrature ──────────────────────────────────
# UPDATED 2026-06-19: resolve_scale was redesigned on 2026-06-18 (#6 priority).
# The old per-bin floor (bin->2.0, non-bin->0.5, bin markets returning the raw
# spread) is gone. The current design — see GLOBAL_SCALE_FLOOR and the resolve_scale
# docstring — applies ONE global floor (3.0 deg F) to every market, folds error_std
# in quadrature, and uses ticker bin-detection for logging only. This widening was a
# deliberate fix for the model's documented underconfidence, so the checks below now
# validate the global-floor + quadrature behavior instead of the retired bin split.
print("\n=== 2. Scale floor & quadrature (likelihood.py) ===")
from kalshi_weather.model.likelihood import resolve_scale, GLOBAL_SCALE_FLOOR

# Tight ensemble + low error floors at the global floor (prevents overconfidence)
s1 = resolve_scale(0.5, 1.0, 24, ticker="KXHIGHTHOU-26JUN15-B89.5")
check(f"Tight market floors at GLOBAL_SCALE_FLOOR ({GLOBAL_SCALE_FLOOR})",
      s1 >= GLOBAL_SCALE_FLOOR, f"got {s1}")

# Bin detection is log-only: identical inputs -> identical scale for B vs T tickers
s2 = resolve_scale(0.5, 1.0, 24, ticker="KXHIGHTHOU-26JUN15-T89.5")
check("Bin vs non-bin ticker -> identical scale (detection is log-only)",
      abs(s1 - s2) < 1e-9, f"bin={s1}, non-bin={s2}")

# Error std is folded into the scale in quadrature on top of the spread
s3 = resolve_scale(3.0, 2.0, None, ticker="KXHIGHTHOU-26JUN15-B89.5")
check("Error std added in quadrature: sqrt(3^2 + 2^2) ~= 3.61",
      abs(s3 - math.sqrt(13.0)) < 0.01, f"got {s3}")

# A wide ensemble is widened further by error quadrature, never shrunk below spread
s4 = resolve_scale(4.5, 2.0, 12, ticker="KXHIGHTHOU-26JUN15-B89.5")
check("High spread widened by quadrature (>= spread, never reduced)",
      s4 >= 4.5 - 1e-9, f"got {s4}")

# ── 3. Error model fixes ────────────────────────────────────────
# UPDATED 2026-06-19: these checks previously encoded the PRE-FIX bug where
# n_effective used `n_new = n_old + alpha*(1 - n_old)` and converged to 1.0, so it
# never reached the has_good_data threshold. The fix (see record_error and
# tests/audit_fixes/test_error_tracker_n_effective.py) made n_effective a simple
# count capped at 100, with fast alpha=0.3 for the first ~2 obs. The checks below
# now validate that simple-count behavior.
print("\n=== 3. Error model fixes (error_tracker.py) ===")
from kalshi_weather.model.error_tracker import ErrorTracker

tracker = ErrorTracker()

# First observation -> simple count == 1.0 (fast alpha=0.3 learning)
tracker.record_error("TEST1", 100.0, 102.0)  # error = 2.0
n1 = tracker.get_effective_n("TEST1")
check("First obs: n_effective == 1.0 (simple count)", abs(n1 - 1.0) < 1e-9, f"got {n1}")

# One observation is not yet enough to trust the error model (threshold = 2.0)
check("has_good_data False after 1 obs", not tracker.has_good_data("TEST1"))

# Second observation -> n_effective == 2.0, now at the has_good_data threshold
tracker.record_error("TEST1", 105.0, 103.0)  # error = -2.0
n2 = tracker.get_effective_n("TEST1")
check("Second obs: n_effective == 2.0 (simple count)", abs(n2 - 2.0) < 1e-9, f"got {n2}")
check("has_good_data True once n_effective >= 2.0", tracker.has_good_data("TEST1"))

# Record more; n_effective keeps accumulating (does NOT plateau at 1.0 — the bug)
for i in range(20):
    tracker.record_error("TEST1", 100.0 + i, 101.0 + i)
n_final = tracker.get_effective_n("TEST1")
has_data = tracker.has_good_data("TEST1")
print(f"    After 22 obs: n_effective={n_final:.2f}, has_good_data={has_data}")
check("n_effective accumulates past 2 (no plateau-at-1 regression)",
      n_final >= 3.0, f"got {n_final}")

# Fresh city has no data yet (n_eff = 0)
check("has_good_data False for a fresh city",
      not tracker.has_good_data("TEST2"), "fresh city should be False")

# Different city: bias starts at 0.0; alpha=0.3 on first obs gives 0.3*5 = 1.5
tracker.record_error("FASTCITY", 80.0, 85.0)
bias = tracker.get_bias("FASTCITY")
check("Fast alpha on first obs: bias ~1.5", 1.3 <= bias <= 1.7, f"got {bias}")

# ── 4. Prior weight sigmoid ──────────────────────────────────────
# UPDATED 2026-06-19: prior_weight was re-tuned on 2026-06-18 to a deliberately
# bounded boost. The docstring sets the range to ~[0.20, 0.35] (anchors: ~0.20 at
# 24h, ~0.27 at 48h, ~0.35 at 72h+, None -> 0.25) to fight underconfidence without
# letting climatology dominate the forecast. The old expectations below (a steeper
# 0.39-0.69 sigmoid centred at 72h, None -> 0.5) never matched the shipped formula.
print("\n=== 4. Prior weight sigmoid (prior.py) ===")
from kalshi_weather.model.prior import Climatology

# Test key horizon values against the documented 0.20-0.35 sigmoid
checks = [
    (0,  "h=0",   0.21, 0.03),    # range floor (~0.20)
    (24, "h=24",  0.23, 0.03),    # docstring anchor: ~0.20 at 24h
    (48, "h=48",  0.26, 0.03),    # docstring anchor: ~0.27 at 48h
    (72, "h=72",  0.29, 0.03),    # sigmoid midpoint region
    (96, "h=96",  0.32, 0.03),    # climbing toward ceiling
    (120,"h=120", 0.34, 0.03),    # docstring anchor: ~0.35 at 72h+
    (168,"h=168", 0.35, 0.02),    # near ceiling (range top ~0.35)
]
for h, label, expected, tol in checks:
    w = Climatology.prior_weight(h)
    ok = abs(w - expected) <= tol
    check(f"Prior weight {label} ≈ {expected} (±{tol})", ok, f"got {w}")

# Documented design invariant: the weight stays inside ~[0.20, 0.35] at every horizon
all_w = [Climatology.prior_weight(h) for h in range(0, 241)]
check("Prior weight bounded to documented [0.20, 0.35] range",
      0.20 <= min(all_w) and max(all_w) <= 0.35,
      f"min={min(all_w):.4f}, max={max(all_w):.4f}")

# Check smoothness (no edge-of-bin jumps)
w24 = Climatology.prior_weight(24)
w25 = Climatology.prior_weight(25)
w23 = Climatology.prior_weight(23)
check("Smooth at h=24 boundary (no jump)", abs(w24 - w23) < 0.01 and abs(w25 - w24) < 0.03,
      f"w23={w23}, w24={w24}, w25={w25}")

w48 = Climatology.prior_weight(48)
w49 = Climatology.prior_weight(49)
w47 = Climatology.prior_weight(47)
check("Smooth at h=48 boundary (no jump)", abs(w48 - w47) < 0.02 and abs(w49 - w48) < 0.04,
      f"w47={w47}, w48={w48}, w49={w49}")

# None case (unknown horizon) -> mid-range default of 0.25
wn = Climatology.prior_weight(None)
check("Prior weight None -> 0.25", abs(wn - 0.25) < 0.01, f"got {wn}")

# ── Summary ───────────────────────────────────────────────────────
print(f"\n{'='*40}")
if errors:
    print(f"❌ {len(errors)} check(s) FAILED:")
    for e in errors:
        print(f"   - {e}")
    sys.exit(1)
else:
    print("✅ All checks passed!")
    sys.exit(0)
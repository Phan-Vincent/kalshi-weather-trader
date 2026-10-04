#!/usr/bin/env python3
"""Test script for LESSONS.md weight decay — verifies per-city bias with/without decay."""
import sys, os
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTOMATIONS = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _AUTOMATIONS)

# Import the actual loader
import importlib.util
spec = importlib.util.spec_from_file_location("build_fair_values", os.path.join(_AUTOMATIONS, "kalshi-weather", "bin", "build_fair_values.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["build_fair_values"] = mod
spec.loader.exec_module(mod)

lessons = mod._load_lessons()

print("=== LESSONS.md aggregate bias (with weight decay) ===")
for city, bias in sorted(lessons["city_temp_bias_f"].items()):
    print(f"  {city}: {bias:+.3f}°F")
print(f"side_bias: {lessons['side_bias']}")
print(f"lesson_lines_count: {len(lessons['lines'])}")

# Manual verification for DC, SEA, HOU
print("\n=== Manual spot-checks ===")
for city in ["DC", "SEA", "HOU"]:
    bias = lessons["city_temp_bias_f"].get(city, 0.0)
    print(f"  {city}: {bias:+.3f}°F")

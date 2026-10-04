#!/usr/bin/env python3
"""Clean the SEA error model by removing threshold-based backfill artifacts."""
import json, math, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_PATH = ROOT / "state" / "error-model.json"

with open(EM_PATH) as f:
    em = json.load(f)

sea = em["cities"]["SEA"]
old_errors = sea["errors"]
old_bias = sea["bias_f"]

# Remove spikes: any error >20°F is a backfill artifact (threshold vs actual)
# Also remove early backfill artifacts where error = 0.0 (from comparing 
# thresholds, not actual forecasts)
clean = [e for e in old_errors if abs(e) < 15.0 and abs(e) > 0.01]
removed = len(old_errors) - len(clean)

print(f"SEA errors: {len(old_errors)} → {len(clean)} (removed {removed} spikes)")
print(f"Spikes removed: {[e for e in old_errors if abs(e) >= 15.0 or abs(e) <= 0.01]}")
print()

# Recompute bias and std from clean data
if clean:
    n = len(clean)
    new_bias = sum(clean) / n
    new_var = sum((e - new_bias) ** 2 for e in clean) / n
    new_std = math.sqrt(max(new_var, 0.25))
    
    print(f"Old: bias={old_bias:+.2f}°F, std={sea['error_std_f']:.1f}°F, n_eff={sea['n_effective']}")
    print(f"New: bias={new_bias:+.2f}°F, std={new_std:.1f}°F, n={n}")
    
    sea["bias_f"] = round(new_bias, 3)
    sea["error_std_f"] = round(new_std, 2)
    sea["n_effective"] = float(n)
    sea["errors"] = clean
else:
    # Reset to defaults
    sea["bias_f"] = 0.0
    sea["error_std_f"] = 2.0
    sea["n_effective"] = 0.0
    sea["errors"] = []

em["cities"]["SEA"] = sea
em["updated_utc"] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()

# Save
with open(EM_PATH, "w") as f:
    json.dump(em, f, indent=2)

print(f"\n✅ SEA error model cleaned and saved")

# Verify
with open(EM_PATH) as f:
    em2 = json.load(f)
sea2 = em2["cities"]["SEA"]
print(f"Verified: bias={sea2['bias_f']:+.2f}°F, std={sea2['error_std_f']:.1f}°F, n={sea2['n_effective']}")

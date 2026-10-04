#!/usr/bin/env python3
"""Compute Brier scores for paper-book closed trades that lack them, then regen dashboard."""
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
PAPER = ROOT / "state" / "paper"

# Load paper book
with open(PAPER / "paper-book.json") as f:
    book = json.load(f)

# Load brier-log
with open(PAPER / "brier-log.jsonl") as f:
    brier_entries = [json.loads(l) for l in f if l.strip()]

settled_count = 0
for c in book.get("closed", []):
    if not isinstance(c, dict):
        continue
    ticker = c.get("ticker", "")
    side = c.get("side", "")
    qty = c.get("qty", 0)
    pnl = c.get("pnl_cents", 0)
    
    # Determine outcome: win → outcome matches side, loss → outcome opposite
    if pnl > 0:
        outcome = (side == "yes")  # True = YES won, False = NO won
    else:
        outcome = (side != "yes")
    
    # Find matching brier entry
    matches = [e for e in brier_entries if e.get("ticker") == ticker 
               and e.get("side") == side and e.get("qty") == qty
               and e.get("status") == "open"]
    
    if not matches:
        print(f"  ⚠️ No open brier entry for {ticker} {side} qty={qty}")
        continue
    
    for m in matches:
        if m.get("status") != "open":
            continue
        
        our_prob_yes = float(m.get("our_prob", 0))
        market_prob_side = float(m.get("market_prob", 0))
        outcome_int = 1 if outcome else 0  # not used directly per 2026-06-01 fix
        
        # Compute prob_for_outcome (see trader/brier.py record_outcome)
        if outcome:  # YES won
            our_prob_for_outcome = our_prob_yes
            market_prob_for_outcome = market_prob_side if side == "yes" else (1.0 - market_prob_side)
        else:  # NO won
            our_prob_for_outcome = 1.0 - our_prob_yes
            market_prob_for_outcome = (1.0 - market_prob_side) if side == "yes" else market_prob_side
        
        # Brier: score probability of what happened against 1.0 (it DID happen)
        our_brier = (our_prob_for_outcome - 1.0) ** 2
        market_brier = (market_prob_for_outcome - 1.0) ** 2
        
        # Update the entry in-place
        m["status"] = "settled"
        m["outcome"] = "yes" if outcome else "no"
        m["actual"] = 1.0 if outcome else 0.0
        m["our_brier"] = round(our_brier, 6)
        m["market_brier"] = round(market_brier, 6)
        m["our_prob_for_outcome"] = round(our_prob_for_outcome, 6)
        m["market_prob_for_outcome"] = round(market_prob_for_outcome, 6)
        m["settled_at_utc"] = datetime.now(timezone.utc).isoformat()
        
        settled_count += 1
        print(f"  ✅ {ticker[-30:]:35s} {side:3s} outcome={'YES' if outcome else 'NO ':5s} "
              f"our_brier={our_brier:.4f} mkt_brier={market_brier:.4f}")

# Write updated brier-log
with open(PAPER / "brier-log.jsonl", "w") as f:
    for e in brier_entries:
        f.write(json.dumps(e) + "\n")

print(f"\nSettled {settled_count} brier entries")
print(f"Brier-log: {len(brier_entries)} total, {sum(1 for e in brier_entries if e.get('status')=='settled')} settled")

# ── Now fix BrierLogger to respect STATE_DIR ──
brier_py = ROOT / "trader" / "brier.py"
with open(brier_py) as f:
    code = f.read()

# Check if it already respects state dir
if "KALSHI_WEATHER_STATE_DIR" not in code:
    # Add STATE_DIR support
    old = '''def _brier_path() -> Path:
    p = Path.home() / ".openclaw/workspace/automations/kalshi-weather/state/brier-log.jsonl"
    return p'''
    new = '''def _brier_path() -> Path:
    base = os.environ.get("KALSHI_WEATHER_STATE_DIR",
          str(Path.home() / ".openclaw/workspace/automations/kalshi-weather/state"))
    p = Path(base) / "brier-log.jsonl"
    return p'''
    code = code.replace(old, new)
    
    # Need os import
    if "import os" not in code:
        code = code.replace("import json\n", "import json\nimport os\n")
    
    with open(brier_py, "w") as f:
        f.write(code)
    print(f"\n✅ Fixed BrierLogger to respect KALSHI_WEATHER_STATE_DIR")
else:
    print(f"\n✅ BrierLogger already respects KALSHI_WEATHER_STATE_DIR")

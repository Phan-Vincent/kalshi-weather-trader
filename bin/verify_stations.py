#!/usr/bin/env python3
"""bin/verify_stations.py — verify our STATIONS identity vs Kalshi's settlement source (audit 2026-07-06).

Finding #8: we ASSUME STATIONS[city] (lat/lon + name, e.g. HOU=Houston Hobby) is the station
Kalshi settles that series against — never checked against Kalshi's own rules. A wrong station is
a permanent multi-°F directional bias the model can never learn away (climatology KDE, bias EWMA,
every forecast anchored to the wrong sensor). Kalshi's rules_secondary names the authoritative
source explicitly, e.g.:
    "Data for CLIHOU ... choosing the location \"Houston-Hobby, TX\" ... Daily Climate Report"

This fetches one live market per city, extracts Kalshi's named location + CLI product code from
rules_secondary, and flags any that don't corroborate our STATIONS name. Read-only.

Usage:  python3 bin/verify_stations.py [--json]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from data.weather_data import STATIONS  # noqa: E402
from settle_paper import fetch_market   # noqa: E402


def _sample_ticker_per_city() -> dict:
    """One representative ticker per city, harvested from the settlement log."""
    log = ROOT / "state" / "paper" / "settlement-log.jsonl"
    out: dict = {}
    if not log.exists():
        return out
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tk = json.loads(line).get("ticker", "")
        except json.JSONDecodeError:
            continue
        m = re.match(r"^KX(?:HIGHT|LOWT|HIGH|LOW|TEMP)([A-Z]+)-", tk)
        if m:
            out.setdefault(m.group(1), tk)  # first seen per city code
    return out


def _kalshi_location(rules: str):
    """Extract (named_location, cli_code) from rules_secondary, or (None, None)."""
    loc = re.search(r'location\s+"([^"]+)"', rules or "")
    cli = re.search(r"\bCLI([A-Z]{2,4})\b", rules or "")
    return (loc.group(1) if loc else None), (cli.group(1) if cli else None)


def _station_verdict(our_station: str, our_name: str, kalshi_loc: str, cli: str) -> str:
    """The CLI product code is the AUTHORITATIVE discriminator: it is the 3-letter airport
    identifier of the exact sensor Kalshi settles on. Compare it to our station code (KDAL→DAL).
    A name-token match is only a weak fallback when Kalshi's rules carry no CLI code — a loose
    name match falsely passed 'Dallas Love Field' (KDAL) against Kalshi 'Dallas/Fort Worth'
    (CLIDFW), two airports ~15mi apart that settle several °F apart."""
    our_code = re.sub(r"[^A-Z]", "", (our_station or "").upper())
    our_code = our_code[1:] if our_code.startswith("K") and len(our_code) == 4 else our_code
    if cli:
        return "ok" if cli.upper() == our_code else "MISMATCH"
    # No CLI code in the rules → fall back to a loose location-name token match.
    def toks(s):
        return {t for t in re.split(r"[^a-z]+", (s or "").lower()) if len(t) >= 3
                and t not in {"the", "intl", "international", "airport", "field"}}
    return "ok" if (toks(our_name) & toks(kalshi_loc)) else "MISMATCH"


def verify(alias_map: dict) -> dict:
    tickers = _sample_ticker_per_city()
    rows = []
    for city, meta in sorted(STATIONS.items()):
        # resolve which ticker-city maps here (NYCH/NY → NYC handled by aliases)
        tk = tickers.get(city)
        if tk is None:
            for tcity, alias in alias_map.items():
                if alias == city and tcity in tickers:
                    tk = tickers[tcity]
                    break
        if tk is None:
            rows.append({"city": city, "status": "no_sample", "our_name": meta.get("name")})
            continue
        m = fetch_market(tk)
        time.sleep(0.2)
        loc, cli = _kalshi_location((m or {}).get("rules_secondary", "")) if m else (None, None)
        if not loc:
            rows.append({"city": city, "status": "no_rules", "our_name": meta.get("name"), "ticker": tk})
            continue
        status = _station_verdict(meta.get("station", ""), meta.get("name", ""), loc, cli)
        rows.append({
            "city": city, "status": status,
            "our_station": meta.get("station"), "our_name": meta.get("name"),
            "kalshi_location": loc, "cli": cli, "ticker": tk,
        })
    return {"rows": rows,
            "mismatches": [r for r in rows if r["status"] == "MISMATCH"],
            "unchecked": [r for r in rows if r["status"] in ("no_sample", "no_rules")]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        from data.weather_data import CITY_ALIASES as alias_map
    except Exception:
        alias_map = {}
    res = verify(alias_map)
    if args.json:
        print(json.dumps(res, indent=2))
        return 1 if res["mismatches"] else 0
    print("STATION IDENTITY — our STATIONS vs Kalshi rules_secondary settlement source\n")
    for r in res["rows"]:
        mark = {"ok": "✅", "MISMATCH": "❌", "no_sample": "· ", "no_rules": "? "}.get(r["status"], "?")
        extra = f" vs Kalshi \"{r.get('kalshi_location')}\" (CLI{r.get('cli')})" if r.get("kalshi_location") else f" [{r['status']}]"
        print(f"  {mark} {r['city']:6} {r.get('our_name',''):28}{extra}")
    if res["mismatches"]:
        print(f"\n❌ {len(res['mismatches'])} station(s) may not match Kalshi's settlement source — investigate.")
    else:
        print(f"\n✅ No station mismatches ({len(res['unchecked'])} unchecked for lack of a sample/rules).")
    return 1 if res["mismatches"] else 0


if __name__ == "__main__":
    sys.exit(main())

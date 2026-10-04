#!/usr/bin/env python3
"""NYC live coverage (2026-06-25): parse_market_ticker must normalize the ticker city to the
climatology/station key (KXHIGHNY → "NY" → "NYC"). Without it the live/climatology lookup misses NYC
and the live arm silently skips every NYC market (was 12 records priced=0). Also locks the audit that
every traded series' parsed city resolves in climatology. Runs under plain python3."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from data.weather_data import parse_market_ticker  # noqa: E402


def test_ny_ticker_aliases_to_nyc():
    assert parse_market_ticker("KXHIGHNY-26JUN25-B81.5")["city_code"] == "NYC"
    assert parse_market_ticker("KXHIGHTSFO-26JUN25-B69.5")["city_code"] == "SFO"   # non-aliased unchanged
    assert parse_market_ticker("KXLOWTNYC-26JUN25-T60")["city_code"] == "NYC"      # explicit NYC resolves


def test_traded_series_resolve_climatology():
    # Every series the live arm trades must parse to a climatology-covered city, or the live arm
    # silently skips it (the NYC bug). Representative subset incl. both NY-aliased and explicit-NYC.
    from model.fair_value import _get_climo
    climo = _get_climo()
    series = ("KXHIGHNY KXLOWTNYC KXHIGHTSFO KXHIGHMIA KXHIGHTNOLA KXHIGHPHIL KXHIGHTDC "
              "KXHIGHTSEA KXHIGHTPHX KXHIGHCHI KXHIGHTBOS KXHIGHLAX").split()
    unresolved = [s for s in series
                  if not climo.has_city(parse_market_ticker(s + "-26JUN25-B70.5")["city_code"])]
    assert unresolved == [], f"traded series with no climatology after alias: {unresolved}"


if __name__ == "__main__":
    test_ny_ticker_aliases_to_nyc(); print("[PASS] KXHIGHNY → NYC (non-aliased cities unchanged)")
    test_traded_series_resolve_climatology(); print("[PASS] traded series resolve climatology")
    print("\nTicker city-alias tests pass.")

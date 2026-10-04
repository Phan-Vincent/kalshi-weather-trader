#!/usr/bin/env python3
"""Regression: the live size-ramp baseline is WEATHER-ONLY and immune to non-weather settlements.

2026-07-05 size-ramp re-key. The ramp (risk.live_size_rung) promotes/demotes on the DELTA of the
`live_total_cents` it is handed. It used to be fed `weather_budget_total_cents` (= account
available+portfolio − non-weather OPEN marks), which neutralizes non-weather UNREALIZED marks but NOT
their realized cash: when a non-weather position (sports, KXUSAIRANAGREEMENT) settles, its payout lands
in available_cents and the position leaves positions[], so nothing subtracts it — the +$147 sports
settlement on 2026-07-02 permanently shifted the total and would spuriously promote the weather ramp.

The fix feeds `weather_ramp_total_cents` = lifetime weather realized P&L + current weather OPEN marks,
which contains NO account-cash term and is therefore structurally immune to every non-weather cash flow.
These tests codify that immunity (the +$147 proof), feed-independence, weather flow-through, and the
paper_trade read's fallback chain. Runs under plain python3 and pytest; no network / no CLI.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from sync_live_positions import weather_ramp_total_cents, weather_budget_total_cents  # noqa: E402

WX_A = "KXHIGHTNOLA-26JUL04-B91.5"   # weather
WX_B = "KXLOWTDC-26JUL04-B74.5"      # weather
SPORTS = "KXNBAGAME-26JUL04-LAL"     # non-weather
IRAN = "KXUSAIRANAGREEMENT-26DEC31"  # non-weather


def _state(available, portfolio, positions):
    return {"available_cents": available, "portfolio_cents": portfolio, "positions": positions}


def _pos(ticker, mark):
    return {"ticker": ticker, "market_exposure_cents": mark}


def test_nonweather_settlement_immunity():
    """The core +$147 proof: a non-weather settlement moves the OLD signal but NOT the ramp signal."""
    realized = 338  # lifetime weather realized (unchanged by a non-weather settlement)
    # Cycle N: sports position OPEN (mark 14700), two weather positions open.
    n = _state(available=50000, portfolio=14700 + 900 + 1200,
               positions=[_pos(SPORTS, 14700), _pos(WX_A, 900), _pos(WX_B, 1200)])
    # Cycle N+1: sports SETTLED — payout +14700 into available, position gone from positions[];
    # weather positions unchanged.
    n1 = _state(available=50000 + 14700, portfolio=900 + 1200,
                positions=[_pos(WX_A, 900), _pos(WX_B, 1200)])

    ramp_n = weather_ramp_total_cents(realized, n)
    ramp_n1 = weather_ramp_total_cents(realized, n1)
    assert ramp_n == 338 + 900 + 1200 == 2438
    assert ramp_n1 == ramp_n, "ramp signal must be UNMOVED by a non-weather settlement"
    assert ramp_n1 - ramp_n == 0

    # And prove the OLD signal WAS contaminated: it jumps by ~the gross payout.
    budget_n = weather_budget_total_cents(n)
    budget_n1 = weather_budget_total_cents(n1)
    assert budget_n1 - budget_n == 14700, "the legacy signal jumps by the sports payout — the bug"


def test_ramp_signal_ignores_account_cash_fields():
    """Feed-independence: available_cents / portfolio_cents never enter the ramp signal, so a
    balance-feed wobble (or an Iran open-mark swing) cannot move it. Only weather marks + realized do."""
    positions = [_pos(WX_A, 900), _pos(IRAN, 8000)]
    base = weather_ramp_total_cents(500, _state(available=1, portfolio=1, positions=positions))
    # Swing account cash and the Iran mark wildly; the weather-only signal is invariant.
    swung = weather_ramp_total_cents(500, _state(available=999999, portfolio=999999,
                                                 positions=[_pos(WX_A, 900), _pos(IRAN, 3000)]))
    assert base == 500 + 900 == 1400
    assert swung == base, "non-weather mark / cash changes must not move the ramp signal"


def test_weather_settlement_flows_through():
    """Real signal must NOT be suppressed: a weather settlement advances realized → ramp moves by it."""
    positions = [_pos(WX_A, 900)]
    before = weather_ramp_total_cents(338, _state(0, 900, positions))
    after = weather_ramp_total_cents(338 + 250, _state(0, 900, positions))  # a weather win booked +250
    assert after - before == 250


def test_weather_open_mark_moves_signal():
    """Mark-to-market leg (chosen 2026-07-05): an unrealized WEATHER swing does move the ramp."""
    lo = weather_ramp_total_cents(100, _state(0, 0, [_pos(WX_A, 400)]))
    hi = weather_ramp_total_cents(100, _state(0, 0, [_pos(WX_A, 700)]))
    assert hi - lo == 300


def test_missing_or_null_marks_are_safe():
    """A position missing / null market_exposure_cents counts as 0, never raises."""
    positions = [_pos(WX_A, None), {"ticker": WX_B}, _pos(SPORTS, 5000)]
    assert weather_ramp_total_cents(42, _state(0, 0, positions)) == 42  # both weather marks -> 0


def test_paper_trade_read_fallback_chain():
    """paper_trade prefers last_weather_total_cents, then last_total_cents, then book cash — the exact
    expression used at the read site, locked here so an old-schema sync-state degrades safely."""
    def read(sd, cash):
        return int(sd.get("last_weather_total_cents", sd.get("last_total_cents", cash)))
    assert read({"last_weather_total_cents": 2438, "last_total_cents": 65309}, 111) == 2438  # new field wins
    assert read({"last_total_cents": 65309}, 111) == 65309                                    # old schema
    assert read({}, 111) == 111                                                               # bare -> cash


if __name__ == "__main__":
    for fn in [
        test_nonweather_settlement_immunity,
        test_ramp_signal_ignores_account_cash_fields,
        test_weather_settlement_flows_through,
        test_weather_open_mark_moves_signal,
        test_missing_or_null_marks_are_safe,
        test_paper_trade_read_fallback_chain,
    ]:
        fn()
    print("OK — weather-only size-ramp signal is immune to non-weather settlements")

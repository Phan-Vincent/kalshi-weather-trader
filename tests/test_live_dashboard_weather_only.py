"""Weather-only equity regression checks. All numeric inputs are synthetic."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# generate_live_data.py runs account queries at import (module-level script), so load it as a
# spec WITHOUT executing top-level — grab just the pure helpers via exec of the function defs.
# Simplest robust approach: import the module object with a guard is not available, so we load
# the source and exec only the two helper defs into a namespace.
_SRC = (ROOT / "bin" / "generate_live_data.py").read_text()


def _load_helpers():
    # Provide __file__ so the module's top-level ROOT = dirname(dirname(abspath(__file__)))
    # resolves; the sliced source runs its own imports (json/os/datetime/...).
    ns = {"__file__": str(ROOT / "bin" / "generate_live_data.py")}
    src = _SRC
    # Execute only up to the first top-level statement after the helper defs (the module's
    # script body begins at 'positions_raw = run_kalshi(...)'). Slice there so no network runs.
    cut = src.index("positions_raw = run_kalshi")
    exec(compile(src[:cut], "generate_live_data_helpers", "exec"), ns)
    return ns


HELPERS = _load_helpers()
compute_weather_equity = HELPERS["compute_weather_equity"]


def test_excludes_sports_cash_headline():
    """Synthetic ledger inputs produce positive weather-only P&L."""
    initial = 100000
    realized = 1250          # synthetic realized P&L
    open_cost = 4000        # synthetic open cost
    weather_portfolio = 4500  # synthetic market value
    avail, total, pnl, pnl_pct = compute_weather_equity(initial, realized, open_cost, weather_portfolio)
    assert avail == initial + realized - open_cost      # weather cash, no sports
    assert total == avail + weather_portfolio
    assert 0 < pnl < 20                                 # ~+$8, nowhere near +$140
    assert pnl == round((total - initial) / 100, 2)


def test_pnl_is_realized_plus_unrealized():
    """pnl (dollars) must equal weather realized + weather unrealized, independent of any
    account-global cash. A +$150 sports settlement changes NONE of these inputs, so it cannot move pnl."""
    initial, realized, open_cost, wpf = 100000, 1250, 4000, 4500
    _, _, pnl, _ = compute_weather_equity(initial, realized, open_cost, wpf)
    unrealized = wpf - open_cost
    expected = round((realized + unrealized) / 100, 2)
    assert pnl == expected


def test_negative_weather_pnl_is_reported():
    """A genuine weather loss (realized -$50, open positions marked below cost) shows negative,
    not masked by any account cash."""
    avail, total, pnl, pnl_pct = compute_weather_equity(100000, -5000, 2000, 1000)
    assert pnl < 0
    assert pnl_pct < 0


def test_zero_initial_no_divide_by_zero():
    avail, total, pnl, pnl_pct = compute_weather_equity(0, 100, 0, 0)
    assert pnl_pct == 0


def test_data_live_json_has_weather_keys_after_generation():
    """If the dashboard JSON exists, it must carry the weather-scoped display keys the HTML reads."""
    import json
    p = ROOT / "dashboard" / "data-live.json"
    if not p.exists():
        return  # generator not yet run in this environment; nothing to assert
    d = json.loads(p.read_text())
    for k in ("weather_available_dollars", "weather_deployed_dollars", "weather_total_dollars",
              "weather_pnl_dollars", "weather_pnl_pct"):
        assert k in d, f"missing weather display key {k}"


def test_generator_uses_canonical_filter_not_substring():
    """The dashboard's position/portfolio filter must be the canonical is_weather_ticker, so FUTURE
    personal bets whose ticker merely contains HIGH/LOW/TEMP as a substring can't leak into the
    displayed panel (KXFEDLOWER, KXALLTIMEHIGH, KXHIGHESTGROSSING, KXHIGHCOURT, KXLOWES, ...).
    Guards against a regression back to the old inline substring test."""
    src = _SRC
    assert "is_weather_ticker(ticker)" in src, "generator must filter positions with is_weather_ticker"
    assert 'any(tag in ticker for tag in ("HIGH", "LOW", "TEMP"))' not in src, \
        "leaky substring filter must not return"

    from data.weather_data import is_weather_ticker
    # Personal-bet substring traps — must be EXCLUDED from the weather panel.
    for t in ("KXFEDLOWER-26SEP-YES", "KXALLTIMEHIGH-26-SPX", "KXHIGHESTGROSSING-27-BARBIE",
              "KXHIGHCOURT-26-RULING", "KXLOWES-26Q4-EARN", "KXCONTEMPT-26-CONGRESS",
              "KXUSAIRANAGREEMENT-27-26SEP", "KXNFLGAME-26-BUF", "KXPRES-28-GOP", "KXBTC-26DEC31-T100000"):
        assert not is_weather_ticker(t), f"{t} must NOT be treated as weather"
    # Real weather forms the account actually trades — must be INCLUDED.
    for t in ("KXHIGHTNOLA-26JUL04-B91.5", "KXHIGHMIA-26JUL04-B90.5", "KXHIGHNY-26JUL04-T88",
              "KXLOWTHOU-26JUL04-T72"):
        assert is_weather_ticker(t), f"{t} must be treated as weather"

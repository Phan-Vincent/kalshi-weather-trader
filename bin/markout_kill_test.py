#!/usr/bin/env python3
"""bin/markout_kill_test.py — fast structural kill-test for the premium-live edge.

Quant review 2026-07-01 experiment #5. Adverse selection (post-fill markout) is a STRUCTURAL cost
that's measurable with far less data than the full edge, so it's a fast way to falsify the strategy:

  - REALIZED net EV/ct (fee-adjusted, EVENT-CLUSTERED): if its upper CI is < 0, the edge is a
    confirmed loser and no sample size rescues it → KILL.
  - ADVERSE MARKOUT vs CAPTURED HALF-SPREAD: the premium arm rests at the join-bid to capture ~half
    the spread; if short-horizon adverse markout reliably exceeds that captured half-spread, the
    edge is structurally net-negative → KILL. (The half-spread comes from the P0b
    `spread_cents_at_post` telemetry, which only began accruing 2026-07-01; N/A until it fills in.)

Weather-only (excludes the KXUSAIRANAGREEMENT commingle) and attributed to OUR posted quotes.
Read-only report — never trades, never halts.

Usage:  python3 bin/markout_kill_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from live_fill_quality import (  # noqa: E402  (reuse the canonical window/attribution + realized edge)
    _read_jsonl, _our_posted_tickers, _mean_markout, _realized_edge,
    MARKOUT_LO_H, MARKOUT_HI_H,
)
from compare_variants import _event, cluster_bootstrap_ci, asymp_confseq   # noqa: E402
from data.weather_data import is_weather_ticker              # noqa: E402

STATE_DIR = ROOT / "state" / "live-premium"


def markout_items_by_event(mk: list[dict], our_tickers) -> list[tuple]:
    """Per-position short-horizon markout keyed by event — mirrors live_fill_quality._mean_markout's
    [LO,HI] window + closest-to-centre dedup + garbage-price filters, but weather-only and returning
    (event, drift) so we can event-cluster a CI (the helper returns only mean+n)."""
    target = (MARKOUT_LO_H + MARKOUT_HI_H) / 2.0
    chosen: dict = {}
    for r in mk:
        if r.get("source") == "sync_fill_detect":   # at-fill diagnostic snaps — never a live instrument
            continue
        cm, fp, age, tk = r.get("cur_mid"), r.get("fill_px"), r.get("age_hours"), r.get("ticker")
        if not (isinstance(cm, (int, float)) and isinstance(fp, (int, float)) and isinstance(age, (int, float))):
            continue
        if not (1 <= fp <= 99) or not (2 <= cm <= 98):        # garbage entry / settlement-rail mid
            continue
        if not (MARKOUT_LO_H <= age <= MARKOUT_HI_H):          # short-horizon only
            continue
        if not is_weather_ticker(tk or ""):                    # exclude Iran / non-weather
            continue
        if our_tickers is not None and tk not in our_tickers:  # our posted quotes only
            continue
        key = (tk, r.get("side"), r.get("paper_order_id") or r.get("opened_utc"))
        dist = abs(age - target)
        if key not in chosen or dist < chosen[key][0]:
            chosen[key] = (dist, _event(tk or ""), cm - fp)
    return [(ev, drift) for _, ev, drift in chosen.values()]


def _event_mean_items(items: list[tuple]) -> list[tuple]:
    """Reduce per-position [(event, value), …] to ONE (event, event_mean) per event, so the
    event is the sample unit for BOTH the bootstrap CI and the anytime CS (a concentrated few
    events must not dominate via trade count). Keeps the (event, value) shape both consumers
    expect. (audit 2026-07-06 estimand alignment)"""
    from collections import defaultdict
    g: dict = defaultdict(list)
    for ev, v in items:
        g[ev].append(v)
    return [(ev, sum(vs) / len(vs)) for ev, vs in g.items()]


def net_edge_items_by_event(settle: list[dict]) -> list[tuple]:
    """Per-position NET ¢/ct keyed by event, weather-only, attributed.

    pnl_cents is ALREADY net of fees/rebate (Kalshi net realized P&L). The old code
    re-subtracted fee_per_contract_cents(maker=True) — a NEGATIVE rebate — double-crediting
    the rebate and inflating net EV, which could suppress a valid KILL (audit 2026-07-06)."""
    items = []
    for r in settle:
        if (r.get("qty") or 0) <= 0 or r.get("entry_cents") in (None, 0):
            continue
        if not is_weather_ticker(r.get("ticker", "")):
            continue
        net = r["pnl_cents"] / r["qty"]
        items.append((_event(r["ticker"]), net))
    return items


def captured_half_spread(life: list[dict]) -> tuple:
    """Mean captured half-spread (¢) from the P0b posted_live book context, weather-only."""
    vals = [r["spread_cents_at_post"] / 2.0 for r in life
            if r.get("event") == "posted_live"
            and isinstance(r.get("spread_cents_at_post"), (int, float))
            and r.get("spread_cents_at_post") > 0
            and is_weather_ticker(r.get("ticker", ""))]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


def _fmt(pt, lo, hi):
    return "n/a" if pt is None else f"{pt:+.2f}¢ [{lo:+.2f}, {hi:+.2f}]"


def evaluate(state_dir: Path = None) -> dict:
    sd = state_dir or STATE_DIR
    mk = _read_jsonl(sd / "markout-log.jsonl")
    settle = _read_jsonl(sd / "settlement-log.jsonl")
    life = _read_jsonl(sd / "maker-lifecycle.jsonl")
    # Attribute markout to OUR posted weather tickers only. Pass the set AS-IS — never coerce an
    # empty set to None: _mean_markout / markout_items_by_event treat None as "attribute ALL rows",
    # which would re-admit the non-weather KXUSAIRANAGREEMENT commingle _our_posted_tickers exists
    # to exclude. Empty set ⇒ nothing attributed ⇒ markout reported n/a (mirrors live_paper_gap.py).
    our = _our_posted_tickers(sd)

    mk_items = markout_items_by_event(mk, our) if our else []
    mo = cluster_bootstrap_ci(mk_items) if mk_items else (None, None, None)
    mo_ref, mo_n = (_mean_markout(mk, our_tickers=our) if (mk and our) else (None, 0))  # canonical cross-check

    net_items = net_edge_items_by_event(settle)
    # ESTIMAND ALIGNMENT (audit 2026-07-06): the displayed CI and the KILL trigger must estimate
    # the SAME thing. asymp_confseq reduces to per-EVENT means (event = independent unit), but
    # cluster_bootstrap_ci's POINT is the pooled per-TRADE mean (events with more contracts count
    # more). On a concentrated book those diverged in sign, so the CI shown to the operator did
    # not match the number the KILL keyed on. Reduce to one per-event mean and feed BOTH — now
    # the bootstrap CI, the CS, and the verdict all share the per-event-mean estimand.
    ev_mean_items = _event_mean_items(net_items)
    ev = cluster_bootstrap_ci(ev_mean_items) if ev_mean_items else (None, None, None)
    # Anytime-valid CS on net EV/ct: this tool runs EVERY cron cycle, so peeking at the fixed-n
    # bootstrap CI and killing the first time it excludes 0 is an invalid multiple-looks test. The
    # CS is valid under continuous monitoring — so the net-EV KILL keys on it, not the bootstrap CI.
    ev_cs = asymp_confseq(ev_mean_items) if ev_mean_items else (None, None, None)
    n_events = len({e for e, _ in net_items})
    re = _realized_edge([s for s in settle if is_weather_ticker(s.get("ticker", ""))]) or {}

    hs_mean, hs_n = captured_half_spread(life)

    # Verdict
    ev_pt = ev[0]
    mo_pt, mo_lo, mo_hi = mo
    ecs_pt, ecs_lo, ecs_hi = ev_cs
    ev_cs_kill = ecs_hi is not None and ecs_lo != ecs_hi and ecs_hi < 0
    if ev_cs_kill:
        verdict = ("KILL — net EV/ct anytime-valid CS upper bound < 0 (losing under continuous "
                   "monitoring; no sample size rescues it)")
    elif mo_hi is not None and hs_mean is not None and mo_hi < -hs_mean:
        verdict = "KILL — adverse markout exceeds captured half-spread (structurally net-negative)"
    elif ev_pt is None:
        verdict = "INSUFFICIENT DATA — no attributed weather fills yet"
    else:
        verdict = f"INCONCLUSIVE — net EV/ct anytime CS spans 0 (n_events={n_events}); keep collecting"
    return {
        "markout_short_horizon": {"ci": mo, "n_positions": len(mk_items), "ref_mean": mo_ref, "ref_n": mo_n},
        "net_ev_per_ct": {"ci": ev, "cs": ev_cs, "n_events": n_events, "n_fills": len(net_items)},
        "realized_edge_helper": re,
        "captured_half_spread": {"mean_c": hs_mean, "n": hs_n},
        "verdict": verdict,
    }


def main() -> int:
    r = evaluate()
    mo = r["markout_short_horizon"]; ev = r["net_ev_per_ct"]; hs = r["captured_half_spread"]
    print("[markout-kill-test] premium-live, weather-only, attributed to our posted quotes")
    print(f"  short-horizon markout ({MARKOUT_LO_H:.0f}-{MARKOUT_HI_H:.0f}h): "
          f"{_fmt(*mo['ci'])}  (n_positions={mo['n_positions']}, negative=adverse selection)")
    print(f"  realized net EV/ct (fee-adjusted, event-clustered): {_fmt(*ev['ci'])}  "
          f"(n_events={ev['n_events']}, n_fills={ev['n_fills']})")
    print(f"    anytime-valid CS (monitoring-safe kill trigger): {_fmt(*ev['cs'])}")
    if hs["mean_c"] is None:
        print("  captured half-spread: N/A — P0b spread_cents_at_post telemetry accruing (0 weather rows)")
    else:
        print(f"  captured half-spread: {hs['mean_c']:+.2f}¢ (n={hs['n']})")
    print(f"  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

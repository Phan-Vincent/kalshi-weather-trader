#!/usr/bin/env python3
"""bin/compare_variants.py — Significance-aware comparison across A/B arms.

Currently the parallel paper streams are eyeballed by raw realized P&L on
different bankrolls / start dates. This reads each arm's logs and reports, per arm:

  n, win rate, total P&L, P&L per contract with a bootstrap 95% CI, mean Brier,
  and Brier skill score vs market (BSS = 1 - mean(our)/mean(market)) with a
  paired-bootstrap 95% CI + z on (market_brier - our_brier).

A CI on per-trade P&L that excludes 0 ⇒ the arm's edge is distinguishable from
noise at 95%. A BSS CI that excludes 0 ⇒ the model beats the market's prices.

Optionally `--pairwise A,B` matches the two arms on (ticker, close_time), sums
P&L per market for each, inner-joins, and bootstraps the per-market difference so
common market noise cancels — the clean way to say "arm A > arm B".

Inputs (per arm): state/<dir>/settlement-log.jsonl, state/<dir>/brier-log.jsonl
Stdlib only (matches the rest of the repo).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
B_DEFAULT = 5000
# Minimum matched events for an arm to enter the Westfall–Young multiplicity FAMILY (2026-07-13 audit).
# Arms below this have too few events for a stable clustered SE; under event-resampling their |t| goes
# degenerate-large and dominates the max-t null, inflating every arm's adj_p toward 1. 20 matches the
# pre-registered decision floor used elsewhere (the >=20-event kill trigger; the futility checkpoint).
MIN_WY_EVENTS = int(os.environ.get("KALSHI_WEATHER_WY_MIN_EVENTS", "20"))


# ── io ───────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _dedupe_settlements(rows: list[dict]) -> list[dict]:
    """Drop duplicate settlement rows by paper_order_id (first occurrence wins). Settle passes
    double-appended rows sub-second apart on 2026-07-09/10 (a retry/race during cron-decoupling),
    inflating arm P&L and n in the dashboard standings by ~30% (2026-07-13 audit). A paper_order_id
    is unique per settled position, so this is safe; rows without one (e.g. the live book, which was
    never duplicated) pass through untouched. Applied at every settlement reader so a future
    recurrence can't silently re-inflate the standings the 7/15 review quotes."""
    seen = set()
    out = []
    for r in rows:
        oid = r.get("paper_order_id")
        if oid is not None:
            if oid in seen:
                continue
            seen.add(oid)
        out.append(r)
    return out


def _read_settlements(state_dir: Path) -> list[dict]:
    """settlement-log.jsonl for an arm, deduped (see _dedupe_settlements)."""
    return _dedupe_settlements(_read_jsonl(state_dir / "settlement-log.jsonl"))


def _arms_from_config(cfg_path: Path) -> list[tuple[str, str]]:
    """Return [(name, dir), ...] from variants.json (all arms, enabled or not)."""
    if not cfg_path.is_file():
        return []
    cfg = json.loads(cfg_path.read_text())
    return [(v["name"], v["dir"]) for v in cfg.get("variants", [])]


def _enabled_arm_names(cfg_path: Path) -> set:
    """Names of ENABLED variants — the concurrently-active experiment arms. Deprecated/disabled arms
    accrue no new data yet were being swept into the Westfall–Young multiplicity family, where their
    tiny-event members produced degenerate huge |t| that dominated the max-t null and pushed EVERY
    arm's adj_p toward 1 (an un-passable ruling; 2026-07-13 audit)."""
    if not cfg_path.is_file():
        return set()
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        return set()
    return {v["name"] for v in cfg.get("variants", []) if v.get("enabled")}


# ── stats (stdlib bootstrap) ─────────────────────────────────────────

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_ci(data: list[float], stat=_mean, B: int | None = None, alpha: float = 0.05, seed: int = 1):
    """Percentile bootstrap CI for `stat` over `data`. Returns (point, lo, hi)."""
    if not data:
        return (None, None, None)
    if B is None:
        B = B_DEFAULT
    rng = random.Random(seed)
    n = len(data)
    point = stat(data)
    samples = []
    for _ in range(B):
        resample = [data[rng.randrange(n)] for _ in range(n)]
        samples.append(stat(resample))
    samples.sort()
    lo = samples[int((alpha / 2) * B)]
    hi = samples[min(B - 1, int((1 - alpha / 2) * B))]
    return (point, lo, hi)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _event(ticker: str) -> str:
    """The independent unit for clustering: the weather EVENT (series+city+date), e.g.
    'KXHIGHTSFO-26JUN26' — all its bins (B66.5, B68.5, …) settle on the SAME realized temperature, so
    their P&Ls are correlated and a bin ticker is NOT independent. Drops the bin threshold (last part).
    Treating bins as independent understates every CI (validation 2026-06-28)."""
    parts = (ticker or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else (ticker or "")


def cluster_bootstrap_ci(items: list[tuple], B: int | None = None, alpha: float = 0.05, seed: int = 3,
                         method: str = "bca"):
    """Bootstrap CI of the MEAN, clustering on the event so within-event correlation doesn't
    understate variance. `items` = [(event_key, value), …]; resamples EVENTS with replacement and
    expands each chosen event to all its values. Returns (point, lo, hi).

    method="bca" (default — 2026-07-01 quant fix): bias-corrected & accelerated. The percentile
    method measured only ~64% empirical coverage vs nominal 95% on this repo's skewed, few-event
    arm diffs (QA-quant-review §2.2) — anti-conservative, so its "excludes 0" stars overstated
    confidence system-wide. BCa corrects median bias (z0; midrank convention so a heavily tied
    bootstrap distribution degrades to percentile rather than skewing) and skew (acceleration from
    an EVENT-level jackknife, matching the resampling unit). method="percentile" keeps the old
    behavior for comparison/tests."""
    if not items:
        return (None, None, None)
    if B is None:
        B = B_DEFAULT
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for ev, v in items:
        groups[ev].append(v)
    events = list(groups)
    point = _mean([v for _, v in items])
    rng = random.Random(seed)
    k = len(events)
    samples = []
    for _ in range(B):
        vals: list = []
        for _ in range(k):
            vals.extend(groups[events[rng.randrange(k)]])
        samples.append(_mean(vals) if vals else 0.0)
    samples.sort()

    def _pct(q: float) -> float:
        return samples[min(B - 1, max(0, int(q * B)))]

    if method == "percentile" or k < 2 or samples[0] == samples[-1]:
        return (point, _pct(alpha / 2), _pct(1 - alpha / 2))

    # ── BCa ──
    from statistics import NormalDist
    nd = NormalDist()
    # z0: median-bias correction. Midrank tie convention: p0 = (#below + ½·#equal)/B, so a
    # discrete bootstrap distribution with mass AT the point estimate doesn't fake a bias.
    n_lt = sum(1 for s in samples if s < point)
    n_eq = sum(1 for s in samples if s == point)
    p0 = min(max((n_lt + 0.5 * n_eq) / B, 1.0 / (B + 1)), B / (B + 1.0))
    z0 = nd.inv_cdf(p0)
    # acceleration from the event-level jackknife (leave-one-EVENT-out means)
    tot = sum(v for _, v in items)
    n_all = len(items)
    jk = []
    for ev in events:
        g = groups[ev]
        rem = n_all - len(g)
        jk.append((tot - sum(g)) / rem if rem > 0 else point)
    jm = _mean(jk)
    num = sum((jm - t) ** 3 for t in jk)
    den = 6.0 * (sum((jm - t) ** 2 for t in jk) ** 1.5)
    a = (num / den) if den > 0 else 0.0

    def _bca_q(q: float) -> float:
        z = nd.inv_cdf(q)
        denom = 1.0 - a * (z0 + z)
        if denom <= 0:
            return q                       # pathological acceleration → percentile fallback for this bound
        return min(max(nd.cdf(z0 + (z0 + z) / denom), 0.5 / B), 1.0 - 0.5 / B)

    return (point, _pct(_bca_q(alpha / 2)), _pct(_bca_q(1 - alpha / 2)))


def asymp_confseq(items: list[tuple], alpha: float = 0.05, t_opt: int = 100):
    """ANYTIME-VALID (asymptotic) confidence sequence for the EVENT-CLUSTERED mean.

    Unlike a fixed-n CI, a confidence sequence stays valid under CONTINUOUS monitoring — you can peek
    every cycle and call a verdict the INSTANT it excludes 0, with no fixed-n peeking penalty (α is
    controlled time-uniformly). This lets the futility/kill tools stop as soon as the data is
    decisive instead of waiting for a fixed checkpoint. Asymptotic CS of Waudby-Smith/Howard/Ramdas;
    `rho` is pre-tuned (data-independent) so the boundary is tightest near `t_opt` events.

    `items` = [(event_key, value)] → reduced to one per-event mean (events are the independent unit).
    Returns (point, lo, hi). It is WIDER than the fixed-n CI (~1.5x at t_opt) — that width IS the
    price of anytime-validity; treat 'lo>0' / 'hi<0' as an at-any-time decisive signal."""
    from collections import defaultdict
    g: dict = defaultdict(list)
    for ev, v in items:
        g[ev].append(v)
    xs = [sum(vs) / len(vs) for vs in g.values()]
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else None, None, None)
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0.0:
        return (mu, mu, mu)                      # degenerate; callers require lo!=hi to rule decisive
    la = -2.0 * math.log(alpha)
    rho2 = (la + math.log(la + 1.0)) / max(1, t_opt)          # fixed pre-tuned rho^2 (data-independent)
    hw = sd * math.sqrt((2.0 * (n * rho2 + 1.0)) / (n * n * rho2)
                        * math.log(math.sqrt(n * rho2 + 1.0) / alpha))
    return (mu, mu - hw, mu + hw)


# ── per-arm metrics ──────────────────────────────────────────────────

def arm_metrics(state_dir: Path) -> dict:
    settlements = _read_settlements(state_dir)
    brier = [r for r in _read_jsonl(state_dir / "brier-log.jsonl") if r.get("status") == "settled"]

    pnl_cents = [float(s.get("pnl_cents", 0)) for s in settlements]
    attr = [s for s in settlements if (s.get("qty") or 0) > 0]   # gap-settled rows are attribution-less
    tot_qty = sum(float(s.get("qty", 0) or 0) for s in attr)
    # per-trade EV/ct, EVENT-CLUSTERED CI (bins of one weather event are correlated → an i.i.d.
    # per-trade bootstrap is too narrow; validation 2026-06-28).
    pc_items = [(_event(s.get("ticker", "")), float(s.get("pnl_cents", 0)) / s["qty"])
                for s in settlements if s.get("qty")]
    n = len(pnl_cents)
    wins = sum(1 for p in pnl_cents if p > 0)

    m = {
        "n": n,
        "n_events": len({_event(s.get("ticker", "")) for s in settlements}),
        "win_rate": (wins / n * 100) if n else 0.0,
        "total_pnl": sum(pnl_cents) / 100.0,
        "pnl_per_contract_ci": cluster_bootstrap_ci(pc_items, seed=1),     # ¢/ct, per-trade mean, event-clustered
        "pnl_per_contract_wt": (sum(float(s.get("pnl_cents", 0)) for s in attr) / tot_qty) if tot_qty else 0.0,  # contract-weighted over ATTRIBUTED fills only (no CI)
    }

    if brier:
        our = [float(r["our_brier"]) for r in brier]
        mkt = [float(r["market_brier"]) for r in brier]
        diff = [mb - ob for ob, mb in zip(our, mkt)]   # >0 ⇒ we beat market
        m["brier"] = _mean(our)
        m["market_brier"] = _mean(mkt)
        m["bss"] = (1 - _mean(our) / _mean(mkt)) if _mean(mkt) else 0.0
        m["brier_diff_ci"] = bootstrap_ci(diff, seed=2)
        sd = _stdev(diff)
        m["brier_z"] = (_mean(diff) / (sd / math.sqrt(len(diff)))) if sd else 0.0
        m["brier_n"] = len(brier)
    else:
        m["brier"] = m["market_brier"] = m["bss"] = None
        m["brier_diff_ci"] = (None, None, None)
        m["brier_z"] = None
        m["brier_n"] = 0
    return m


# ── pairwise matched-market P&L ──────────────────────────────────────

def _per_market_stats(state_dir: Path, by_ticker_only: bool = False) -> dict:
    """Per market: summed pnl_cents + summed qty (qty needed to normalize for book size). Keyed on the
    ticker (always date-specific = one market); robust when arms log market_close_time inconsistently.

    Also accumulates qty-weighted sums of LEAK-SAFE, PRE-OUTCOME covariates for CUPED (P1b):
      fp    = fair_prob_at_open (the model's view; set at post, before settlement)
      entry = entry_cents       (fill price; set before settlement)
    Both are pre-outcome so they can't leak the result. `fp` is arm-COMMON for execution A/Bs (valid
    control) but ON the causal path for MODEL A/Bs — the variance-report flags over-adjustment (point
    shift) so a covariate that's really the treatment is caught, not trusted."""
    out: dict = {}
    for s in _read_settlements(state_dir):
        if (s.get("qty") or 0) <= 0 or s.get("gap_settled"):
            continue                  # attribution-less gap-settled row → out of the per-contract diff
        tk = s.get("ticker", "")
        key = tk if by_ticker_only else (tk, s.get("market_close_time", ""))
        d = out.setdefault(key, {"pnl": 0.0, "qty": 0.0, "ticker": tk, "_fp_q": 0.0, "_entry_q": 0.0})
        q = float(s.get("qty", 0) or 0)
        d["pnl"] += float(s.get("pnl_cents", 0))
        d["qty"] += q
        fp = s.get("fair_prob_at_open")
        if fp is not None:
            d["_fp_q"] += float(fp) * q
        ec = s.get("entry_cents")
        if ec is not None:
            d["_entry_q"] += float(ec) * q
    # finalize qty-weighted covariate means
    for d in out.values():
        qq = d["qty"] or 1.0
        d["fp"] = d["_fp_q"] / qq
        d["entry"] = d["_entry_q"] / qq
    return out


def pairwise(a_dir: Path, b_dir: Path, by_ticker_only: bool = False, per_contract: bool = True) -> dict:
    """Matched-market diff between two arms. PER-CONTRACT by default — so a book-SIZE difference (e.g. a
    live $5/market cap vs a paper flat size, ~6x) can't masquerade as a strategy difference — and
    EVENT-CLUSTERED — so the correlated bins of one weather event aren't counted as independent. Also
    returns the raw dollar diff + each arm's qty/market so a size mismatch is always visible.
    (Both flaws were real and inverted the verdict in the 2026-06-28 validation.)"""
    a, b = _per_market_stats(a_dir, by_ticker_only), _per_market_stats(b_dir, by_ticker_only)
    keys = sorted(set(a) & set(b), key=str)
    items_pc, items_usd, items_pc_cov = [], [], []
    qa = qb = 0.0
    for k in keys:
        ev = _event(a[k]["ticker"])
        ac, bc = a[k]["qty"], b[k]["qty"]
        if ac > 0 and bc > 0:
            diff = (a[k]["pnl"] / ac) - (b[k]["pnl"] / bc)
            items_pc.append((ev, diff))                                       # ¢/contract diff
            # covariates: arm-average pre-outcome market props (leak-safe); qty ratio for strict-match
            fp = (a[k].get("fp", 0.0) + b[k].get("fp", 0.0)) / 2.0
            entry = (a[k].get("entry", 0.0) + b[k].get("entry", 0.0)) / 2.0
            qratio = (ac / bc) if bc else float("inf")
            items_pc_cov.append((ev, diff, fp, entry, qratio))
        items_usd.append((ev, (a[k]["pnl"] - b[k]["pnl"]) / 100.0))           # $ diff (context only)
        qa += ac; qb += bc
    n = len(keys)
    return {
        "matched_markets": n,
        "matched_events": len({_event(a[k]["ticker"]) for k in keys}),
        "qty_per_mkt_a": (qa / n) if n else 0.0,
        "qty_per_mkt_b": (qb / n) if n else 0.0,
        "per_contract": per_contract,
        "items_pc": items_pc,                                                      # for re-CI at other alpha
        "items_pc_cov": items_pc_cov,                                              # (event, diff, fp, entry, qratio) for P1a/P1b
        "diff_ci": cluster_bootstrap_ci(items_pc if per_contract else items_usd),  # event-clustered, 95%
        "diff_ci_usd": cluster_bootstrap_ci(items_usd),                            # $ context
    }


# ── P1 estimator levers (opt-in; default output unchanged = A/A neutral) ──────────

def _ci_hw(ci) -> float | None:
    """Half-width of a (point, lo, hi) CI, or None."""
    _, lo, hi = ci
    return None if lo is None else (hi - lo) / 2.0


def strict_match(items_cov: list[tuple], qlo: float = 0.5, qhi: float = 2.0) -> list[tuple]:
    """P1a — keep only matched markets whose two arms have COMPARABLE size (qty ratio in [qlo,qhi]).
    A market where A traded 40 and B traded 1 is a different fill population, not a clean paired unit;
    dropping it removes qty-reweight residual. Returns [(event, diff)]."""
    return [(ev, d) for (ev, d, _fp, _entry, qr) in items_cov if qr == qr and qlo <= qr <= qhi]


def cuped_adjust(items_cov: list[tuple], cov: str = "fp") -> tuple[list[tuple], float, float]:
    """P1b — CUPED / control-variate variance reduction on the paired difference.
    D_adj = D - θ(X - E[X]), θ = Cov(D,X)/Var(X), X a PRE-OUTCOME covariate (fp=fair_prob_at_open,
    entry=entry_cents). Returns ([(event, D_adj)], theta, point_shift). point_shift = mean(D_adj)-mean(D):
    a LARGE shift means X is on the treatment's causal path (over-adjustment) — the caller must reject,
    not celebrate, that case. Covariate is arm-COMMON market context, never the fill result."""
    idx = {"fp": 2, "entry": 3}[cov]
    ds = [it[1] for it in items_cov]
    xs = [it[idx] for it in items_cov]
    if len(ds) < 3:
        return ([(it[0], it[1]) for it in items_cov], 0.0, 0.0)
    mx = _mean(xs); md = _mean(ds)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 1e-12:
        return ([(it[0], it[1]) for it in items_cov], 0.0, 0.0)
    theta = sum((d - md) * (x - mx) for d, x in zip(ds, xs)) / vx
    adj = [(it[0], it[1] - theta * (it[idx] - mx)) for it in items_cov]
    shift = _mean([v for _, v in adj]) - md
    return (adj, theta, shift)


def westfall_young(arm_items: dict, alpha: float = 0.05, B: int | None = None, seed: int = 7) -> dict:
    """P1c — Westfall–Young max-t step-down multiplicity, event-clustered and JOINTLY resampled across
    arms so cross-arm correlation (shared events, shared baseline, and — once P0a lands — shared book)
    legitimately recovers the family-wise power a per-arm Bonferroni discards.

    arm_items: {arm_name: [(event, diff), ...]}. Resamples the UNION event pool once per iteration and
    feeds those same events to every arm (preserving dependence). Returns {arm: {'t','adj_p','reject'}}.
    Under the null each arm is centered to mean 0; the max standardized |t| across arms builds the null."""
    if B is None:
        B = B_DEFAULT
    from collections import defaultdict
    # per-arm event->values, and observed standardized stat
    per_arm = {}
    obs_t = {}
    union = set()
    for name, items in arm_items.items():
        g = defaultdict(list)
        for ev, v in items:
            g[ev].append(v)
        per_arm[name] = g
        union |= set(g)
        vals = [v for _, v in items]
        m = _mean(vals)
        # clustered SE: sd of per-event means / sqrt(n_events)
        ev_means = [_mean(g[e]) for e in g]
        se = (_stdev(ev_means) / math.sqrt(len(ev_means))) if len(ev_means) > 1 else 0.0
        obs_t[name] = (abs(m) / se) if se > 0 else 0.0
    union = sorted(union)
    if not union:
        return {n: {"t": 0.0, "adj_p": 1.0, "reject": False} for n in arm_items}
    rng = random.Random(seed)
    k = len(union)
    # centered per-arm event means for the null
    centered = {}
    for name, g in per_arm.items():
        m = _mean([v for e in g for v in g[e]])
        centered[name] = {e: _mean(g[e]) - m for e in g}
    maxt_null = []
    names = list(arm_items)
    for _ in range(B):
        picks = [union[rng.randrange(k)] for _ in range(k)]
        best = 0.0
        for name in names:
            cm = centered[name]
            evs = [cm[e] for e in picks if e in cm]
            if len(evs) > 1:
                m = _mean(evs); se = _stdev(evs) / math.sqrt(len(evs))
                t = abs(m) / se if se > 0 else 0.0
                if t > best:
                    best = t
        maxt_null.append(best)
    maxt_null.sort()
    out = {}
    for name in names:
        t = obs_t[name]
        adj_p = sum(1 for z in maxt_null if z >= t) / len(maxt_null)
        out[name] = {"t": t, "adj_p": adj_p, "reject": adj_p < alpha}
    return out


def variance_report(base_dir: Path, arm_dirs: list[tuple], seed: int = 11) -> None:
    """Measure — never assert — each P1 lever's effect on the paired difference, on EXISTING logs.
    A lever's CI shrink is only 'FREE POWER' when it doesn't change the estimand. For STRICT the tell
    is the point-shift Δpt (large Δpt = the edge lived in the trimmed size-mismatched markets). For
    CUPED, Δpt is USELESS as a tell — CUPED is mean-preserving by construction, so Δpt≡0 even when the
    covariate sits on the treatment's causal path (2026-07-01 quant fix). CUPED over-adjustment shows
    up ONLY as variance collapse, so we print the variance-ratio vr = cuped hw / raw hw: a suspiciously
    SMALL vr means the covariate proxies the treatment — scrutinize, don't celebrate."""
    print("\nVARIANCE REPORT — measured per-lever effect on the paired diff (event-clustered, existing logs)")
    print(f"  {'arm vs base':20s} {'nev':>3s} | {'raw pt':>7s} {'hw':>5s} | {'strict pt':>9s} {'hw':>5s} {'Δpt':>6s} n | {'cuped pt':>8s} {'hw':>5s} {'Δpt':>6s} {'vr':>5s}")
    for name, d in arm_dirs:
        r = pairwise(ROOT / "state" / d, base_dir, by_ticker_only=True)
        cov = r["items_pc_cov"]
        if len(cov) < 5:
            print(f"  {name:20s} {len(cov):>3d}  (too few matched markets)")
            continue
        raw = [(ev, dd) for (ev, dd, *_r) in cov]
        strict = strict_match(cov)
        cuped, _th, cshift = cuped_adjust(cov, "fp")
        rp, rlo, rhi = cluster_bootstrap_ci(raw, seed=seed)
        n_strict_ev = len({ev for ev, _ in strict})
        degenerate = n_strict_ev < 5  # strict trimmed the sample below usefulness → don't show a fake-tight hw
        sp, slo, shi = cluster_bootstrap_ci(strict, seed=seed) if (strict and not degenerate) else (None, None, None)
        cp, clo, chi = cluster_bootstrap_ci(cuped, seed=seed)
        nev = len({ev for ev, _ in raw})
        hw = lambda lo, hi: f"{(hi-lo)/2:.2f}" if lo is not None else "—"
        s_dpt = (sp - rp) if sp is not None else 0.0
        n_drop = len(raw) - len(strict)
        s_pt = f"{sp:>+9.2f}" if sp is not None else ("degen" if degenerate else "—").rjust(9)
        s_hw = hw(slo, shi) if sp is not None else "—"
        s_d = f"{s_dpt:>+6.2f}" if sp is not None else "   — "
        # vr = cuped hw / raw hw — the ONLY visible tell for CUPED over-adjustment (Δpt≡0 by construction)
        _raw_hw = (rhi - rlo) / 2 if rlo is not None else None
        _cup_hw = (chi - clo) / 2 if clo is not None else None
        vr = f"{(_cup_hw / _raw_hw):>5.2f}" if (_raw_hw and _cup_hw is not None and _raw_hw > 0) else "    —"
        print(f"  {name:20s} {nev:>3d} | {rp:>+7.2f} {hw(rlo,rhi):>5s} | "
              f"{s_pt} {s_hw:>5s} {s_d} {n_drop:>2d} | "
              f"{cp:>+8.2f} {hw(clo,chi):>5s} {cshift:>+6.2f} {vr}")
    print("  READ: strict is FREE POWER only if hw shrinks AND Δpt≈0. A large strict Δpt ⇒ the edge was")
    print("  carried by high-leverage size-mismatched markets (robustness re-read of a SMALLER estimand).")
    print("  CUPED is MEAN-PRESERVING (Δpt≡0 by construction — so Δpt can NEVER flag CUPED over-adjustment;")
    print("  a non-zero Δpt would be a bug). Watch vr = cuped hw / raw hw instead: its benefit is the hw")
    print("  shrink, the leak is prevented STRUCTURALLY (only pre-outcome covariates fp/entry allowed), and")
    print("  a suspiciously SMALL vr means the covariate proxies the treatment — scrutinize, don't trust.")
    print("  Nothing here is claimed as a multiplier; every number is bootstrapped on the real logs.")


# ── formatting ───────────────────────────────────────────────────────

def _fmt_ci(ci, scale=1.0, sig="$", excl0=True) -> str:
    pt, lo, hi = ci
    if pt is None:
        return "—"
    star = " *" if (excl0 and lo is not None and (lo > 0 or hi < 0)) else ""
    return f"{sig}{pt*scale:+.2f} [{lo*scale:+.2f}, {hi*scale:+.2f}]{star}"


def main() -> int:
    global B_DEFAULT
    ap = argparse.ArgumentParser(description="Compare A/B arms with bootstrap CIs.")
    ap.add_argument("--config", default=str(ROOT / "variants.json"))
    ap.add_argument("--pairwise", default="", help="Two arm names 'A,B' for matched-market P&L diff")
    ap.add_argument("--baseline", default="", help="Arm name: print EVERY arm's matched-market P&L diff vs this baseline — the rigorous 'is X significantly better than baseline', with within-noise flagged")
    ap.add_argument("--variance-report", default="", metavar="BASELINE", help="P1: MEASURE (not assert) the paired-diff CI half-width per estimator lever (strict-match, CUPED, both) vs this baseline arm, on existing logs")
    ap.add_argument("--maxt", action="store_true", help="P1c: add the Westfall–Young max-t multiplicity verdict alongside Bonferroni in --baseline (event-clustered, jointly resampled)")
    ap.add_argument("--bootstrap", type=int, default=B_DEFAULT, help="bootstrap resamples")
    args = ap.parse_args()
    B_DEFAULT = args.bootstrap

    arms = _arms_from_config(Path(args.config))
    arms = [(name, d) for name, d in arms if (ROOT / "state" / d / "settlement-log.jsonl").is_file()]
    if not arms:
        print("No arms with settlement logs found.")
        return 0

    print(f"\nA/B arm comparison  (bootstrap B={B_DEFAULT}, 95% CI; EVENT-CLUSTERED; * = CI excludes 0)\n")
    hdr = (f"{'arm':18s} {'n':>4s} {'ev':>4s} {'win%':>5s} {'P&L$':>9s} {'wt¢':>6s} "
           f"{'¢/ct (evt-clust 95%CI)':>30s} {'BSS':>6s} {'BSS Δbrier CI':>20s}")
    print(hdr)
    print("-" * len(hdr))
    for name, d in arms:
        m = arm_metrics(ROOT / "state" / d)
        bss = f"{m['bss']*100:+.1f}%" if m["bss"] is not None else "—"
        print(f"{name:18s} {m['n']:>4d} {m['n_events']:>4d} {m['win_rate']:>4.0f}% "
              f"{m['total_pnl']:>+9.2f} {m['pnl_per_contract_wt']:>+6.1f} "
              f"{_fmt_ci(m['pnl_per_contract_ci'], sig='', excl0=True):>30s} {bss:>6s} "
              f"{_fmt_ci(m['brier_diff_ci'], sig='', excl0=True):>20s}")

    print("\n  ¢/ct: equal-weighted mean of pnl/qty, EVENT-CLUSTERED bootstrap CI (bins of one weather")
    print("  event settle together → correlated → an i.i.d. CI is too narrow). wt¢ = contract-weighted")
    print("  (total$/total contracts; the $ at current sizing; no CI). ev = # independent events (the")
    print("  REAL sample size, not n trades). Arms WITHOUT a * are WITHIN NOISE — don't rank on point")
    print("  estimates. BSS Δbrier CI >0 ⇒ model beats market. Use --baseline for the pairwise test.")

    if args.baseline:
        dirs = dict(_arms_from_config(Path(args.config)))
        if args.baseline not in dirs:
            print(f"\n--baseline: unknown arm '{args.baseline}'. Known: {', '.join(dirs)}")
            return 2
        base_dir = ROOT / "state" / dirs[args.baseline]
        # 2026-07-01 quant fix: Westfall–Young max-t IS the ruling. The old per-arm percentile CI at
        # alpha/family was NOT a family-wise correction (it just read extreme bootstrap tails whose
        # measured coverage was ~64% vs nominal 95%) and emitted a false "passive BETTER (sig)" flag
        # that WY (adj_p=0.571) and Bonferroni-on-p (~0.127) both reject. A verdict now needs BOTH
        # WY FWER rejection AND survival on the strict size-matched subset (the +1.52 passive edge
        # collapsed to +0.68 once size-mismatched markets were trimmed).
        print(f"\nPairwise vs baseline '{args.baseline}'  (PER-CONTRACT so a book-SIZE mismatch can't fake")
        print(f"  an edge; same markets; EVENT-CLUSTERED BCa CI). Ruling = Westfall–Young max-t adj_p")
        print(f"  (FWER across the arm family) AND survival on the strict size-matched subset.")
        print(f"  WY family = ENABLED arms with >= {MIN_WY_EVENTS} events (concurrently-active experiment arms);")
        print(f"  disabled/deprecated and tiny-event arms are shown for context but EXCLUDED from the FWER")
        print(f"  correction — their degenerate max-t null otherwise pushes every adj_p toward 1 (2026-07-13).")
        print(f"  {'arm':16s} {'mkts':>5s} {'ev':>4s} {'qty/mkt a|base':>14s} {'¢/ct diff (95% CI)':>26s} {'adj_p':>6s}  verdict")
        rows: list = []
        for name, d in arms:
            if name == args.baseline:
                continue
            r = pairwise(ROOT / "state" / d, base_dir, by_ticker_only=True)
            rows.append((name, r))
        # Westfall–Young FAMILY = concurrently-active (enabled) arms with a stable event count
        # (>= MIN_WY_EVENTS). Deprecated/disabled arms and tiny-event arms are EXCLUDED from the
        # multiplicity correction: swept in, their degenerate max-t null made the ruling un-passable
        # for EVERY arm (adj_p→1; 2026-07-13 audit). The pairwise table still lists all arms for
        # context; only the FWER correction is scoped to the real experiment family.
        enabled_names = _enabled_arm_names(Path(args.config))
        family = {n: r["items_pc"] for n, r in rows
                  if r["items_pc"] and n in enabled_names and r["matched_events"] >= MIN_WY_EVENTS}
        wy = westfall_young(family) if family else {}
        for name, r in rows:
            pt, lo, hi = r["diff_ci"]
            w = wy.get(name)
            adjp = f"{w['adj_p']:.3f}" if w else "    —"
            in_family = name in family
            if pt is None or r["matched_events"] < 5:
                verdict, adjp = "too few events", "    —"
            elif not in_family:
                # Reported for context, but NOT part of the FWER ruling — so it never gets a misleading
                # "fails WY FWER" (which would imply it competed and lost). Say WHY it is out of family.
                why = "disabled" if name not in enabled_names else f"<{MIN_WY_EVENTS} ev"
                if lo is not None and (lo > 0 or hi < 0):
                    verdict = f"raw 95% excl 0 — NOT in WY family ({why})"
                else:
                    verdict = f"within noise (not in WY family: {why})"
            elif w and w["reject"]:
                # Strict size-match gate: only flag BETTER/WORSE when the comparable-size subset
                # agrees in sign with a CI clear of 0 (kills size-artifact edges).
                strict = strict_match(r.get("items_pc_cov") or [])
                n_sev = len({ev for ev, _ in strict})
                sp, slo, shi = cluster_bootstrap_ci(strict, seed=5) if n_sev >= 5 else (None, None, None)
                if sp is not None and ((pt > 0 and slo > 0) or (pt < 0 and shi < 0)):
                    verdict = f"{'BETTER' if pt > 0 else 'WORSE'} (sig: WY FWER + survives strict size-match)"
                else:
                    verdict = "borderline (WY-sig but fails strict size-match)"
            elif lo is not None and (lo > 0 or hi < 0):
                verdict = "borderline (95% only — fails WY FWER)"
            else:
                verdict = "within noise"
            qty = f"{r['qty_per_mkt_a']:.0f}|{r['qty_per_mkt_b']:.0f}"
            print(f"  {name:16s} {r['matched_markets']:>5d} {r['matched_events']:>4d} {qty:>14s} "
                  f"{_fmt_ci(r['diff_ci'], sig=''):>26s} {adjp:>6s}  {verdict}")
        print(f"  positive ⇒ arm beats '{args.baseline}' PER CONTRACT. 'within noise' ⇒ not distinguishable.")
        print(f"  A big qty/mkt mismatch means the raw $ diff would be a SIZE artifact, not strategy.")
        if args.maxt and wy:
            print(f"\n  Westfall–Young max-t detail (the ruling above; studentized, event-clustered, "
                  f"jointly resampled):")
            for name in sorted(wy, key=lambda n: wy[n]["adj_p"]):
                w = wy[name]
                mark = " REJECT (sig, FWER-controlled)" if w["reject"] else ""
                print(f"    {name:16s} |t|={w['t']:>5.2f}  adj_p={w['adj_p']:.3f}{mark}")
            print("    NOTE (2026-07-01): WY replaced the old percentile-Bonferroni CI as the ruling — that")
            print("    path's measured coverage was ~64% vs nominal 95% on these skewed few-event data, i.e.")
            print("    anti-conservative; 'more conservative' WY verdicts are the trustworthy ones.")

    if getattr(args, "variance_report", ""):
        dirs = dict(_arms_from_config(Path(args.config)))
        if args.variance_report not in dirs:
            print(f"\n--variance-report: unknown arm '{args.variance_report}'. Known: {', '.join(dirs)}")
            return 2
        base_dir = ROOT / "state" / dirs[args.variance_report]
        others = [(n, d) for n, d in arms if n != args.variance_report]
        variance_report(base_dir, others)

    if args.pairwise:
        try:
            a_name, b_name = [x.strip() for x in args.pairwise.split(",")]
        except ValueError:
            print("\n--pairwise expects 'A,B'")
            return 2
        dirs = dict(_arms_from_config(Path(args.config)))
        if a_name not in dirs or b_name not in dirs:
            print(f"\n--pairwise: unknown arm(s). Known: {', '.join(dirs)}")
            return 2
        r = pairwise(ROOT / "state" / dirs[a_name], ROOT / "state" / dirs[b_name])
        print(f"\nPairwise (matched markets, PER-CONTRACT, event-clustered): {a_name} − {b_name}")
        print(f"  matched markets: {r['matched_markets']}  (events {r['matched_events']}; "
              f"qty/mkt {r['qty_per_mkt_a']:.0f} vs {r['qty_per_mkt_b']:.0f})")
        print(f"  ¢/ct diff: {_fmt_ci(r['diff_ci'], sig='')}    $ diff/mkt (context): {_fmt_ci(r['diff_ci_usd'])}")
        print(f"  (positive ⇒ {a_name} > {b_name} per contract; * ⇒ sig; mind the qty/mkt for size parity)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

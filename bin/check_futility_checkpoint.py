#!/usr/bin/env python3
"""bin/check_futility_checkpoint.py — pre-registered futility checkpoints for the premium-live edge.

Quant review 2026-07-01 experiment #1. The premium edge is unproven and ~30x under-powered; the
failure mode is trading it indefinitely while chasing a daily point estimate that drifts in noise.
This PRE-REGISTERS the estimand (premium-live per-contract edge, EVENT-CLUSTERED, BCa CI) and fixed
checkpoints at n_events = 200, 400. When n_events reaches a checkpoint:

    GO                if the 95% event-clustered CI is clear of 0 on the positive side (lo > 0),
    STOP-FOR-FUTILITY otherwise (spans 0, or negative).

A pre-registered checkpoint does NOT "continue" — that would be the daily-chasing behavior we're
preventing. Each checkpoint's verdict is written ONCE to state/live-premium/futility-checkpoint-
state.json and never overwritten (immutable), with the estimand + checkpoints frozen at first run.
Report + alert only — it does NOT halt trading (a human makes the live-money call).

Usage:  python3 bin/check_futility_checkpoint.py
Env:    KALSHI_WEATHER_FUTILITY_CHECKPOINTS (default "200,400")
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "bin"))

from compare_variants import _read_jsonl, _event, cluster_bootstrap_ci, asymp_confseq  # noqa: E402
from data.weather_data import is_weather_ticker                          # noqa: E402

# Anytime-valid confidence sequence params (pre-registered, frozen on first run) — lets a verdict
# fire the INSTANT the CS excludes 0, without waiting for a fixed n_events checkpoint.
_CS_ALPHA, _CS_TOPT = 0.05, 100

STATE_DIR = ROOT / "state" / "live-premium"
STATE_PATH = STATE_DIR / "futility-checkpoint-state.json"
_NOW = lambda: datetime.now(timezone.utc).isoformat()   # noqa: E731


def _checkpoints() -> list[int]:
    raw = os.environ.get("KALSHI_WEATHER_FUTILITY_CHECKPOINTS", "200,400")
    return sorted({int(x) for x in raw.split(",") if x.strip().isdigit()})


def edge_items(state_dir: Optional[Path] = None):
    """Weather-only, attributed premium-live fills → (event-clustered per-¢/ct items, n_events).
    Filter mirrors the fill-edge tooling: qty>0, entry present, real weather ticker.
    Resolves STATE_DIR at call time (not default-bound) so tests can redirect it."""
    state_dir = state_dir or STATE_DIR
    rows = _read_jsonl(state_dir / "settlement-log.jsonl")
    attr = [s for s in rows
            if (s.get("qty") or 0) > 0
            and s.get("entry_cents") not in (None, 0)
            and is_weather_ticker(s.get("ticker", ""))]
    items = [(_event(s.get("ticker", "")), float(s.get("pnl_cents", 0)) / s["qty"]) for s in attr]
    n_events = len({ev for ev, _ in items})
    return items, n_events


def _load_state(checkpoints: list[int]) -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    # First run: FREEZE the estimand + checkpoints. Never mutated afterward.
    return {
        "tool_version": "1.0",
        "created_utc": _NOW(),
        "estimand": {
            "metric": "premium-live per-contract edge (pnl_cents/qty)",
            "unit": "event-clustered (series+city+date)",
            "ci": "BCa bootstrap, 95%",
            "source": "state/live-premium/settlement-log.jsonl",
            "filter": "qty>0 AND entry_cents present AND is_weather_ticker",
            "rule": "GO if CI lo>0 at the checkpoint, else STOP-FOR-FUTILITY (no 'continue')",
        },
        "checkpoints": [
            {"n_events": n, "reached": False, "decision": None, "verdict_utc": None,
             "edge_point_cents": None, "edge_ci_cents": None}
            for n in checkpoints
        ],
        # Anytime-valid confidence sequence (pre-registered): a verdict may fire BEFORE any fixed
        # checkpoint the instant the CS excludes 0. Immutable once set.
        "anytime_cs": {"alpha": _CS_ALPHA, "t_opt": _CS_TOPT, "method": "asymptotic CS (Howard/Ramdas)",
                       "rule": "GO-EARLY if CS lo>0, STOP-EARLY if CS hi<0 — valid at any time"},
        "anytime_verdict": None,
    }


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def main() -> int:
    state = _load_state(_checkpoints())
    items, n_events = edge_items()
    pt, lo, hi = cluster_bootstrap_ci(items) if items else (None, None, None)
    state["last_run_utc"] = _NOW()
    state["n_events_current"] = n_events
    state["edge_current_cents"] = (
        None if pt is None else {"point": round(pt, 2), "ci": [round(lo, 2), round(hi, 2)]}
    )

    print(f"[futility] premium-live: n_events={n_events}  edge/ct="
          + ("n/a" if pt is None else f"{pt:+.2f}¢ [{lo:+.2f}, {hi:+.2f}] (event-clustered BCa 95%)"))

    # Anytime-valid CS: a decisive read here can rule BEFORE the fixed 200/400 checkpoints.
    cpt, clo, chi = asymp_confseq(items, alpha=_CS_ALPHA, t_opt=_CS_TOPT) if items else (None, None, None)
    cs_decisive = clo is not None and clo != chi and (clo > 0 or chi < 0)
    if clo is not None:
        print(f"  anytime-valid CS: {cpt:+.2f}¢ [{clo:+.2f}, {chi:+.2f}]"
              + ("  → DECISIVE" if cs_decisive else "  (spans 0 — keep collecting)"))
    changed = False
    if state.get("anytime_verdict") is None and cs_decisive:
        av = "GO-EARLY" if clo > 0 else "STOP-EARLY-FOR-FUTILITY"
        state["anytime_verdict"] = {"decision": av, "verdict_utc": _NOW(), "n_events": n_events,
                                    "edge_point_cents": round(cpt, 2), "cs_ci_cents": [round(clo, 2), round(chi, 2)]}
        changed = True
        print(f"  ⚑ ANYTIME VERDICT: {av} at n_events={n_events} "
              f"(CS {cpt:+.2f}¢ [{clo:+.2f}, {chi:+.2f}] excludes 0 — valid despite continuous monitoring)")
        try:
            from trader.notify import alert
            alert(f"Futility ANYTIME verdict: {av} (n={n_events}) — premium-live edge CS "
                  f"{cpt:+.2f}¢/ct [{clo:+.2f}, {chi:+.2f}] excludes 0. Report only; halt is your call.",
                  key="futility_anytime")
        except Exception as e:
            print(f"  [futility] anytime alert dispatch failed: {e}", file=sys.stderr)
    elif state.get("anytime_verdict") is not None:
        av = state["anytime_verdict"]
        print(f"  anytime verdict: {av['decision']} (locked {av['verdict_utc']}, n={av['n_events']})")
    for cp in state["checkpoints"]:
        n = cp["n_events"]
        if cp["decision"] is not None:                        # immutable — already decided
            print(f"  checkpoint {n}: {cp['decision']} (locked {cp['verdict_utc']})")
            continue
        if n_events < n:
            print(f"  checkpoint {n}: awaiting ({n_events}/{n} events)")
            continue
        # Reached, undecided → decide ONCE. GO only if the CI is clear of 0 on the positive side.
        decision = "GO" if (lo is not None and lo > 0) else "STOP-FOR-FUTILITY"
        cp.update(reached=True, decision=decision, verdict_utc=_NOW(),
                  edge_point_cents=(None if pt is None else round(pt, 2)),
                  edge_ci_cents=(None if lo is None else [round(lo, 2), round(hi, 2)]),
                  n_events_at_verdict=n_events)
        changed = True
        print(f"  checkpoint {n}: DECISION = {decision}  "
              f"(edge {pt:+.2f}¢ [{lo:+.2f}, {hi:+.2f}], n={n_events})")
        if decision == "STOP-FOR-FUTILITY":
            try:
                from trader.notify import alert
                alert(f"Futility checkpoint {n} reached ({n_events} events): STOP-FOR-FUTILITY — "
                      f"premium-live edge {pt:+.2f}¢/ct CI [{lo:+.2f}, {hi:+.2f}] not clear of 0. "
                      f"Report only; halt is your call.", key=f"futility_cp_{n}")
            except Exception as e:
                print(f"  [futility] alert dispatch failed: {e}", file=sys.stderr)

    _save_state(state)
    if not changed:
        print(f"[futility] no new verdict; state at {STATE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

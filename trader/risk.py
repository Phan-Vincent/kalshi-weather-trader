#!/usr/bin/env python3
"""
trader/risk.py — Risk gate + state persistence for the Kalshi weather trader.
All money in cents internally. Display in dollars.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _load_json_state_or_raise(path: Path, kind: str) -> dict:
    """Load a JSON state file with fail-CLOSED corruption handling (audit 2026-07-06).

    The old loaders collapsed a corrupt/unreadable file to {} (all counters → zero) and
    the next save PERSISTED that amnesia — erasing the day's realized losses and manual
    city halts (so live orders keep flowing past the $/city stops) or wiping the
    calibration-source book. That is the opposite of fail-safe for money state.

    Rules:
      - Absent file            → {} (legitimate fresh start).
      - Present but EMPTY (0B)  → {} + a warning (likely a torn write; nothing to lose).
      - Present but UNPARSEABLE → back the bytes up to <path>.corrupt-<ts>, alert, and
        RAISE. We never overwrite recoverable state with defaults, and we never silently
        trade on zeroed risk counters.
    """
    if not path.exists():
        return {}
    try:
        blob = path.read_text()
    except OSError as e:
        raise RuntimeError(f"{kind}: cannot read {path}: {e}") from e
    if not blob.strip():
        return {}
    try:
        return json.loads(blob)
    except json.JSONDecodeError as e:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".corrupt-{ts}")
        try:
            backup.write_text(blob)  # preserve for recovery BEFORE anything can overwrite it
        except OSError:
            pass
        try:
            from trader.notify import alert
            alert(f"⚠️ {kind} state file CORRUPT ({path.name}): {e}. Backed up to "
                  f"{backup.name}; refusing to reset+persist (would erase money state). "
                  f"Inspect/restore then re-run.", key=f"state_corrupt_{kind}")
        except Exception:
            pass
        raise RuntimeError(
            f"{kind}: {path} is corrupt (backed up to {backup.name}); refusing to "
            f"silently reset money state. Restore or delete the file to proceed."
        ) from e


def _pacific_trading_day(now_utc: datetime) -> str:
    """The trading-day date string (YYYY-MM-DD) in America/Los_Angeles for the DAILY loss
    window. Keyed to PT (not UTC) so the daily counter rolls at PT midnight — OUTSIDE the
    9:20-19:20 PT slot schedule — instead of at UTC midnight (~16-17:00 PT, mid-schedule),
    where an afternoon stop used to silently re-arm before the evening slots (audit
    2026-07-06). Falls back to the UTC date if the tz database is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return now_utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return now_utc.strftime("%Y-%m-%d")


def _event_key(ticker: str) -> str:
    """The weather EVENT a ticker belongs to: series+city+date, dropping the strike
    bin, e.g. 'KXHIGHTHOU-26JUL06-B95' -> 'KXHIGHTHOU-26JUL06'. All strikes of the
    same event settle on ONE realized temperature, so their exposures must be capped
    together (mirrors compare_variants._event)."""
    parts = (ticker or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else (ticker or "")


def _market_family(ticker: str) -> str:
    """Temperature market family from the series token: 'high' (KXHIGHT*/KXHIGH*),
    'low' (KXLOWT*/KXLOW*), 'temp' (KXTEMP* band markets), or '' if not a weather
    temperature ticker. Series-prefix regex mirrors
    paper_trade._extract_city_from_ticker."""
    import re
    m = re.match(r"^KX(HIGHT|LOWT|HIGH|LOW|TEMP)", ticker or "")
    if not m:
        return ""
    tok = m.group(1)
    if tok in ("HIGHT", "HIGH"):
        return "high"
    if tok in ("LOWT", "LOW"):
        return "low"
    return "temp"


def _correlated_side(ticker: str, side: str) -> Optional[str]:
    """The correlation bucket for the cross-event exposure cap: the BET SIDE ('yes'|'no')
    on a daily-high/low weather market, or None for markets excluded from the cap.

    Side — NOT an inferred warm/cold 'thermal direction' — is the correct correlation axis.
    Most daily temperature markets are two-sided '-B' BAND markets ([strike-1, strike+1]),
    where a NO is TWO-TAILED (wins if the temp is colder OR warmer than the bin) and has no
    single warm/cold direction. The 2026-07-10..12 drawdown was 12 simultaneous NO ('fade the
    bin') positions across cities — mostly band markets — that all lost when the temps landed
    in the faded bins under one regional model/forecast miss. Grouping by side captures that
    'large simultaneous fade book, all exposed to the same model/regime error' failure for
    EVERY market type, including the bands that actually busted; a warm/cold mapping would
    misclassify bands (and invert on 'below' threshold markets) and would EXCLUDE exactly the
    positions that caused the loss. Scoped to the daily high/low families that busted; KXTEMP
    hourly markets and non-weather tickers (sports, etc.) return None and are not capped."""
    if _market_family(ticker) not in ("high", "low"):
        return None
    s = (side or "").lower()
    return s if s in ("yes", "no") else None


@dataclass
class ResearchModeConfig:
    """
    Configurable research-mode guardrails. Defaults to SAFE.

    Environment overrides:
      KALSHI_RESEARCH_MODE=0              — disable research mode (use production defaults)
      KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS=N — override safe min price (default 10¢)
      KALSHI_RESEARCH_MODE_ALLOW_RED=1    — allow new entries even when Brier gate is RED
      KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES=1 — allow same-ticker re-exposure on reruns
      KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER=1 — disable city circuit breakers
    """
    enabled: bool = True
    min_price_cents: int = 20          # 2026-06-19 Claude audit: ≤15¢ bucket is net loser (-$28, 11% WR). Raised to 20¢.
    brier_min_trades: int = 150        # meaningful sample before gating
    allow_red_gate: bool = False       # block new entries if model Brier gate is RED
    allow_duplicates: bool = False     # block same-ticker re-exposure on reruns
    disable_city_circuit_breaker: bool = False  # normally enforced

    @classmethod
    def from_env(cls) -> "ResearchModeConfig":
        return cls(
            enabled=os.getenv("KALSHI_RESEARCH_MODE", "1") != "0",
            min_price_cents=int(os.getenv("KALSHI_RESEARCH_MODE_MIN_PRICE_CENTS", "20")),
            brier_min_trades=int(os.getenv("KALSHI_RESEARCH_MODE_BRIER_MIN_TRADES", "150")),
            allow_red_gate=os.getenv("KALSHI_RESEARCH_MODE_ALLOW_RED", "") == "1",
            allow_duplicates=os.getenv("KALSHI_RESEARCH_MODE_ALLOW_DUPLICATES", "") == "1",
            disable_city_circuit_breaker=os.getenv("KALSHI_RESEARCH_MODE_DISABLE_CIRCUIT_BREAKER", "") == "1",
        )


@dataclass
class RiskGate:
    """Enforces trading limits and persists state to disk."""

    daily_max_loss_dollars: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_DAILY_MAX_LOSS_DOLLARS", "50")))
    weekly_max_loss_dollars: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_WEEKLY_MAX_LOSS_DOLLARS", "150")))  # ~3x daily; now env-overridable so a small per-arm daily (e.g. live $20) can keep ~3x ($60) instead of a 7.5x-loose backstop (audit 2026-06-25)
    per_event_max_position_dollars: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_MAX_POSITION_DOLLARS", "0")))  # 0 = auto-scale with bankroll
    per_run_max_orders: int = 3
    # 2026-06-19 P&L work: close the long-standing max_open_positions gap. Caps
    # total concurrent exposure (open positions + resting maker quotes). Env
    # KALSHI_WEATHER_MAX_OPEN_POSITIONS; default 40 (paper); live passes a lower
    # cap. 0 disables. Blunts the top-20-trades=83%-of-P&L concentration risk.
    max_open_positions: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_MAX_OPEN_POSITIONS", "40")))
    min_confidence: str = field(default_factory=lambda: os.environ.get("KALSHI_WEATHER_MIN_CONFIDENCE", "med"))  # env-overridable: "low" for paper data gathering
    min_edge_cents_after_fees: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_MIN_EDGE_CENTS", "20")))  # 2026-06-17: env-overridable. 20¢ for live, 10¢ for paper data gathering.
    min_price_cents: int = 20            # 2026-06-19 Claude audit: ≤15¢ bucket net loser. Raised to 20¢.
    max_price_cents: int = 80            # symmetric upper bound: NO at >80¢ = YES at <20¢, too thin
    city_loss_circuit_breaker_threshold: int = 8  # Halt new exposure in a city after N consecutive losses
    # Cross-city correlated same-side exposure cap (2026-07-13, drawdown fix #1). The
    # per-event and per-city gates never fired on 2026-07-10..12 because the loss was 12
    # SAME-SIDE (NO / fade-the-bin) positions across DIFFERENT cities, all exposed to one
    # regional forecast miss. These cap the number of DISTINCT daily high/low events
    # (series+city+date) held simultaneously on one SIDE (yes|no) and, optionally, the
    # worst-case $ notional per side bucket. BOTH default 0 = DISABLED — opt-in via env
    # (idiom mirrors SIZE_RAMP / CALIBRATED_EDGE), so merely landing this code changes
    # NOTHING on the live arm and does not perturb the futility-checkpoint stream until an
    # operator sets the knob on the live variant.
    max_correlated_direction_events: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_MAX_CORRELATED_DIRECTION_EVENTS", "0")))
    max_correlated_direction_dollars: int = field(default_factory=lambda: int(os.environ.get("KALSHI_WEATHER_MAX_CORRELATED_DIRECTION_DOLLARS", "0")))
    max_close_time_seconds_into_future: int = 36 * 3600   # 36h
    min_close_time_seconds: int = 90 * 60                    # 90 min
    _state_dir: Path = field(repr=False, default_factory=lambda: Path(os.environ.get("KALSHI_WEATHER_STATE_DIR", Path.home() / ".openclaw/workspace/automations/kalshi-weather/state")))

    # Research-mode guardrails (default safe)
    research_mode: ResearchModeConfig = field(default_factory=ResearchModeConfig.from_env)

    # Confidence ordinal for comparisons
    _CONF_ORD = {"low": 0, "med": 1, "high": 2}

    def __post_init__(self):
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / "risk-state.json"
        # Defensive defaults for every field _load_state sets, so a RiskGate whose _load_state is
        # mocked/short-circuited can't crash hot paths like get_city_risk_multiplier (audit #1).
        # _load_state() overrides all of these from disk on the normal path.
        self._today_pnl_cents = 0
        self._week_pnl_cents = 0
        self._today = ""
        self._week_id = ""
        self._open_positions = []
        self._realized_today_cents = 0
        self._consecutive_losses = 0
        self._consecutive_losses_by_city = {}
        self._city_pnl_cents = {}
        self._total_bankroll_cents = 0
        self._halted_cities = []
        self._rung_idx = 0
        self._fills_at_rung_start = 0
        self._balance_at_rung_start = 0
        # Correlated-direction cap buckets (in-memory ONLY; seeded per run, never persisted —
        # paper_trade.py does not persist risk state, so these cannot leak across cycles).
        self._dir_events: dict = {}      # direction ('warm'|'cold') -> set(event_key)
        self._dir_notional: dict = {}    # direction -> worst-case cents at risk
        self._load_state()
        # In research mode, override min_price_cents to the safer default unless
        # the caller explicitly passes a different value AND research mode env is off.
        if self.research_mode.enabled:
            self.min_price_cents = self.research_mode.min_price_cents

    # ── State persistence ────────────────────────────────────────────────

    def _load_state(self) -> None:
        # Fail-CLOSED on corruption: a corrupt risk-state must NOT silently reset the
        # loss counters / city halts to zero and then persist that amnesia (audit
        # 2026-07-06). _load_json_state_or_raise backs up the corrupt file and raises.
        raw = _load_json_state_or_raise(self._state_path, "risk")

        # FIX 2026-07-06 (audit): key the DAILY window to the PACIFIC trading day, not the
        # UTC date. UTC midnight is ~16:00-17:00 PT — squarely inside the 9:20-19:20 PT slot
        # schedule — so a daily loss stop tripped in the afternoon silently re-armed with a
        # fresh $20 before the evening slots. PT midnight rolls OUTSIDE the trading window,
        # so a stop now binds for the rest of that trading day. (Weekly stays ISO/UTC — it
        # spans days so the intra-day re-arm doesn't apply; test_weekly_window pins it.)
        today = _pacific_trading_day(datetime.now(timezone.utc))
        _iso = datetime.now(timezone.utc).isocalendar()
        week_id = f"{_iso[0]}-W{_iso[1]:02d}"  # true ISO calendar week (resets Monday)

        self._today_pnl_cents: int = int(raw.get("today_pnl_cents", 0))
        self._week_pnl_cents: int = int(raw.get("week_pnl_cents", 0))
        self._today: str = raw.get("date", today)
        self._week_id: str = raw.get("week_id", week_id)  # legacy "week_start" files seed to current
        self._open_positions: list[dict] = list(raw.get("open_positions", []))
        self._realized_today_cents: int = int(raw.get("realized_today_cents", 0))
        self._consecutive_losses: int = int(raw.get("consecutive_losses", 0))
        self._consecutive_losses_by_city: dict = dict(raw.get("consecutive_losses_by_city", {}))
        self._city_pnl_cents: dict = dict(raw.get("city_pnl_cents", {}))  # cumulative PnL per city
        self._total_bankroll_cents: int = int(raw.get("total_bankroll_cents", 0))  # set from paper_trade
        self._halted_cities: list[str] = sorted(set(raw.get("halted_cities", [])))
        # Gradual live size-ramp state (used only when KALSHI_WEATHER_SIZE_RAMP=1).
        self._rung_idx: int = int(raw.get("rung_idx", 0))
        self._fills_at_rung_start: int = int(raw.get("fills_at_rung_start", 0))
        self._balance_at_rung_start: int = int(raw.get("balance_at_rung_start", 0))

        # Roll dates if stale
        if self._today != today:
            self._today = today
            self._today_pnl_cents = 0
            self._realized_today_cents = 0
        # Reset the weekly accumulator only when the ISO week actually changes. (Previously a
        # today−6d sliding window that advanced daily, so the weekly stop reset every UTC day
        # and never spanned a real week — H4, AUDIT-REPORT-2026-06-23.)
        if self._week_id != week_id:
            self._week_id = week_id
            self._week_pnl_cents = 0

    def _save_state(self) -> None:
        payload = {
            "date": self._today,
            "week_id": self._week_id,
            "today_pnl_cents": self._today_pnl_cents,
            "week_pnl_cents": self._week_pnl_cents,
            "realized_today_cents": self._realized_today_cents,
            "open_positions": self._open_positions,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_losses_by_city": self._consecutive_losses_by_city,
            "city_pnl_cents": self._city_pnl_cents,
            "total_bankroll_cents": self._total_bankroll_cents,
            # Persist only explicit/manual halts. Auto-halted cities are derived
            # from consecutive_losses_by_city at runtime so a later win can
            # reset the streak and automatically re-enable the city.
            "halted_cities": sorted(set(self._halted_cities)),
            "city_loss_circuit_breaker_threshold": self.city_loss_circuit_breaker_threshold,
            "rung_idx": self._rung_idx,
            "fills_at_rung_start": self._fills_at_rung_start,
            "balance_at_rung_start": self._balance_at_rung_start,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._state_path.with_name(self._state_path.name + f".{os.getpid()}.tmp")  # QA-19: per-process tmp
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self._state_path)

    # ── Checks ──────────────────────────────────────────────────────────

    def check_confidence(self, confidence: str) -> bool:
        """Return True if confidence meets minimum."""
        return self._CONF_ORD.get(confidence, -1) >= self._CONF_ORD.get(self.min_confidence, 1)

    def check_close_time(self, close_time_iso: str) -> bool:
        """Return True if market close is within the allowed window."""
        try:
            close_dt = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        now = datetime.now(timezone.utc)
        delta = (close_dt - now).total_seconds()
        return self.min_close_time_seconds <= delta <= self.max_close_time_seconds_into_future

    def get_halted_cities(self) -> list[str]:
        """Cities where new exposure is halted, either manually or by loss streak."""
        halted = set(self._halted_cities)
        for city, losses in self._consecutive_losses_by_city.items():
            try:
                if int(losses) >= self.city_loss_circuit_breaker_threshold:
                    halted.add(city)
            except (TypeError, ValueError):
                continue
        return sorted(halted)

    def set_bankroll(self, bankroll_cents: int) -> None:
        """Set the current bankroll for percentage-based risk scaling.

        Called from paper_trade.py before scanning. Used by
        get_city_risk_multiplier() to compute PnL% thresholds.
        Persisted to disk so settle_paper.py and other processes
        see the live bankroll, not the initial seed.

        Also auto-scales per_event_max_position_dollars to 1% of bankroll
        (unless overridden via KALSHI_WEATHER_MAX_POSITION_DOLLARS env var).
        Floor at $2.50, ceiling at $50.
        """
        self._total_bankroll_cents = max(bankroll_cents, 100)  # floor at $1

        # Auto-scale position size: 1% of bankroll if not explicitly set
        env_override = int(os.environ.get("KALSHI_WEATHER_MAX_POSITION_DOLLARS", "0"))
        if env_override > 0:
            self.per_event_max_position_dollars = env_override
        else:
            pct_of_bank = max(300, min(5000, int(bankroll_cents * 0.01))) // 100  # floor $3, ceiling $50
            self.per_event_max_position_dollars = pct_of_bank

        self._save_state()

    def get_city_risk_multiplier(self, city_code: Optional[str] = None) -> tuple[float, Optional[str]]:
        """Return (multiplier, reason) for position sizing in this city.

        Multiplier values:
          1.00 — full size (city profitable or neutral)
          0.50 — halved (city at -10% to -20% of bankroll)
          0.25 — quartered (city at -20% to -30% of bankroll)
          0.00 — halted (city at <= -30% of bankroll, OR ≥15 consecutive losses)

        FIX 2026-06-11 (trading consultant): replaced hard circuit breaker
        with dynamic Kelly multiplier. Full halt wastes data collection.
        Halving/quartering reduces risk while preserving the error model's
        learning signal.
        """
        if self.research_mode.enabled and self.research_mode.disable_city_circuit_breaker:
            return 1.0, None

        if not city_code:
            return 1.0, None

        city_code = city_code.upper()

        # Manual halt (from _halted_cities) is absolute
        if city_code in set(self._halted_cities):
            return 0.0, f"risk_mult:city_manually_halted({city_code})"

        # Safety: extreme consecutive losses still trigger full halt
        city_losses = int(self._consecutive_losses_by_city.get(city_code, 0) or 0)
        if city_losses >= 15:
            return 0.0, f"risk_mult:consecutive_losses={city_losses}>=15({city_code})"

        # Dynamic multiplier based on city PnL % of bankroll
        city_pnl = int(self._city_pnl_cents.get(city_code, 0) or 0)
        bank = max(self._total_bankroll_cents, 100)

        if city_pnl >= 0:
            return 1.0, None

        pnl_pct = abs(city_pnl) / bank  # 0.0 to 1.0

        if pnl_pct >= 0.30:
            return 0.0, f"risk_mult:city_pnl={city_pnl/100:.2f} <= -30%_bank({city_code})"
        elif pnl_pct >= 0.20:
            return 0.25, f"risk_mult:city_pnl={city_pnl/100:.2f} >= -30%_bank({city_code})"
        elif pnl_pct >= 0.10:
            return 0.50, f"risk_mult:city_pnl={city_pnl/100:.2f} >= -20%_bank({city_code})"
        else:
            return 1.0, None

    # Backward compat: old boolean API wraps the new multiplier
    def check_circuit_breaker(self, city_code: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """Return (ok, reason). False = fully halted (multiplier = 0).

        This is a backward-compatible wrapper. New code should use
        get_city_risk_multiplier() for graduated sizing.
        """
        mult, reason = self.get_city_risk_multiplier(city_code)
        if mult == 0.0:
            return False, reason or f"circuit_breaker:halted({city_code})"
        return True, None

    def check_brier_gate(self, brier_summary: dict) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_not). Blocks new entries if model Brier gate is RED
        after a meaningful sample (default ≥150 trades)."""
        if not self.research_mode.enabled:
            return True, None
        if self.research_mode.allow_red_gate:
            return True, None
        n_trades = int(brier_summary.get("n_trades", 0))
        gate_status = brier_summary.get("gate_status", "WAITING")
        if n_trades >= self.research_mode.brier_min_trades and gate_status == "RED":
            skill = brier_summary.get("brier_skill_score")
            return False, (
                f"brier_gate:RED({n_trades} trades >= {self.research_mode.brier_min_trades}, "
                f"skill={skill})"
            )
        return True, None

    def check_duplicate_exposure(self, ticker: str, existing_tickers: set[str]) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_not). Blocks same-ticker re-exposure on reruns."""
        if not self.research_mode.enabled:
            return True, None
        if self.research_mode.allow_duplicates:
            return True, None
        if ticker in existing_tickers:
            return False, f"duplicate_exposure:{ticker}"
        return True, None

    def check_open_positions(self, current_open: int) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_not). Caps total concurrent exposure.

        2026-06-19 P&L work: closes the long-documented max_open_positions gap.
        `current_open` should count open positions + resting maker quotes.
        max_open_positions <= 0 disables the cap.
        """
        if self.max_open_positions <= 0:
            return True, None
        if current_open >= self.max_open_positions:
            return False, f"max_open_positions:{current_open}>={self.max_open_positions}"
        return True, None

    def live_size_scale(self) -> float:
        """Scale-on-proof multiplier for LIVE Kelly sizing (2026-06-19 P&L work).

        Live exposure should grow only once the (realistic-fill) paper stream
        proves a positive edge. We proxy "proof" with the week's realised P&L
        relative to the bankroll:
          week P&L >= 0           → 1.00 (full size)
          down to -2% of bankroll → 0.50
          worse than -2%          → 0.25 (keep collecting data, minimal risk)
        Env KALSHI_WEATHER_LIVE_SIZE_SCALE overrides with a fixed value.
        Always in [0.25, 1.0]; never increases beyond full Kelly fraction.
        """
        override = os.environ.get("KALSHI_WEATHER_LIVE_SIZE_SCALE", "")
        if override:
            try:
                return max(0.0, min(1.0, float(override)))
            except ValueError:
                pass
        bank = max(self._total_bankroll_cents, 100)
        wk = self._week_pnl_cents
        if wk >= 0:
            return 1.0
        drawdown_pct = abs(wk) / bank
        if drawdown_pct <= 0.02:
            return 0.5
        return 0.25

    def live_size_rung(self, live_fills_total: int, live_total_cents: int) -> int:
        """Per-market $ cap from a GATED ladder — the scale-UP counterpart to
        live_size_scale (which only scales down). Used only when the caller opts in
        (KALSHI_WEATHER_SIZE_RAMP=1); default behaviour elsewhere is the fixed cap.

        Promotes to the next rung only on PROOF: at least RUNG_MIN_FILLS live fills at
        the current rung AND positive live P&L since the rung started. Demotes one rung
        on a drawdown worse than RUNG_STOP_DOLLARS. P&L is measured from a WEATHER-ONLY
        balance-equivalent (lifetime weather realized P&L + current weather open marks; see
        sync_live_positions.weather_ramp_total_cents) — ground-truth Kalshi money on the realized
        leg, structurally excluding non-weather account activity (sports / KXUSAIRANAGREEMENT). This
        method only DIFFS the integer the caller supplies; the weather-only scoping lives at the caller.

        Caller passes the current cumulative live fill count and total balance (cents).
        Returns the allowed per-market dollar cap for the (possibly updated) rung.
        Env: KALSHI_WEATHER_SIZE_LADDER (default "5,10,25,50"),
             KALSHI_WEATHER_RUNG_MIN_FILLS (default 50),
             KALSHI_WEATHER_RUNG_STOP_DOLLARS (default 10).
        """
        ladder = [int(x) for x in os.environ.get("KALSHI_WEATHER_SIZE_LADDER", "5,10,25,50").split(",")
                  if x.strip().isdigit()]
        if not ladder:
            ladder = [5]
        min_fills = int(os.environ.get("KALSHI_WEATHER_RUNG_MIN_FILLS", "50"))
        stop_cents = int(os.environ.get("KALSHI_WEATHER_RUNG_STOP_DOLLARS", "10")) * 100

        idx = max(0, min(self._rung_idx, len(ladder) - 1))
        # Initialise the rung baseline on first use (no promotion on the seeding call).
        if self._balance_at_rung_start == 0:
            self._balance_at_rung_start = live_total_cents
            self._fills_at_rung_start = live_fills_total
            self._rung_idx = idx
            self._save_state()
            return ladder[idx]

        fills_since = live_fills_total - self._fills_at_rung_start
        pnl_since = live_total_cents - self._balance_at_rung_start

        new_idx, reason = idx, None
        if idx > 0 and pnl_since <= -stop_cents:
            new_idx = idx - 1
            reason = f"DEMOTE rung {idx}->{new_idx} (P&L ${pnl_since/100:.2f} <= -${stop_cents/100:.0f})"
        elif idx < len(ladder) - 1 and fills_since >= min_fills and pnl_since > 0:
            new_idx = idx + 1
            reason = f"PROMOTE rung {idx}->{new_idx} ({fills_since} fills>= {min_fills}, P&L ${pnl_since/100:.2f}>0)"

        if new_idx != idx:
            self._rung_idx = new_idx
            self._fills_at_rung_start = live_fills_total
            self._balance_at_rung_start = live_total_cents
            self._save_state()
            try:
                from trader.notify import alert
                alert(f"📊 size ramp {reason} -> ${ladder[new_idx]}/market", key="size_ramp_change", dedup_seconds=60)
            except Exception:
                pass
            idx = new_idx
        return ladder[idx]

    def record_settlement(self, pnl_cents: int, city_code: Optional[str] = None,
                          update_budget: bool = True) -> None:
        """Update consecutive loss counters AND city PnL after a settlement.

        Called from settle_paper.py. Positive pnl → streak resets. Negative →
        streak increments AND city PnL accumulates downward for the dynamic
        Kelly multiplier.

        2026-06-19 audit fix: Also updates today/week_pnl_cents so the
        dollar kill-switch (check_budget) can see live losses.

        2026-06-19 P&L work: `update_budget` lets the LIVE caller
        (sync_live_positions.py) skip the today/week update, because there the
        dollar counters are fed once from Kalshi's ground-truth balance delta via
        record_budget_pnl(). Without this, live P&L was double-counted into the
        kill-switch (tripping at ~half the real loss). Paper (settle_paper.py)
        keeps the default True — it has no balance-delta path.
        """
        if update_budget:
            self._today_pnl_cents += pnl_cents
            self._week_pnl_cents += pnl_cents
        if pnl_cents < 0:
            self._consecutive_losses += 1
            if city_code:
                city_code = city_code.upper()
                self._consecutive_losses_by_city[city_code] = self._consecutive_losses_by_city.get(city_code, 0) + 1
                self._city_pnl_cents[city_code] = self._city_pnl_cents.get(city_code, 0) + pnl_cents
        else:
            self._consecutive_losses = 0
            if city_code:
                city_code = city_code.upper()
                self._consecutive_losses_by_city[city_code] = 0
                self._city_pnl_cents[city_code] = self._city_pnl_cents.get(city_code, 0) + pnl_cents
        self._save_state()

    def record_budget_pnl(self, pnl_cents: int) -> None:
        """Directly update today/week dollar counters for the budget kill-switch.
        
        2026-06-19 audit fix: Allows sync_live_positions.py to feed Kalshi's
        actual balance change into the daily/weekly loss circuit breakers
        without going through per-position settlement logic.
        """
        self._today_pnl_cents += pnl_cents
        self._week_pnl_cents += pnl_cents
        self._save_state()

    def record_settlements_batch(self, records: list) -> None:
        """Apply a batch of realized settlements (budget + consecutive-loss + per-city counters) with a
        SINGLE persist. Atomic w.r.t. a caller's dedup ledger: a mid-batch failure persists NOTHING (the
        fresh in-memory RiskGate is discarded), so the whole feed is safe to retry without
        double-counting the $ kill-switch. Each record: {pnl_cents, city_code}. (sync feed, 2026-06-26)"""
        for r in records:
            pnl = int(r.get("pnl_cents", 0) or 0)
            city = (r.get("city_code") or "").upper() or None
            self._today_pnl_cents += pnl
            self._week_pnl_cents += pnl
            if pnl < 0:
                self._consecutive_losses += 1
                if city:
                    self._consecutive_losses_by_city[city] = self._consecutive_losses_by_city.get(city, 0) + 1
                    self._city_pnl_cents[city] = self._city_pnl_cents.get(city, 0) + pnl
            else:
                self._consecutive_losses = 0
                if city:
                    self._consecutive_losses_by_city[city] = 0
                    self._city_pnl_cents[city] = self._city_pnl_cents.get(city, 0) + pnl
        self._save_state()

    def check_budget(self, prospective_cost_cents: int = 0) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_not). Checks daily + weekly loss limits."""
        daily_limit_cents = -self.daily_max_loss_dollars * 100
        weekly_limit_cents = -self.weekly_max_loss_dollars * 100

        if self._today_pnl_cents + prospective_cost_cents <= daily_limit_cents:
            return False, f"daily_max_loss_exceeded ({(self._today_pnl_cents + prospective_cost_cents)/100:.2f} <= {daily_limit_cents/100:.2f})"
        if self._week_pnl_cents + prospective_cost_cents <= weekly_limit_cents:
            return False, f"weekly_max_loss_exceeded ({(self._week_pnl_cents + prospective_cost_cents)/100:.2f} <= {weekly_limit_cents/100:.2f})"
        return True, None

    def check_event_limit(self, ticker: str, qty: int, price_cents: int) -> bool:
        """Return True if this order won't breach per-event max.

        FIX 2026-07-06 (audit): aggregate over the EVENT (series+city+date), not the
        exact ticker. The premium band drifts across adjacent strikes as the book
        moves (B45 one cycle, B47 the next), so the old exact-ticker match let every
        new strike of the same event pass the cap, multiplying per-event exposure
        well past per_event_max_position_dollars and concentrating live losses on a
        single weather outcome."""
        cost_cents = qty * price_cents
        event = _event_key(ticker)
        existing = sum(
            p.get("count", 0) * p.get("avg_cost_cents", 0)
            for p in self._open_positions
            if _event_key(p.get("ticker", "")) == event
        )
        return (existing + cost_cents) <= self.per_event_max_position_dollars * 100

    def seed_open_positions(self, positions: list[dict]) -> None:
        """Seed the in-memory per-event exposure view (read by check_event_limit) from REAL
        already-held positions. The LIVE path (bin/paper_trade.py) loads an empty open_positions
        from risk-state and never calls record_order, so without this check_event_limit sums an
        EMPTY list → the per-event $ cap collapses to a per-ORDER check and lets multiple strikes
        of one event stack across cycles (audit 2026-07-08). In-memory ONLY: paper_trade.py never
        persists risk state, so this cannot leak into risk-state.json (no cross-cycle double-count).
        Each item needs ticker + count + avg_cost_cents (the fields check_event_limit reads)."""
        self._open_positions = [
            {"ticker": p["ticker"], "count": int(p.get("count", 0) or 0),
             "avg_cost_cents": int(p.get("avg_cost_cents", 0) or 0)}
            for p in positions if p.get("ticker")
        ]

    def add_open_position(self, ticker: str, qty: int, price_cents: int) -> None:
        """Append a just-placed order to the in-memory exposure view so LATER signals in the SAME
        run aggregate toward the per-event cap (record_order is not called on the live path). Not
        persisted; no PnL effect — this only feeds check_event_limit."""
        self._open_positions.append(
            {"ticker": ticker, "count": int(qty), "avg_cost_cents": int(price_cents)}
        )

    # ── Correlated same-side cap (2026-07-13, drawdown fix #1) ───────────
    # Same seed/add/check trio the per-event cap uses, but the bucket key is the BET
    # SIDE (yes|no) rather than the event, so it aggregates same-side exposure ACROSS
    # cities (the 'fade-the-bin book'). All in-memory, no-op unless armed.

    @property
    def correlated_cap_enabled(self) -> bool:
        """True if either correlated-direction knob is armed (both default 0 = off)."""
        return self.max_correlated_direction_events > 0 or self.max_correlated_direction_dollars > 0

    def _accrue_direction(self, ticker: str, side: str, qty: int, price_cents: int) -> None:
        d = _correlated_side(ticker, side)
        if d is None:
            return
        self._dir_events.setdefault(d, set()).add(_event_key(ticker))
        # Worst-case cost of the HELD side = qty * entry price (matches the settlement-log
        # risk basis: a NO at 33¢ risks 33¢/ct, a YES at 25¢ risks 25¢/ct).
        self._dir_notional[d] = self._dir_notional.get(d, 0) + max(0, int(qty)) * max(0, int(price_cents))

    def seed_direction_exposure(self, items: list) -> None:
        """Seed the in-memory same-side exposure view (read by check_correlated_direction)
        from already-held positions + resting makers. Mirrors seed_open_positions: in-memory
        ONLY. Each item needs ticker + side + count + avg_cost_cents. No-op unless armed."""
        self._dir_events = {}
        self._dir_notional = {}
        if not self.correlated_cap_enabled:
            return
        for p in items or []:
            self._accrue_direction(
                p.get("ticker", ""), p.get("side", ""),
                int(p.get("count", 0) or 0), int(p.get("avg_cost_cents", 0) or 0),
            )

    def add_direction_exposure(self, ticker: str, side: str, qty: int, price_cents: int) -> None:
        """Accrue a just-placed order into the in-run side buckets so LATER signals in the
        SAME run count it (record_order is not called on the live path). No-op unless armed."""
        if not self.correlated_cap_enabled:
            return
        self._accrue_direction(ticker, side, qty, price_cents)

    def direction_bucket_counts(self) -> dict:
        """Distinct same-side event counts currently seeded/accrued, per side, e.g.
        {'yes': 6, 'no': 0}. For observability logging — how close each side is to the cap."""
        return {k: len(v) for k, v in self._dir_events.items()}

    def check_correlated_direction(self, ticker: str, side: str, qty: int,
                                   price_cents: int) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_not). Caps SAME-SIDE exposure ACROSS cities: the count of
        DISTINCT daily high/low events (series+city+date) held on this side, and optionally the
        worst-case $ notional in that side bucket. Adding another STRIKE of an event already in
        the bucket does NOT raise the event count (intra-event exposure is check_event_limit's
        job). Returns (True, None) — no block — when neither knob is armed or the ticker is
        excluded (KXTEMP hourly / non-weather)."""
        if not self.correlated_cap_enabled:
            return True, None
        d = _correlated_side(ticker, side)
        if d is None:
            return True, None
        events = self._dir_events.get(d, set())
        is_new_event = _event_key(ticker) not in events
        if self.max_correlated_direction_events > 0 and is_new_event:
            prospective = len(events) + 1
            if prospective > self.max_correlated_direction_events:
                return False, (f"corr_side:{d} events {prospective}>"
                               f"{self.max_correlated_direction_events}")
        if self.max_correlated_direction_dollars > 0:
            prospective_cents = self._dir_notional.get(d, 0) + max(0, int(qty)) * max(0, int(price_cents))
            if prospective_cents > self.max_correlated_direction_dollars * 100:
                return False, (f"corr_side:{d} ${prospective_cents/100:.2f}>"
                               f"${self.max_correlated_direction_dollars}")
        return True, None

    def check_run_orders(self, already_placed: int) -> bool:
        """Return True if we can still place another order this run."""
        return already_placed < self.per_run_max_orders

    # ── Mutation ────────────────────────────────────────────────────────

    def record_order(self, ticker: str, side: str, qty: int, price_cents: int, order_id: str) -> None:
        """Record an open position. Does NOT mark realized PnL."""
        self._open_positions.append({
            "ticker": ticker,
            "side": side,
            "count": qty,
            "price_cents": price_cents,
            "avg_cost_cents": price_cents,
            "order_id": order_id,
            "opened_utc": datetime.now(timezone.utc).isoformat(),
        })
        # Approximate mark-to-market debit
        cost_cents = qty * price_cents
        self._today_pnl_cents -= cost_cents
        self._week_pnl_cents -= cost_cents
        self._save_state()

    def record_fill(self, order_id: str, filled_count: int, fill_price_cents: int) -> None:
        """Update position after partial/full fill."""
        for p in self._open_positions:
            if p.get("order_id") == order_id:
                p["count"] = filled_count
                p["avg_cost_cents"] = fill_price_cents
                break
        self._save_state()

    def record_close(self, order_id: str, exit_price_cents: int) -> None:
        """Mark position as closed and realize approximate PnL."""
        for i, p in enumerate(self._open_positions):
            if p.get("order_id") == order_id:
                entry = p.get("avg_cost_cents", 0)
                qty = p.get("count", 0)
                side = p.get("side", "yes")
                # YES: profit = exit - entry; NO: profit = (100-exit) - (100-entry) = entry - exit
                realized = qty * (exit_price_cents - entry) if side == "yes" else qty * (entry - exit_price_cents)
                self._realized_today_cents += realized
                self._today_pnl_cents += realized
                self._week_pnl_cents += realized
                self._open_positions.pop(i)
                self._save_state()
                return

    def get_summary(self) -> dict:
        """Current risk summary for logging / JSON output."""
        return {
            "today_pnl_cents": self._today_pnl_cents,
            "today_pnl_dollars": self._today_pnl_cents / 100,
            "week_pnl_cents": self._week_pnl_cents,
            "week_pnl_dollars": self._week_pnl_cents / 100,
            "realized_today_cents": self._realized_today_cents,
            "open_positions": len(self._open_positions),
            "daily_max_loss_dollars": self.daily_max_loss_dollars,
            "weekly_max_loss_dollars": self.weekly_max_loss_dollars,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_losses_by_city": self._consecutive_losses_by_city,
            "city_pnl_cents": self._city_pnl_cents,
            "halted_cities": self.get_halted_cities(),
            "manual_halted_cities": sorted(set(self._halted_cities)),
            "city_loss_circuit_breaker_threshold": self.city_loss_circuit_breaker_threshold,
            "max_correlated_direction_events": self.max_correlated_direction_events,
            "max_correlated_direction_dollars": self.max_correlated_direction_dollars,
            "research_mode": {
                "enabled": self.research_mode.enabled,
                "min_price_cents": self.research_mode.min_price_cents,
                "brier_min_trades": self.research_mode.brier_min_trades,
                "allow_red_gate": self.research_mode.allow_red_gate,
                "allow_duplicates": self.research_mode.allow_duplicates,
                "disable_city_circuit_breaker": self.research_mode.disable_city_circuit_breaker,
            },
        }

    def get_open_positions(self) -> list[dict]:
        return list(self._open_positions)

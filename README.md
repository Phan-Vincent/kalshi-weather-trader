# Kalshi Weather Fair-Value System

Upstream data + fair-value model layer for Kalshi daily city temperature markets (KXHIGHT*, KXLOWT*, KXTEMP*H hourly).

> **⚠️ Status (2026-06-02): PAPER-ONLY, no proven edge — do NOT go live.**
> Full audit (profitability, security, settlement diagnosis, applied fixes, open issues):
> [`research/agent-reports/kalshi-audit-2026-06-02.md`](../../research/agent-reports/kalshi-audit-2026-06-02.md).
> Headline open item: the Gaussian-fit-to-daily-max model is statistically mis-specified — that's why
> calibration (Brier) shows no edge vs. market. Plumbing was fixed 2026-06-02; the **model** was not.

## Files

| File | Purpose |
|------|---------|
| `data/weather_data.py` | NWS NBM + Open-Meteo fetchers, station catalogue, ticker parser |
| `model/fair_value.py` | Gaussian CDF fair-probability estimator |
| `bin/build_fair_values.py` | End-to-end CLI: query Kalshi markets, fetch forecasts, write fair-values.json |
| `cache/` | 1-hour TTL forecast cache |

## Usage

```bash
python3 bin/build_fair_values.py \
    --series KXHIGHTHOU,KXHIGHTNYC,KXHIGHTBOS,KXLOWTHOU \
    --out fair-values.json
```

## fair-values.json Schema

```json
{
  "generated_at_utc": "2026-05-28T19:00:00+00:00",
  "series_queried": ["KXHIGHTHOU"],
  "markets_processed": 42,
  "records": [
    {
      "ticker": "KXHIGHTHOU-26MAY29-T95",
      "fair_prob": 0.2045,
      "ci_low": 80.94,
      "ci_high": 95.06,
      "source": "blend",
      "confidence": "high",
      "model_temp_forecast": 88.0,
      "model_temp_std": 3.6,
      "rationale": "Daily high ~ N(88.00, 3.60^2); P(T_max > 95) = 0.2045",
      "asof_utc": "2026-05-28T19:00:00+00:00",
      "_market": { ... raw kalshi-cli market JSON ... },
      "_market_mid": 0.15,
      "_edge_vs_mid": 0.0545
    }
  ]
}
```

## JSON Contract for Trader Subagent

Each record in `records` has:
- `fair_prob`: model-implied probability market settles YES (0.0–1.0)
- `confidence`: `high` | `med` | `low` — use as position-sizing filter
- `_market_mid`: mid price from yes_bid/yes_ask (0.0–1.0)
- `_edge_vs_mid`: `abs(fair_prob - market_mid)` — larger = more edge
- `source`: `blend` (NWS+Open-Meteo), `nws_only`, `openmeteo_only`
- `model_temp_forecast` / `model_temp_std`: Gaussian params used for CDF integral
- `rationale`: human-readable math summary

**Trader should filter on:** `confidence in ("high", "med")` and `_edge_vs_mid > fee_drag` (1.75¢ at 50/50 = ~0.0175, or 0.00875 after maker rebate).

## Known Limitations

1. **NWS ensemble spread** — NWS gridpoints API does not expose ensemble std directly. We infer a conservative spread from daily min/max range (hi–lo / 4 ≈ 2σ). Floor values (1.2°F <24h, 2.0°F 24–48h, 3.0°F >48h) always apply.
2. **Hourly markets** — `KXTEMP*H` settlement uses exact UTC hour from ticker. Forecast resolution is 1h; interpolation is not yet implemented.
3. **Below markets** — `T` tickers can be either above or below depending on market title. We infer direction from the `title` field; ambiguous cases default to `above`.
4. **No historical calibration** — Gaussian assumption is unbacktested. Use with position limits.
5. **SSL** — macOS cert bundles may need `ssl.CERT_NONE` workaround (already applied).

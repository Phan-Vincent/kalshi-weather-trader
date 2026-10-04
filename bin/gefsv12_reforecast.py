#!/usr/bin/env python3
"""bin/gefsv12_reforecast.py — SCAFFOLD: GEFSv12 reforecast APCP ingester for the rain-firewall escalation.

The quick persistence+ENSO firewall (bin/rain_firewall_backtest.py) reads KILL-SUPPORTING but cannot test
the one remaining component of a real forecast edge: within-month SYNOPTIC skill (days 1–~16). Testing
THAT leak-free needs a genuine month-open ensemble forecast at fixed init — i.e. the GEFSv12 REFORECAST
(2000–2019), which this module pulls, mirroring data/gefs_ingester.py's byte-range GRIB2 pattern.

SCOPE OF THIS SCAFFOLD:
  • RUNNABLE NOW, validated end-to-end: fetch one month-open init's APCP for one/all of the 11 KXRAIN
    stations, de-accumulate to a day-1..16 total per member, print the ensemble distribution + P(>N).
    `python3 bin/gefsv12_reforecast.py --init 2005-07-01 --city HOU`  (~1 member ≈16MB; all 5 ≈90MB)
  • HEAVY TODO (guarded stub, does NOT run here): build_reforecast_month_open_table() — the ~20GB one-time
    2000-2019 batch that feeds bin/rain_firewall_reforecast.py (the sibling rescorer; see that TODO).

DATA (all verified live 2026-07-14, public bucket, no auth):
  bucket  noaa-gefs-retrospective  (NOT the realtime noaa-gefs-pds host)
  key     GEFSv12/reforecast/{YYYY}/{YYYYMMDD}00/{member}/{Days:1-10|Days:10-16|Days:10-35}/apcp_sfc_{init}_{member}.grib2 (+.idx)
  members c00,p01–p04 daily (5); +p05–p10 on WEDNESDAY inits (11). Non-Wed has Days:10-16 (→16d); Wed has
          Days:10-35 (→35d). Detect by probing the .idx, never by weekday assumption.
  APCP    disjoint 6-HOUR buckets (labels 0-6,6-12,…) — a period total is a SUM, not a since-init diff.
          Days:1-10 ALSO interleaves 3h partials (0-3,6-9,…): KEEP ONLY buckets with (end-start)==6, else
          you double-count. Units kg/m² = mm; /25.4 → inches. Grid lon 0-360 → sample at 360+lon.

HONESTY: a day-1 00Z init reaches only 384h/16d (except Wednesday 1st-of-months). So the default is
"1–16d genuine NWP + an ACIS-climatology tail for days 17..EOM" (the tail lives in the rescorer), NOT a
full 30d NWP forecast — do not overclaim. This retrospective bucket ENDS 2019 and can NEVER be a live
input; it validates/kills the model side historically. Expected outcome per the roadmap: still a KILL.
Read-only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from rain_screen_snapshot import CITY_COORDS  # noqa: E402  (11 stations: city → lat,lon,tz,ACIS id)
try:
    from rain_firewall_backtest import THRESHOLDS  # noqa: E402  (the KXRAIN "> N inches" ladder)
except Exception:
    THRESHOLDS = [0.5, 1, 2, 3, 4, 5, 6, 7, 8]

REFO_BASE = "https://noaa-gefs-retrospective.s3.amazonaws.com"
REFO_DAILY_MEMBERS = ["c00", "p01", "p02", "p03", "p04"]
REFO_WEEKLY_MEMBERS = REFO_DAILY_MEMBERS + [f"p{i:02d}" for i in range(5, 11)]
ACCUM_WINDOW_H = 6
MM_PER_IN = 25.4
REFO_YEARS = range(2000, 2020)   # 2000-01-01 .. 2019-12-31
CONCURRENT_WORKERS = 8
CACHE = ROOT / "cache" / "gefsv12-reforecast"
TABLE = ROOT / "state" / "rain-firewall" / "reforecast_month_open.jsonl"
_IDX_ACC = re.compile(r"(\d+)-(\d+)\s+hour\s+acc")


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ── pure helpers (unit-tested, no network) ────────────────────────────────────────────────────────

def refo_key(init: date, member: str, folder: str) -> str:
    ymd = f"{init.year}{init.month:02d}{init.day:02d}"
    return f"GEFSv12/reforecast/{init.year}/{ymd}00/{member}/{folder}/apcp_sfc_{ymd}00_{member}.grib2"


def parse_idx(idx_text: str) -> list[dict]:
    """Each .idx line → {n, offset, next_offset, a, b}. next_offset = the NEXT line's offset (None last).
    Only APCP:surface accumulation lines are returned; a/b are the accumulation-window bounds in hours."""
    recs = []
    lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    offs = []
    for ln in lines:
        try:
            offs.append(int(ln.split(":", 3)[1]))
        except Exception:
            offs.append(None)
    for i, ln in enumerate(lines):
        f = ln.split(":", 6)
        if len(f) < 6 or f[3] != "APCP" or f[4] != "surface":
            continue
        m = _IDX_ACC.match(f[5])
        if not m:
            continue
        nxt = offs[i + 1] if i + 1 < len(offs) else None
        recs.append({"n": int(f[0]), "offset": int(f[1]), "next_offset": nxt,
                     "a": int(m.group(1)), "b": int(m.group(2))})
    return recs


def select_6h_buckets(recs: list[dict]) -> list[dict]:
    """The entire de-accumulation step: keep only disjoint 6-hour buckets (drops interleaved 3h partials
    in the Days:1-10 file; harmless no-op on the clean Days:10-16/10-35 files)."""
    return [r for r in recs if (r["b"] - r["a"]) == ACCUM_WINDOW_H]


def month_open_total_in(buckets_mm: list[tuple], max_lead_h: int | None = None) -> float:
    """Sum bucket mm (each tagged by valid-END hour since init) up to max_lead_h, → inches. For a day-1
    00Z init the Days:1-10+Days:10-16 buckets all fall in the target month (≤384h)."""
    tot = sum(mm for (end_h, mm) in buckets_mm if (max_lead_h is None or end_h <= max_lead_h))
    return round(tot / MM_PER_IN, 4)


def bin_prob(threshold: float, member_totals: list[float]) -> float | None:
    """P(month total > threshold) as the fraction of ensemble members exceeding it (5- or 11-member)."""
    if not member_totals:
        return None
    return sum(1 for t in member_totals if t > threshold) / len(member_totals)


# ── network: probe coverage / members ─────────────────────────────────────────────────────────────

def _head_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "kwb-refo/1.0"})
        with urllib.request.urlopen(req, timeout=10, context=_ctx()) as r:
            return r.status == 200
    except Exception:
        return False


def tail_and_members(init: date) -> tuple[str, list[str]]:
    """Probe (not weekday-guess) the extended tail. Days:10-35 present ⇒ 11-member Wednesday run reaching
    35 days; else Days:10-16 ⇒ 5-member daily run reaching 16 days."""
    ext = f"{REFO_BASE}/{refo_key(init, 'c00', 'Days:10-35')}.idx"
    if _head_ok(ext):
        return "Days:10-35", REFO_WEEKLY_MEMBERS
    return "Days:10-16", REFO_DAILY_MEMBERS


# ── network: fetch one (init, member) → per-station 6h bucket series ───────────────────────────────

def _get(url: str, headers=None, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "kwb-refo/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return r.read()


def _download_message(grib_url: str, rec: dict) -> bytes:
    end = f"{rec['next_offset'] - 1}" if rec["next_offset"] is not None else ""
    return _get(grib_url, headers={"User-Agent": "kwb-refo/1.0", "Range": f"bytes={rec['offset']}-{end}"})


def _extract_points(grib_bytes: bytes, stations: dict) -> dict:
    """One GRIB2 message → {city: mm}. Nearest grid cell at 360+lon (grid is 0-360)."""
    import xarray as xr
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=True) as tmp:
        tmp.write(grib_bytes)
        tmp.flush()
        ds = xr.open_dataset(tmp.name, engine="cfgrib", backend_kwargs={"indexpath": ""})
        var = "tp" if "tp" in ds else list(ds.data_vars)[0]
        out = {}
        for city, (lat, lon, *_rest) in stations.items():
            try:
                out[city] = float(ds[var].sel(latitude=lat, longitude=360 + lon, method="nearest").values)
            except Exception:
                pass
        ds.close()
        return out


def _cache_path(init: date, member: str) -> Path:
    return CACHE / f"{init.year}{init.month:02d}{init.day:02d}00_{member}.json"


def fetch_member_series(init: date, member: str, folder: str, stations: dict = CITY_COORDS,
                        use_cache: bool = True) -> dict:
    """ATOMIC UNIT: download the member's 6h APCP buckets across Days:1-10 + the tail folder, extracting
    ALL stations from each message (one download serves all 11). Returns {city: [(valid_end_h, mm), …]}.
    Persists the tiny per-(init,member) point series to disk so the heavy bytes are pulled at most once."""
    cp = _cache_path(init, member)
    if use_cache and cp.exists():
        try:
            raw = json.loads(cp.read_text())
            return {c: [tuple(x) for x in v] for c, v in raw.items()}
        except Exception:
            pass
    series = {c: [] for c in stations}
    for fold in ("Days:1-10", folder):
        key = refo_key(init, member, fold)
        try:
            idx_text = _get(f"{REFO_BASE}/{key}.idx", timeout=15).decode("utf-8", "ignore")
        except Exception:
            continue
        buckets = select_6h_buckets(parse_idx(idx_text))
        grib_url = f"{REFO_BASE}/{key}"

        def _one(rec):
            try:
                pts = _extract_points(_download_message(grib_url, rec), stations)
                return rec["b"], pts
            except Exception:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as ex:
            for res in ex.map(_one, buckets):
                if not res:
                    continue
                end_h, pts = res
                for c, mm in pts.items():
                    series[c].append((end_h, mm))
    if use_cache and any(series.values()):
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps({c: v for c, v in series.items()}))
        except Exception:
            pass
    return series


def fetch_month_open_distribution(year: int, month: int, stations: dict = CITY_COORDS,
                                  cities: list[str] | None = None) -> dict:
    """init = the 1st at 00Z → per-member day-1..16 monthly-so-far totals (inches) for each station."""
    init = date(year, month, 1)
    tail, members = tail_and_members(init)
    sel = {c: stations[c] for c in (cities or list(stations))}
    per_member = {}
    for m in members:
        series = fetch_member_series(init, m, tail, sel)
        per_member[m] = {c: month_open_total_in(series.get(c, [])) for c in sel}
    dist = {c: [per_member[m][c] for m in members if c in per_member[m]] for c in sel}
    return {"init": init.isoformat(), "tail": tail, "n_members": len(members), "members": members,
            "dist": dist}


# ── HEAVY TODO (guarded; does NOT run in the scaffold) ─────────────────────────────────────────────

def build_reforecast_month_open_table(cities: dict = CITY_COORDS, years=REFO_YEARS, confirm: bool = False):
    """Batch the full 2000-2019 month-open table → state/rain-firewall/reforecast_month_open.jsonl (one
    line per city-month-year with per-member day-1..16 totals). This is the ~20GB ONE-TIME download that
    feeds bin/rain_firewall_reforecast.py. Guarded: pass confirm=True to actually run. Resumable — the
    per-(init,member) disk cache (fetch_member_series) means re-runs never re-download. NOT executed here."""
    n_inits = len(list(years)) * 12
    if not confirm:
        print(f"[gefsv12] build_reforecast_month_open_table is a HEAVY stub — ~{n_inits} month-open inits "
              f"× 5–11 members ≈ ~20GB one-time download (no server-side spatial subset). Not running.\n"
              f"  Re-invoke with confirm=True (and expect hours + disk). Resumable via {CACHE.relative_to(ROOT)}/.")
        return
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(TABLE, "a") as out:
        for y in years:
            for mo in range(1, 13):
                try:
                    d = fetch_month_open_distribution(y, mo, cities)
                except Exception as e:
                    print(f"[gefsv12] skip {y}-{mo:02d}: {type(e).__name__}: {e}", file=sys.stderr)
                    continue
                for city, totals in d["dist"].items():
                    out.write(json.dumps({"city": city, "year": y, "month": mo, "init": d["init"],
                                          "tail": d["tail"], "n_members": d["n_members"],
                                          "member_totals_in": totals}) + "\n")
                    written += 1
    print(f"[gefsv12] wrote {written} city-month rows → {TABLE.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="GEFSv12 reforecast APCP ingester (scaffold)")
    ap.add_argument("--init", help="month-open init YYYY-MM-DD (day should be 01)")
    ap.add_argument("--city", default="HOU", help="station code or ALL (default HOU)")
    ap.add_argument("--build-table", action="store_true", help="run the HEAVY 2000-2019 batch (guarded)")
    args = ap.parse_args()

    if args.build_table:
        build_reforecast_month_open_table(confirm=False)   # stays guarded; edit confirm=True to run
        return 0
    if not args.init:
        ap.error("--init YYYY-MM-DD required (or --build-table)")
    d = datetime.strptime(args.init, "%Y-%m-%d").date()
    cities = None if args.city.upper() == "ALL" else [args.city.upper()]
    print(f"[gefsv12] fetching reforecast APCP for init {d.isoformat()} "
          f"(cities: {args.city}) — this Range-GETs GRIB2 from noaa-gefs-retrospective…")
    res = fetch_month_open_distribution(d.year, d.month, cities=cities)
    print(f"  init={res['init']}  tail={res['tail']}  members={res['n_members']}\n")
    for city, totals in res["dist"].items():
        if not totals:
            print(f"  {city}: (no data)"); continue
        ens = sum(totals) / len(totals)
        probs = "  ".join(f">{N:g}in:{bin_prob(N, totals):.2f}" for N in THRESHOLDS if bin_prob(N, totals))
        print(f"  {city}: day1-16 member totals(in) = [{', '.join(f'{t:.2f}' for t in totals)}]  "
              f"ens-mean={ens:.2f}")
        print(f"       P(month-so-far > N): {probs}")
    print("\n  NOTE: this is the day-1..16 NWP portion only; the rescorer (rain_firewall_reforecast.py, "
          "TODO) adds the ACIS climatology tail for days 17..EOM and the gauge bias-correction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

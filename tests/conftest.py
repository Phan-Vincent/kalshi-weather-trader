"""Repo-wide pytest fixtures for the kalshi-weather suite.

Autouse env isolation
──────────────────────
The scanner, RiskGate, and paper_trade branch on ~20 ``KALSHI_WEATHER_*`` flags
(CALIBRATED_EDGE, LEADTIME_FILTER, LIVE_MODE, PREMIUM_MODE, SIDE_BIAS,
QUOTE_REFRESH, STATE_DIR, SIZE_LADDER, …). Several tests mutate these in-process
and never restore them — e.g. tests/test_calibrated_edge_gate.py leaves
``CALIBRATED_EDGE=1`` + ``LEADTIME_FILTER=off`` set. Run standalone
(``python3 tests/test_calibrated_edge_gate.py``) that is harmless, but inside the
shared pytest process the leak bleeds forward into whatever module collects next,
making pass/fail depend on execution order (this is exactly what forced
bin/verify.py's test_scanner_filtering to defend itself with a local
``_pristine_weather_env()`` once it was wired into pytest via test_verify_bin).

This autouse fixture snapshots every ``KALSHI_WEATHER_*`` key *before* each test
and restores that exact set *after*, so no test can leak a weather flag into
another. We touch only the ``KALSHI_WEATHER_*`` namespace (never HOME/PATH/etc.)
— that is the entire surface the engine reads, and it keeps this fixture from
interfering with tests that legitimately rely on the ambient environment.

Note we snapshot/restore rather than *clear*: module-level sets (test_halt,
test_healthcheck, test_size_ramp set flags at import time) are applied during
collection, i.e. identically for every test regardless of order, so they form a
deterministic ambient baseline that is not itself a source of order-dependence.
Clearing them here would break the very modules that set them; restoring the
per-test snapshot is enough to make the suite order-independent.

Autouse notify/halt path isolation
──────────────────────────────────
``KALSHI_WEATHER_ALERTS=0`` stops the outbound *send*, but trader.notify still
appends a ``status='disabled'`` record to the REAL logs/alerts.jsonl (module-level
``_ALERTS_LOG``, computed at import) — by design in production ("nothing is
silently lost"), but under pytest it meant every suite run dumped hundreds of
synthetic lines into the production alert log, drowning real alert history (this
camouflaged the 2026-07-01 'x' halt incident's ~35.5h outage). The second autouse
fixture below (``_isolate_notify_and_halt_paths``) redirects those paths to a
per-test tmp dir so pytest can never write the prod alert log / dedup state /
halt sentinel / halt archive. trader/notify.py itself is deliberately unchanged.
"""
import os
import sys
from pathlib import Path

import pytest

# The trader/ package lives at the repo root, one level above tests/ (which is the
# only dir pytest puts on sys.path for us). ``python3 -m pytest`` from the repo root
# works without this, but the autouse fixture below must be able to
# ``import trader.*`` regardless of the invocation cwd (IDE runners, bin/ wrappers).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_PREFIX = "KALSHI_WEATHER_"


@pytest.fixture(autouse=True)
def _isolate_weather_env():
    """Restore the ``KALSHI_WEATHER_*`` env namespace after every test.

    Also force ``KALSHI_WEATHER_ALERTS=0`` for the duration of every test so a tool's ``main()``
    (futility / markout / spread-cushion — anything that calls ``trader.notify.alert``) can NEVER
    send a real outbound operator alert or burn the shared logs/alert-dedup.json window from a test
    run (a real spurious `spread_cushion_review` alert leaked before this guard — QA review 2026-07-02).
    ``notify.alert`` returns before both the send and the dedup record when ALERTS=0."""
    saved = {k: v for k, v in os.environ.items() if k.startswith(_PREFIX)}
    os.environ["KALSHI_WEATHER_ALERTS"] = "0"
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _isolate_notify_and_halt_paths(monkeypatch, tmp_path):
    """Redirect trader.notify / trader.halt on-disk side effects to a per-test tmp dir.

    WHY: notify._log_local() appends EVERY alert — including the ``status='disabled'``
    records produced when _isolate_weather_env forces ``KALSHI_WEATHER_ALERTS=0`` — to
    the module-level ``_ALERTS_LOG`` path frozen at import time, i.e. the PRODUCTION
    logs/alerts.jsonl. That is correct in production (best-effort local record so no
    alert is silently lost) and is deliberately NOT changed; tests just must never
    reach the prod file (hundreds of synthetic lines had accumulated there, masking
    the 2026-07-01 'x' incident's real 35.5h alert gap).

    CONSTRAINTS:
      * notify._ALERTS_LOG / _DEDUP_STATE are redirected UNCONDITIONALLY. The one test
        that re-patches them itself (tests/test_qa_remaining_fixes.py, QA-08) uses the
        same function-scoped ``monkeypatch`` fixture instance, so its later setattr
        wins inside that test and teardown unwinds in reverse order — composition is
        safe. We patch into a SUBDIR of tmp_path so we can never collide with a
        test's own ``tmp_path / "alerts.jsonl"``-style files.
      * halt._HALT_PATH / _HALT_HISTORY are redirected ONLY while they still point at
        the real prod files. tests/test_halt.py re-pins both at MODULE level (import
        time, plain assignment) and test_sentinel_lives_at_root_state asserts the
        sentinel's literal path — an unconditional patch would break it. The
        conditional keeps that module's re-pin authoritative while giving every OTHER
        test that transitively calls set_halt()/clear_halt() a tmp sentinel/archive:
        belt-and-suspenders on top of halt._refuse_pytest_prod_write() (which guards
        the sentinel but not the logs/halt-history.jsonl archive).
      * Ordering vs _isolate_weather_env: none required — the two autouse fixtures
        touch disjoint state (env-var namespace vs module path constants), so neither
        depends on the other.
    """
    import trader.halt as halt
    import trader.notify as notify

    iso = tmp_path / "notify-isolation"  # the modules mkdir(parents=True) on write
    monkeypatch.setattr(notify, "_ALERTS_LOG", iso / "alerts.jsonl")
    monkeypatch.setattr(notify, "_DEDUP_STATE", iso / "alert-dedup.json")

    # Prod archive path derived the same way halt.py builds _HALT_HISTORY from its
    # state dir (<repo>/state/../logs/halt-history.jsonl), anchored on the module's
    # own never-reassigned _PROD_HALT_PATH constant.
    prod_history = halt._PROD_HALT_PATH.parent.parent / "logs" / "halt-history.jsonl"
    for attr, prod, tmp_name in (
        ("_HALT_PATH", halt._PROD_HALT_PATH, "LIVE_HALT.json"),
        ("_HALT_HISTORY", prod_history, "halt-history.jsonl"),
    ):
        cur = getattr(halt, attr)
        try:
            points_at_prod = cur == prod or cur.resolve() == prod.resolve()
        except Exception:
            points_at_prod = True  # can't prove it is NOT prod → redirect (fail safe)
        if points_at_prod:
            monkeypatch.setattr(halt, attr, iso / tmp_name)

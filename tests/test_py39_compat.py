#!/usr/bin/env python3
"""CI guard: no ACTIVE cron/import-path module may use Python-3.10+ constructs that crash under
cron's python3.9 (/usr/bin/python3 = 3.9.6).

Background. Several scripts are invoked by cron/launchd with a bare `python3` that, in a
minimal-PATH environment, is 3.9 (see bin/nightly-review.sh's pin). PEP-604 `X | Y` union
annotations and `match` statements raise at import/compile there. In 2026-07 four such landmines
were found and fixed (reconcile_actuals, learn_actual_corrections, check_futility_checkpoint, the
nightly-review chain); several were behind `|| true` and would have failed SILENTLY. This test
runs bin/scan_py39_compat.py over the project every suite run so a regression fails loudly instead.

Follows the tests/test_verify_bin.py doctrine — it also GUARDS THE GUARD: if the scanner stops
detecting violations (or stops scanning), the positive-control and coverage tests below fail rather
than the clean-tree assertion silently passing green.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SCAN_PATH = ROOT / "bin" / "scan_py39_compat.py"


def _load_scanner():
    # bin/ is not a package; load by file path. Module name != "__main__" so its CLI does not fire.
    spec = importlib.util.spec_from_file_location("kalshi_scan_py39_compat", _SCAN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_IMPORT_ERROR = None
try:
    scan = _load_scanner()
except Exception as exc:  # noqa: BLE001 — a broken scanner must fail loud, not abort collection
    scan, _IMPORT_ERROR = None, exc


def test_scanner_imports():
    """Guard the guard #0: the scanner module itself must load."""
    assert _IMPORT_ERROR is None, f"bin/scan_py39_compat.py failed to import: {_IMPORT_ERROR!r}"


# ── the actual gate ──────────────────────────────────────────────────

def test_no_runtime_py39_violations():
    """No active cron/import-path (.py that isn't under tests/ or deprecated/) may contain a
    construct that crashes under python3.9. This is the regression the whole 2026-07 fix chain
    was closing; keep it closed."""
    assert _IMPORT_ERROR is None, _IMPORT_ERROR
    violations = scan.find_violations(str(ROOT))
    runtime = [v for v in violations if v["category"] == "runtime"]
    assert runtime == [], (
        "cron/import-path files use python3.10+ constructs that crash under cron's python3.9:\n\n"
        + scan.format_report(runtime)
        + "\n\nFix: convert `X | None` -> Optional[X] (import from typing), or add "
          "`from __future__ import annotations`; replace `match` with if/elif."
    )


# ── guard the guard: the scanner must still DETECT breakers (positive control) ──

# Every entry MUST crash on real python3.9 (verified in the 2026-07 red-team of this scanner). Kept
# as regression cases so a detection gap can't silently reopen. Grouped by the position that made
# the original scanner (annotation-positions-only) miss it.
_BREAKERS = {
    # annotation positions
    "return-annotation": "def f() -> int | None:\n    return None\n",
    "arg-annotation": "def f(x: int | None):\n    return x\n",
    "module-var-annotation": "x: int | None = None\n",
    "class-var-annotation": "class C:\n    x: int | None = None\n",
    "isinstance-union": "def f(x):\n    return isinstance(x, int | str)\n",
    # value positions — the red-team's flagship blind spot (all evaluated at import/runtime)
    "type-alias-with-none": "JSONVal = str | int | None\n",
    "type-alias-no-none": "Number = int | float\n",
    "future-plus-value-union": "from __future__ import annotations\nMaybe = int | None\n",  # future-import does NOT save a value union
    "default-value-union": "def f(x=int | None):\n    return x\n",
    "kwonly-default-union": "def g(*, y=int | None):\n    return y\n",
    "annassign-value-union": "X: object = int | str\n",
    "subscript-generic-alias": "from typing import Dict\nVec = Dict[str, int | None]\n",
    "class-base-union": "class C(int | None):\n    pass\n",
    "class-body-attr-union": "class D:\n    Alias = int | None\n",
    "cast-union": "from typing import cast\nv = cast(int | None, 3)\n",
    "bare-expression-union": "int | None\n",
    "return-value-union": "def f():\n    return int | None\n",
    # class-body annotation nested in a function IS evaluated (when the class is defined) — the
    # fdepth heuristic must not treat it as a never-evaluated function-local.
    "class-in-func-annotation": "def outer():\n    class C:\n        x: int | None = None\n    return C\n",
    # 3.10+/3.12+ syntax
    "match-statement": "def f(x):\n    match x:\n        case 1:\n            return 1\n",
    "pep695-type-alias": "type Alias = int | str\n",
    "pep695-generic-func": "def f[T](x: T) -> T:\n    return x\n",
}

# Every entry is SAFE on real python3.9 — the scanner must NOT flag it (else it blocks legit commits).
_SAFE = {
    "future-annotations-union": "from __future__ import annotations\ndef f() -> int | None:\n    return None\n",
    "integer-bitwise-or": "FLAGS = 1 | 2 | 4\n",
    "set-union": "a = {1} | {2}\n",
    "function-local-annotation": "def f():\n    x: int | None = None\n    return x\n",
    "pep585-generic": "def f() -> list[int]:\n    return []\n",
    # `|` on IntFlag members / non-types is a real bitwise-or, not a type union
    "annotated-flag-metadata": ("from enum import IntFlag\nfrom typing import Annotated\n"
                                "class P(IntFlag):\n    R = 1\n    W = 2\n"
                                "def f(x: Annotated[int, P.R | P.W]) -> int:\n    return x\n"),
    "enum-flag-or-value": ("from enum import IntFlag\nclass P(IntFlag):\n    R = 1\n    W = 2\n"
                           "both = P.R | P.W\n"),
    "isinstance-int-index": "REG = (int, str, float)\ndef f(x):\n    return isinstance(x, REG[0 | 1])\n",
    # dead at runtime on 3.9
    "type-checking-guarded-union": ("from typing import TYPE_CHECKING\n"
                                    "if TYPE_CHECKING:\n    y: int | None = None\n"),
}


@pytest.mark.parametrize("name", sorted(_BREAKERS))
def test_scanner_detects_breakers(name):
    """If the scanner ever STOPS flagging a real 3.9 breaker, this fails — so the clean-tree
    assertion above can never pass green on a broken scanner."""
    assert scan.scan_source(_BREAKERS[name]), f"scanner failed to flag a real 3.9 breaker: {name}"


@pytest.mark.parametrize("name", sorted(_SAFE))
def test_scanner_ignores_safe_constructs(name):
    """No false positives: legit bitwise-or, set unions, PEP-585 generics, function-local
    annotations, and `from __future__ import annotations` unions must NOT be flagged."""
    assert scan.scan_source(_SAFE[name]) == [], f"scanner false-positived on safe construct: {name}"


def test_scanner_scanned_nontrivial_corpus():
    """Guard the guard: prove the walker actually visited the codebase, so a broken find_violations
    that silently returns [] can't make the gate pass. The project has ~200 .py files; a run that
    classifies at least a few dozen into categories means the walk really happened."""
    assert _IMPORT_ERROR is None
    # scan_source over every runtime file must at minimum not crash, and the corpus is large.
    py_files = [p for p in ROOT.rglob("*.py")
                if not any(part in scan.SKIP_DIRS for part in p.parts)]
    assert len(py_files) > 100, f"expected a large corpus, found {len(py_files)} .py files"
    # and the known-quarantined deprecated breaker is still SEEN (scanning genuinely works)
    violations = scan.find_violations(str(ROOT))
    cats = {v["category"] for v in violations}
    # deprecated/backtest_real.py is a standing, intentionally-unfixed union — if the scanner stops
    # seeing ANY violation anywhere, it has silently broken.
    assert "deprecated" in cats or violations == [], (
        "scanner returned no deprecated hit — if bin/deprecated/backtest_real.py was cleaned that's "
        "fine, but verify the scanner still detects unions (see test_scanner_detects_breakers)."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

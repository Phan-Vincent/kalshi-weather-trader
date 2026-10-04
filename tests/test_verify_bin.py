#!/usr/bin/env python3
"""Wire bin/verify.py into the pytest suite so its assertions can't silently rot.

bin/verify.py is a standalone smoke test (run directly: ``python3 bin/verify.py``),
but because it lives under bin/ and is not named ``test_*.py``, pytest never
collected it — so ``test_scanner_filtering`` sat broken for weeks with nothing
catching it (the only other caller, tests/run_all_tests.py, is a manual runner
that pytest does not collect and no cron invokes). This wrapper imports that
module by file path and runs each of its ``test_*`` functions as its own pytest
case, so a future regression fails the suite. bin/verify.py stays runnable on its
own — this only adds the safety net.
"""
import importlib.util
from pathlib import Path

import pytest

_VERIFY_PATH = Path(__file__).resolve().parent.parent / "bin" / "verify.py"


def _load_verify():
    # Load bin/verify.py by path (bin/ is not a package). The module name differs
    # from "__main__", so its `if __name__ == "__main__"` runner does NOT fire on
    # import — we invoke the individual test_* functions ourselves below.
    spec = importlib.util.spec_from_file_location("kalshi_bin_verify", _VERIFY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Import at collection time, but CAPTURE any error instead of letting it raise —
# a bare raise here would turn a broken bin/verify.py into a whole-module
# collection error, and (worse) an empty discovery list would make the
# parametrized runner below collect zero cases and pytest would report it GREEN
# via a silent skip — i.e. the anti-rot wrapper would itself rot. We surface both
# as loud, ordinary test FAILURES via the guard test.
_IMPORT_ERROR = None
try:
    _VERIFY = _load_verify()
    _VERIFY_TESTS = sorted(
        name
        for name in dir(_VERIFY)
        if name.startswith("test_") and callable(getattr(_VERIFY, name))
    )
except Exception as exc:  # noqa: BLE001 — any import-time failure must fail loud, not abort collection
    _VERIFY, _VERIFY_TESTS, _IMPORT_ERROR = None, [], exc

# bin/verify.py currently defines 5 test_* functions; assert we still find them all.
_EXPECTED_MIN_TESTS = 5


def test_bin_verify_discovery():
    """Guard the guard: bin/verify.py must import cleanly and still expose its
    smoke tests. If it stops importing, or its test_* functions get renamed or
    removed, this fails loudly instead of the parametrized runner silently
    collecting nothing and passing green."""
    assert _IMPORT_ERROR is None, f"bin/verify.py failed to import: {_IMPORT_ERROR!r}"
    assert len(_VERIFY_TESTS) >= _EXPECTED_MIN_TESTS, (
        f"expected >= {_EXPECTED_MIN_TESTS} test_* fns in bin/verify.py, "
        f"found {_VERIFY_TESTS} — did they get renamed/removed?"
    )


@pytest.mark.parametrize("name", _VERIFY_TESTS or ["<none-discovered>"])
def test_bin_verify(name):
    """Run each bin/verify.py smoke test as a first-class pytest case."""
    if _IMPORT_ERROR is not None or not _VERIFY_TESTS:
        pytest.fail(f"bin/verify.py not runnable (import_error={_IMPORT_ERROR!r})")
    getattr(_VERIFY, name)()

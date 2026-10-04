#!/usr/bin/env python3
"""bin/supervise-settings.json — the read-only supervisor's permission config. Pins that it is
valid, in dontAsk mode, wired to the deny-gate hook, and free of the allow/deny conflict that once
denied the no-arg halt_live STATUS READ (a `Bash(cmd:*)` deny silently shadows a `Bash(cmd)` exact
allow because deny wins and `:*` also matches the zero-arg form)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "bin" / "supervise-settings.json"


def _cfg():
    return json.loads(SETTINGS.read_text())


def _bash_target(rule: str):
    m = re.match(r"^Bash\((.*)\)$", rule)
    return m.group(1) if m else None


def test_valid_and_dontask():
    c = _cfg()
    assert c["defaultMode"] == "dontAsk"
    assert c["permissions"]["allow"] and c["permissions"]["deny"]


def test_hook_wired_and_exists():
    c = _cfg()
    cmds = [h["command"] for grp in c["hooks"]["PreToolUse"] for h in grp["hooks"]]
    assert any("supervise-readonly-gate.py" in cmd for cmd in cmds)
    assert (ROOT / "bin" / "supervise-readonly-gate.py").exists()


def test_no_exact_allow_is_shadowed_by_a_wildcard_deny():
    c = _cfg()
    deny_prefixes = {t[:-2] for r in c["permissions"]["deny"]
                     if (t := _bash_target(r)) and t.endswith(":*")}
    for r in c["permissions"]["allow"]:
        t = _bash_target(r)
        if not t or "*" in t:
            continue                      # only exact-match allows can be shadowed
        assert t not in deny_prefixes, f"exact allow Bash({t}) is shadowed by a Bash({t}:*) deny"


def test_allowed_reporters_exist():
    c = _cfg()
    for r in c["permissions"]["allow"]:
        t = _bash_target(r)
        if t and t.startswith("python3 bin/"):
            script = re.sub(r":\*$", "", t.split()[1])     # bin/<reporter>.py
            assert (ROOT / script).exists(), f"allowed reporter missing: {script}"


def test_dangerous_scripts_are_denied():
    c = _cfg()
    deny_blob = " ".join(c["permissions"]["deny"])
    for tok in ("run_variants", "flatten_live", "sync_live_positions", "python3 -c", "rm"):
        assert tok in deny_blob, f"expected {tok!r} in the deny-list"

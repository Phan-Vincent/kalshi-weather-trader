#!/usr/bin/env python3
"""PreToolUse deny-gate for the read-only kalshi-weather supervisor (wired via bin/supervise-settings.json).

The supervisor runs headless `claude -p` and must be strictly READ-ONLY even under prompt-injection
(it reads attacker-influenceable inputs: logs, alerts.jsonl, maker-lifecycle rows). The settings.json
allow-list + `dontAsk` already deny-by-default and Claude Code splits compound commands on | && ; & —
but two gaps remain that this hook BACKSTOPS by blocking unconditionally (exit 2, before permission
rules are evaluated):
  1. a file-write redirect appended to an otherwise-allowed read, e.g. `cat state/x > state/y`;
  2. a mutation hidden inside command substitution `$(...)` / backticks / a compound chain.

It denies any Bash command that (a) contains a mutation/exec token ANYWHERE in the raw string
(so a `$(rm …)` or `… | flatten_live` is caught), or (b) writes to a non-/dev file. Benign
substitution like `$(date +%F)` and a literal '>' inside a quoted notify message are preserved
(the redirect scan de-quotes first, so quoted text can't look like an operator). Hooks run OUTSIDE
the permission allow-list, so this script is not itself restricted. Fail-CLOSED on bad input.

Input contract: JSON on stdin, {"tool_name": "...", "tool_input": {"command": "..."}}.
See docs.anthropic.com/en/docs/claude-code/hooks (PreToolUse permissionDecision).
"""
import json
import re
import sys

# Mutation / exec / trading tokens. Any occurrence (incl. inside $(...) or backticks or after a
# pipe) denies. The trading-script names are distinctive enough not to false-positive on a status
# message; the allow-list is the primary gate and this is a backstop.
_DANGER = re.compile(
    r"(?:\brm\b|\brmdir\b|\bmv\b|\bcp\b|\btee\b|\bdd\b|\btruncate\b|\bln\b|\bmkfifo\b|"
    r"\bchmod\b|\bchown\b|\bkill\b|\bpkill\b|\bkillall\b|"
    r"\bbash\b|\bzsh\b|/bin/sh\b|\bsource\b|\beval\b|\bexec\b|"
    r"\bcurl\b|\bwget\b|\bnc\b|\bssh\b|\bscp\b|"
    r"run-cycle|flatten_live|run_variants|sync_live_positions|place_limit_order|"
    r"set_halt|clear_halt|halt_live\.py\s+--|"          # halt_live with a flag; the no-arg READ is allowed
    r"python3?\s+-c\b|python3?\s+-(?=\s|$)|"            # arbitrary inline python
    r"\bLIVE\s*=\s*1\b)",
    re.IGNORECASE,
)
# A '>' or '>>' NOT targeting /dev/* or an fd-dup (&1/&2) — i.e. a real file write.
_REDIR = re.compile(r">>?(?!\s*(?:/dev/|&))")


def _deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"read-only supervisor gate: {reason}",
    }}))
    raise SystemExit(2)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0   # unreadable input → defer; the allow-list still default-denies under dontAsk
    if data.get("tool_name") != "Bash":
        return 0
    cmd = ((data.get("tool_input") or {}).get("command") or "")
    if _DANGER.search(cmd):
        _deny("mutation/exec token present")
    dequoted = re.sub(r'"[^"]*"|\'[^\']*\'', "", cmd)   # strip quoted text so a '>' in a message is inert
    if _REDIR.search(dequoted):
        _deny("file-write redirect")
    return 0


if __name__ == "__main__":
    sys.exit(main())

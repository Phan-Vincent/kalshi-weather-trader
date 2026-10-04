#!/usr/bin/env python3
"""bin/halt_live.py — operator kill-switch for LIVE trading (stop new orders).

  python3 bin/halt_live.py                 # show current halt status
  python3 bin/halt_live.py --on "reason"   # halt: live arms stop opening new exposure
  python3 bin/halt_live.py --off           # resume live trading

Halting only blocks NEW live orders; to also cancel resting orders run
`python3 bin/flatten_live.py --execute`. Paper arms are unaffected.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader.halt import is_halted, set_halt, clear_halt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Halt/resume LIVE trading (manual kill-switch)")
    ap.add_argument("--on", metavar="REASON", nargs="?", const="manual halt", help="halt live trading")
    ap.add_argument("--off", action="store_true", help="resume live trading")
    args = ap.parse_args()

    if args.on is not None and args.off:
        print("error: pass --on or --off, not both", file=sys.stderr)
        return 2
    if args.on is not None:
        set_halt(args.on, source="operator")
        print(f"🛑 LIVE HALTED: {args.on}")
        print("   New live orders are blocked. Run `bin/flatten_live.py --execute` to cancel resting orders.")
        # A halt leaves resting GTC quotes on the book AND disables the refresh/halt-cancel
        # machinery — alert (not just print) so the flatten reminder reaches the operator and
        # the persistent-halt watchdog has a paired signal (audit 2026-07-06).
        try:
            from trader.notify import alert
            alert(f"🛑 LIVE halted ({args.on}). Resting quotes are NOT auto-cancelled — run "
                  f"`bin/flatten_live.py --execute` to cut exposure, `halt_live.py --off` to resume.",
                  key="live_halt_operator")
        except Exception:
            pass
    elif args.off:
        clear_halt()
        print("✅ LIVE halt cleared — live trading may resume next cycle.")
    else:
        halted, reason = is_halted()
        print(f"LIVE status: {'🛑 HALTED — ' + str(reason) if halted else '✅ active (not halted)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

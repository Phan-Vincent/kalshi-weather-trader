#!/usr/bin/env python3
"""bin/notify.py — CLI for operator alerts (wraps trader.notify.alert).

  python3 bin/notify.py "message"            # deliver via openclaw Telegram
  python3 bin/notify.py "message" --key k     # de-duped by key (1h default)
  python3 bin/notify.py "message" --print     # show the command, send nothing
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader.notify import alert  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Send a kalshi-weather operator alert")
    p.add_argument("message")
    p.add_argument("--key", default=None, help="de-dupe key")
    p.add_argument("--dedup-seconds", type=float, default=3600)
    p.add_argument("--print", dest="dry", action="store_true", help="print command, don't send")
    a = p.parse_args()
    ok = alert(a.message, key=a.key, dedup_seconds=a.dedup_seconds, dry_run=a.dry)
    if not a.dry:
        print("delivered" if ok else "not-delivered (see logs/alerts.jsonl)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

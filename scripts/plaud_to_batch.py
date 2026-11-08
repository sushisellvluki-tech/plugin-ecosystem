#!/usr/bin/env python3
"""
Convert a Plaud-like JSON export into meetings__import_batch arguments.

Does not call the server. Pipe the JSON into import_batch yourself
(HTTP / MCP / cabinet), or use it as a reviewable intermediate file.

Usage:
  python scripts/plaud_to_batch.py export.json
  python scripts/plaud_to_batch.py export.json --account my-plaud
  plaud-like-export | python scripts/plaud_to_batch.py -
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.meetings.sources.plaud import normalize_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="JSON file or - for stdin")
    parser.add_argument(
        "--account",
        default="plaud-export",
        help="source_account_id for import_batch (default: plaud-export)",
    )
    args = parser.parse_args()

    if args.path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.path).read_text(encoding="utf-8")

    payload = json.loads(raw)
    batch = normalize_payload(payload, source_account_id=args.account)
    json.dump(batch, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Print one verse's accepted units, with its Greek and its ASV above them.

The companion to `corpus_query.py`. That says how a construction is treated
and names the verses; this shows what one of those verses actually did,
without opening the packet and the accepted file and joining them by token id.
Read-only, and it works for any chapter, which is the point — a reconciliation
reads precedent in chapters other than the one it is fixing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.pipeline import ROOT  # noqa: E402
from alignment_research.reconciliation import render_verse  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: verse_view.py REF   (a verse, such as GAL.2.17)",
              file=sys.stderr)
        return 2
    try:
        print(render_verse(ROOT, args[0]), end="")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

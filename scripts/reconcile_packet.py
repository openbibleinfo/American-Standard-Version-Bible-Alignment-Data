#!/usr/bin/env python3
"""Print the packet a bounded reconciliation would be sent, and send nothing.

Read-only, and the way to see what `RECONCILE=code` actually asks: the same two
halves the driver builds, concatenated as a reader sees them. Building it here
costs a corpus read and no model, so a packet can be checked before a chapter
is paid for.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.adjudication import adjudication_parts  # noqa: E402
from alignment_research.pipeline import ROOT  # noqa: E402
from alignment_research.scope import chapter_scope  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chapter")
    parser.add_argument(
        "--no-report", action="store_true",
        help="leave out the validation report, as a chapter without one is sent",
    )
    args = parser.parse_args(argv)
    scope = chapter_scope(args.chapter)
    report = ROOT / f"reports/{scope.slug}-validation.md"
    try:
        stable, packet = adjudication_parts(
            ROOT, scope.ref,
            report=(
                "" if args.no_report or not report.exists()
                else report.read_text(encoding="utf-8")
            ),
        )
    except (FileNotFoundError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(stable)
    print(packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

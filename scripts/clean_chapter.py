#!/usr/bin/env python3
"""Reset one chapter to its deterministic, pre-judgment inputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.alignment import ROOT  # noqa: E402
from alignment_research.cleanup import clean_chapter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chapter")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        removed = clean_chapter(ROOT, args.chapter, args.confirm)
    except ValueError as error:
        parser.error(str(error))
    if removed:
        print(f"Removed {len(removed)} chapter-local artifact path(s) for {args.chapter}:")
        for path in removed:
            print(f"  {path.relative_to(ROOT)}")
    else:
        print(f"{args.chapter} is already clean")
    print("Shared contracts, sources, normalized inputs, and global indexes were retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

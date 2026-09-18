#!/usr/bin/env python3
"""Print a chapter's cross-verse candidate table and write its full dump.

Read-only. It reports which lemmas the chapter analysed more than one way and
decides nothing; a candidate is usually the ASV rendering one word differently
in different contexts, and telling that apart from a real split is judgment.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.pipeline import ROOT  # noqa: E402
from alignment_research.reconciliation import view_text  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chapter")
    args = parser.parse_args(argv)
    try:
        print(view_text(ROOT, args.chapter))
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

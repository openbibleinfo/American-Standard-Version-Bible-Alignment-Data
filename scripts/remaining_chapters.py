#!/usr/bin/env python3
"""What is left to align, in canonical order. See alignment_research.remaining."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.remaining import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

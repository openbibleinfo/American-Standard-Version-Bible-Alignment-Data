#!/usr/bin/env python3
"""Drive a chapter as one model session per verse. See alignment_research.driver."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.driver import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Refuse to start another chapter below a requested CLI-window percentage."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.driver import (  # noqa: E402
    short_window_availability,
    windows_below_threshold,
)
from alignment_research.routing import resolve_routes  # noqa: E402


LABELS = {"code": "Claude Code", "codex": "Codex"}


def percentage(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("align", "review", "full"))
    parser.add_argument("until", type=percentage)
    parser.add_argument("--align")
    parser.add_argument("--review")
    parser.add_argument("--reconcile")
    parser.add_argument("--decisions")
    args = parser.parse_args(argv)
    try:
        routes = resolve_routes(
            args.align, args.review, args.reconcile, args.decisions,
        )
    except ValueError as error:
        parser.error(str(error))

    available = short_window_availability(routes, args.stage)
    below = windows_below_threshold(available, args.until)
    readings = ", ".join(
        f"{LABELS[name]} {left}%" if left is not None else f"{LABELS[name]} unreadable"
        for name, left in available.items()
    )
    if below:
        print(
            f"UNTIL={args.until}: {readings}; not starting another chapter",
            file=sys.stderr,
        )
        return 3
    if any(left is None for left in available.values()):
        print(
            f"UNTIL={args.until}: {readings}; proceeding because unreadable "
            "windows do not strand work",
            file=sys.stderr,
        )
    else:
        print(f"UNTIL={args.until}: {readings}; starting next chapter", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

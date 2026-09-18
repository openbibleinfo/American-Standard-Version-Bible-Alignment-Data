#!/usr/bin/env python3
"""Ask the finished chapters how they already treat a construction.

This is the check the contract-promotion rule requires and had no tool for: a
rule that contradicts finished chapters is worse than no rule, and the only way
to know is to count. Filters are ANDed; the output groups hits by treatment, so
whether the corpus is unanimous, lopsided, or split is the answer rather than
something to work out from a list.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.pipeline import ROOT  # noqa: E402
from alignment_research.reconciliation import (  # noqa: E402
    corpus_units, matches, render_corpus_query,
)
from alignment_research.scope import chapter_scope  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lemma", help="source lemma, e.g. ὁ")
    parser.add_argument("--morph", help="morph prefix, e.g. T- or V-PAP")
    parser.add_argument("--target", help="regex over the unit's English text")
    parser.add_argument(
        "--status", choices=["aligned", "unrepresented", "added", "noncompositional"]
    )
    parser.add_argument(
        "--prev", help="morph prefix of the source unit before this one, in "
                       "source order and within the same verse",
    )
    parser.add_argument(
        "--next", help="morph prefix of the source unit after this one — the "
                       "shape most article questions take",
    )
    parser.add_argument("--prev-target", help="regex over the previous unit's English")
    parser.add_argument("--next-target", help="regex over the next unit's English")
    parser.add_argument(
        "--testament", choices=["OT", "NT"],
        help="restrict to one testament; a rule is promoted per testament, so "
             "this is usually what you want",
    )
    parser.add_argument(
        "--like", metavar="CHAPTER",
        help="restrict to the testament of this chapter, and exclude it — ask "
             "the corpus what it does apart from the chapter in hand",
    )
    parser.add_argument("--examples", type=int, default=6)
    parser.add_argument(
        "--notes", action="store_true",
        help="print each example's own note. A precedent whose note concedes "
             "the other reading is weaker evidence than one that does not, "
             "and the counts alone cannot say which kind a row is",
    )
    args = parser.parse_args(argv)

    neighbour = (args.prev, args.next, args.prev_target, args.next_target)
    if not any((args.lemma, args.morph, args.target, args.status, *neighbour)):
        parser.error("give at least one of --lemma, --morph, --target, --status, "
                     "--prev, --next, --prev-target, --next-target")

    testament, exclude = args.testament, None
    if args.like:
        scope = chapter_scope(args.like)
        testament, exclude = testament or scope.testament, scope.ref

    units, skipped = corpus_units(ROOT, testament=testament, exclude=exclude)
    found = list(matches(
        units, lemma=args.lemma, morph=args.morph,
        target=args.target, status=args.status,
        prev_morph=args.prev, next_morph=args.next,
        prev_target=args.prev_target, next_target=args.next_target,
    ))
    described = ", ".join(filter(None, [
        f"lemma {args.lemma}" if args.lemma else "",
        f"morph {args.morph}*" if args.morph else "",
        f"English /{args.target}/" if args.target else "",
        f"status {args.status}" if args.status else "",
        f"after {args.prev}*" if args.prev else "",
        f"before {args.next}*" if args.next else "",
        f"after English /{args.prev_target}/" if args.prev_target else "",
        f"before English /{args.next_target}/" if args.next_target else "",
        f"{testament} only" if testament else "",
        f"excluding {exclude}" if exclude else "",
    ]))
    print(render_corpus_query(
        found, skipped, examples=args.examples, described=described,
        notes=args.notes,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

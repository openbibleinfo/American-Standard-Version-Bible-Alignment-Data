#!/usr/bin/env python3
"""Fetch every outstanding chapter's sources, one at a time, ahead of a drive.

`make drive` fetches a chapter's sources itself, and about one fetch in twenty
fails. Nothing about a chapter causes that: the fetch is a git clone, fetch,
sparse-checkout and detached checkout against four shared repositories under
data/vendor/, and those are shared by every chapter. Two runs in separate
shells that reach their fetch at the same time are two `git checkout --detach`
in one work tree, and the loser gets an index.lock or a half-written checkout
and halts a chapter that was otherwise ready to go.

So this fetches serially, before any of them run. A chapter whose sources are
already on disk is skipped -- `sources_present` is the driver's own test, so
what this leaves behind is exactly what the driver will not fetch again -- and
a chapter that fails is retried after a pause rather than losing its run.

    make prefetch                     everything `make remaining` still lists
    make prefetch LIMIT=20            the first twenty of them
    make prefetch SORT=size           the cheapest chapters first
    make prefetch CHAPTER="EZK.17 EZK.18"
    make prefetch DRY_RUN=1           name what would be fetched, fetch nothing
    make prefetch BATCH=1             one chapter per fetch, the slow way

It writes no alignment artifacts and spends no money: every source it touches
is a pinned input the chapter already asks for. Safe to re-run, and safe to
interrupt -- the next run picks up whatever is still missing.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alignment_research.alignment import ROOT  # noqa: E402
from alignment_research.chapter import log, sources_present  # noqa: E402
from alignment_research.remaining import CHAPTER_COUNTS, remaining_ranges  # noqa: E402
from alignment_research.scope import chapter_scope  # noqa: E402


def fetch(root: Path, refs: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["./scripts/fetch_sources.sh"]
    for ref in refs:
        command += ["--chapter", ref]
    return subprocess.run(command, cwd=root, capture_output=True, text=True)


def fetch_batch(
    root: Path, refs: list[str], sleep: float, retries: int
) -> list[str]:
    """Fetch these chapters together. Returns the ones still missing sources.

    A failed batch is retried, and then taken apart: the failure this exists
    to survive is one chapter's collision or hiccup, and a batch that gave up
    whole would fail two dozen chapters over one of them.
    """
    for attempt in range(1, retries + 2):
        result = fetch(root, refs)
        missing = [ref for ref in refs if not sources_present(root, chapter_scope(ref))]
        if result.returncode == 0 and not missing:
            return []
        if attempt > retries:
            break
        # A transient failure is a collision at a shared checkout or a hiccup
        # at the remote, and both clear on their own. Back off further each
        # time rather than retrying into the same second.
        pause = max(sleep, 0.5) * 4 * attempt
        log("prefetch", f"{len(missing) or len(refs)} chapter(s) failed; "
                        f"retrying in {pause:.1f}s")
        time.sleep(pause)
    if len(refs) == 1:
        tail = result.stderr.strip().splitlines()[-3:]
        log("prefetch", f"{refs[0]} failed: {' / '.join(tail) or 'no stderr'}")
        return refs
    log("prefetch", f"batch of {len(refs)} failed; retrying them one at a time")
    failed: list[str] = []
    for ref in refs:
        if sources_present(root, chapter_scope(ref)):
            continue
        failed += fetch_batch(root, [ref], sleep, retries)
    return failed


def prefetch(
    root: Path, refs: list[str], sleep: float, retries: int, batch: int,
    dry_run: bool,
) -> tuple[int, list[str]]:
    """Fetch each ref that needs it. Returns how many, and which still lack sources.

    Chapters are fetched in groups rather than one at a time. Every source is a
    sparse checkout of a repository that all of them share, and the cost of one
    is the pattern set matched against the whole index, not the file that came
    out: forty chapters of Leviticus want one checkout of the ASV's Leviticus,
    not forty. Deciding and fetching are one pass for a different reason --
    `sources_present` reads the OSHB versification table for an Old Testament
    chapter, which is a third of a second here, so surveying the list and then
    walking it again is minutes of silence before the first line of output.
    """
    failed: list[str] = []
    fetched = 0
    pending: list[str] = []

    def flush() -> None:
        nonlocal fetched, pending
        if not pending:
            return
        log("prefetch", f"fetching {len(pending)}: {' '.join(pending)}")
        failed.extend(fetch_batch(root, pending, sleep, retries))
        fetched += len(pending)
        pending = []
        if sleep:
            time.sleep(sleep)

    for index, ref in enumerate(refs, start=1):
        # Say what is happening before the slow part rather than after it: a
        # silent minute is indistinguishable from a hung script, and the
        # checking is most of the silence.
        log("prefetch", f"{ref} ({index}/{len(refs)}) checking")
        if sources_present(root, chapter_scope(ref)):
            continue
        if dry_run:
            log("prefetch", f"{ref} would be fetched")
            continue
        pending.append(ref)
        if len(pending) >= batch:
            flush()
    flush()
    return fetched, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--chapters", nargs="+", metavar="BOOK.N",
        help="these chapters instead of everything outstanding",
    )
    parser.add_argument("--sort", choices=("canonical", "size"), default="canonical")
    parser.add_argument("--limit", type=int, help="stop after this many chapters")
    parser.add_argument(
        "--sleep", type=float, default=0.0,
        help="seconds between batches (default: 0)",
    )
    parser.add_argument(
        "--batch", type=int, default=25, metavar="N",
        help="chapters per fetch (default: 25); 1 is the old one-at-a-time",
    )
    parser.add_argument(
        "--retries", type=int, default=2,
        help="extra attempts per chapter (default: 2)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    refs = args.chapters or remaining_ranges(sort=args.sort)
    # Validated before anything is fetched, so a typo in a hand-written list
    # is not discovered forty chapters in. The chapter number is checked as
    # well as the book, because `sources_present` answers per book for most of
    # what it needs: EXO.99 has no sources of its own to miss, and would
    # otherwise be reported as already fetched.
    for ref in refs:
        scope = chapter_scope(ref)
        if scope.chapter > CHAPTER_COUNTS[scope.book.code]:
            parser.error(
                f"{ref}: {scope.book.code} has "
                f"{CHAPTER_COUNTS[scope.book.code]} chapters in the ASV"
            )
    if args.limit is not None:
        refs = refs[: args.limit]

    log("prefetch", f"considering {len(refs)} chapter(s)")
    fetched, failed = prefetch(
        ROOT, refs, args.sleep, args.retries, max(1, args.batch), args.dry_run
    )
    if failed:
        log("prefetch", f"{len(failed)} chapter(s) still without sources: "
                        f"{' '.join(failed)}")
        return 1
    if not args.dry_run:
        log("prefetch", f"{fetched} chapter(s) fetched, "
                        f"{len(refs) - fetched} already on disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

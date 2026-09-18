"""Getting a chapter ready to align, and refusing to guess at a judgment.

Every step a chapter needs already exists as its own command, and running them
by hand is the most mechanical work in the project — which is exactly where a
step gets skipped. These are the steps that come before any verse pass —
fetching, textual decisions, preparation — together with the halt that ends a
run and the queue file that records it. `driver.py` walks them and then runs
the passes; this module is what it walks, kept separate because none of it
depends on which model the passes use.

Halting is the design, not an escape hatch. Three things in this pipeline are
judgments — an unresolved ketiv/qere reading, where a Hebrew verse divides, and
whether a chapter taught a rule that belongs in the contract. Automating past any of them would produce a
chapter that validates and is wrong, so each one ends the run with the file to
open and the command to resume.

Judgment does not mean a person, though. A reading is judged by a model pass
like any other alignment when a caller supplies one — see the `resolve`
argument to `ensure_variant_decisions` — and what these steps guarantee is that
the judgment was made and recorded with its evidence, never that a script
guessed it. What has no pass to run still halts.

It is safe to re-run at any point. Every step asks the durable artifacts what
is already done rather than remembering, so a batch that takes a day is
resumed by running the same command again.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .alignment import (
    initialize_variant_decisions,
    initialize_verse_divisions,
    prepare_alignment,
    unresolved_variant_units,
)
from .pipeline import hebrew_lowfat_files, hebrew_oshb_files
from .scope import ChapterScope, chapter_scope


@dataclass
class Halt(Exception):
    """A judgment this driver will not make on someone's behalf."""

    reason: str
    resume: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"halted": self.reason, "resume": self.resume, **self.detail}


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


# What this driver would not decide, one file per chapter. It exists because
# the run that discovers a halt is unattended and its terminal output is gone
# by the time anyone looks: a halt that is only printed is a halt that is
# missed.
#
# One file per chapter rather than one shared list, because chapters are meant
# to run at the same time. A shared list is a read-modify-write on a path every
# run touches, so two chapters halting together would race and the later writer
# would silently drop the earlier one's entry. Here a run only ever writes the
# path named after its own chapter, and the directory listing is the index.
QUEUE_DIRECTORY = "needs-alignment"


def queue_directory(root: Path) -> Path:
    return root / QUEUE_DIRECTORY


def queue_path(root: Path, scope: ChapterScope) -> Path:
    return queue_directory(root) / f"{scope.slug}.md"


def read_queue(root: Path) -> dict[str, str]:
    """Every chapter currently owing a judgment, keyed by chapter."""
    directory = queue_directory(root)
    if not directory.exists():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
    }


def record_queue_entry(
    root: Path, scope: ChapterScope, title: str, lines: list[str]
) -> Path:
    """Replace this chapter's entry, touching no other chapter's file.

    Written to a temporary name and renamed into place so a reader never sees
    a half-written entry and an interrupted run never truncates one.
    """
    path = queue_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([
        f"# {scope.ref} — {title}",
        "",
        "Written by `make drive`. This is something the driver reached and",
        "would not decide on its own, because deciding it wrongly produces a",
        "chapter that validates perfectly and is wrong. Resolve it, then run the",
        "command below. This file is rewritten as the chapter advances and",
        "deleted when it owes nothing.",
        "",
        *lines,
    ]) + "\n"
    pending = path.with_suffix(f".md.{os.getpid()}")
    pending.write_text(body, encoding="utf-8")
    pending.replace(path)
    return path


def clear_queue_entry(root: Path, scope: ChapterScope) -> None:
    queue_path(root, scope).unlink(missing_ok=True)


# The last step shells out to `make finish`, and `make finish` runs the test
# suite. If anything in that suite ever drove this function, the run would
# re-enter itself and fork without bound. A comment is not a guard, so the
# subprocess is marked and a marked environment refuses to start another.
REENTRY_GUARD = "ALIGNMENT_CHAPTER_DRIVER"


def sources_present(root: Path, scope: ChapterScope) -> bool:
    """Whether every source this chapter is built from has been fetched.

    Each sparse checkout is per book or per chapter, so "already fetched" is a
    question about this chapter's own files, not about the vendor directory
    existing. The whole-Bible Greek TSV is the trap: it arrives with the first
    NT book fetched and then answers yes forever, so a second book looked
    present while its ASV text, its MACULA lowfat, and its SBLGNT text were all
    missing — and the build, several steps later, failed on whichever it read
    first.

    Which MACULA Hebrew files a chapter needs is not "the one of its own
    number": it is what the versification table and the Hebrew book's chapter
    list say between them, which is both more (Isaiah 64 reads Isaiah 63) and
    sometimes less (the ASV's Malachi 4 has no Hebrew chapter 4 to read). That
    answer is itself read from the OSHB checkout, so a book whose OSHB is not
    down yet is simply not present, and fetching is what makes the question
    answerable.
    """
    needed = [root / "data/vendor/asv/usx" / scope.book.usx_file]
    if scope.is_hebrew:
        oshb = root / "data/vendor/oshb/wlc" / scope.book.oshb_file
        verse_map = root / "data/vendor/oshb/wlc/VerseMap.xml"
        if not (oshb.exists() and verse_map.exists()):
            return False
        # Its own book, and any other a supplied verse is read from: the ASV's
        # Nehemiah 7:68 is not in WLC's Nehemiah and is read from Ezra 2:66, so
        # a Nehemiah 7 with no Ezra on disk is not fetched yet.
        needed.extend(
            root / "data/vendor/oshb/wlc" / oshb_file
            for oshb_file in hebrew_oshb_files(scope.ref, root)
        )
        needed.extend(
            root / "data/vendor/macula-hebrew/WLC/lowfat" / lowfat_file
            for lowfat_file in hebrew_lowfat_files(scope.ref, root)
        )
    else:
        needed.append(
            root / "data/vendor/macula-greek/SBLGNT/tsv/macula-greek-SBLGNT.tsv"
        )
        needed.append(
            root / "data/vendor/macula-greek/SBLGNT/lowfat"
            / str(scope.book.macula_lowfat_file)
        )
        needed.append(
            root / "data/vendor/sblgnt/data/sblgnt/text"
            / f"{scope.book.sblgnt_name}.txt"
        )
    return all(path.exists() for path in needed)


def ensure_sources(root: Path, scope: ChapterScope) -> None:
    if sources_present(root, scope):
        return
    log("fetch", f"{scope.ref} sources are not on disk; fetching")
    result = subprocess.run(
        ["./scripts/fetch_sources.sh", "--chapter", scope.ref],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0 or not sources_present(root, scope):
        raise Halt(
            f"fetching {scope.ref} did not produce its source files",
            f"make fetch CHAPTER={scope.ref}",
            {"stderr": result.stderr.strip().splitlines()[-5:]},
        )


def ensure_variant_decisions(
    root: Path, scope: ChapterScope,
    resolve: Callable[[str], None] | None = None,
) -> None:
    """Scaffold the chapter's decisions and see that every unit is judged.

    `resolve` is how a caller offers to make the judgments — the driver hands
    in a model pass. The rule is unchanged either way and lives here: a chapter
    with an unjudged unit does not reach preparation, because the projection a
    verse worker receives would otherwise carry a reading nobody chose. Without
    a resolver, or when one leaves units owing, that is a halt.
    """
    path = root / "config" / scope.variant_decisions_name
    if not path.exists():
        log("decisions", f"scaffolding {path.name}")
        initialize_variant_decisions(root, scope.ref)
    unresolved = unresolved_variant_units(root, scope.ref)
    if unresolved and resolve is not None:
        resolve(scope.ref)
        unresolved = unresolved_variant_units(root, scope.ref)
    if unresolved:
        raise Halt(
            f"{scope.ref} has {len(unresolved)} textual decision(s) awaiting review",
            f"resolve config/{path.name}, then re-run",
            {"units": unresolved[:10]},
        )


def ensure_prepared(root: Path, scope: ChapterScope) -> None:
    """Prepare the chapter, turning a division refusal into its own halt."""
    try:
        packets = prepare_alignment(root, scope.ref)
    except ValueError as error:
        if "init-divisions" not in str(error):
            raise
        path = root / "config" / scope.verse_divisions_name
        if not path.exists():
            log("divisions", f"scaffolding {path.name}")
            initialize_verse_divisions(root, scope.ref)
        raise Halt(
            str(error),
            f"resolve config/{path.name} and mark each entry resolved, then re-run",
            {"file": f"config/{path.name}"},
        ) from error
    log("prepare", f"{len(packets)} verse packet(s) ready")

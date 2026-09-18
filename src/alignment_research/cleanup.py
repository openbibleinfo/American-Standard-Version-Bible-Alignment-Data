"""Remove one chapter's judgments and chapter-local derived artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path

from .remaining import CHAPTER_COUNTS
from .scope import ChapterScope, chapter_scope


def cleanable_scope(ref: str, confirmation: str) -> ChapterScope:
    """Resolve an exact canonical chapter reference with typed confirmation."""
    scope = chapter_scope(ref)
    if ref != scope.ref:
        raise ValueError(
            f"chapter must use its canonical spelling {scope.ref!r}, not {ref!r}"
        )
    if scope.chapter > CHAPTER_COUNTS[scope.book.code]:
        raise ValueError(f"chapter {scope.ref} is outside the canon")
    if confirmation != scope.ref:
        raise ValueError(
            f"refusing to clean {scope.ref}: pass --confirm {scope.ref}"
        )
    return scope


def chapter_artifacts(root: Path, scope: ChapterScope) -> list[Path]:
    """Return only judgment-bearing or chapter-local derived paths."""
    slug = scope.slug
    paths = [
        root / "config" / scope.variant_decisions_name,
        root / "config" / scope.verse_divisions_name,
        root / "config" / scope.unsourced_verses_name,
        root / "data" / "alignments" / slug,
        root / "data" / "alignments" / f"{slug}.jsonl",
        root / "data" / "alignments" / f"{slug}-run.json",
        root / "data" / "driver" / slug,
        root / "data" / "batch" / slug,
        root / "data" / "output" / slug,
        root / "data" / "output" / "reverse-interlinear" / f"{slug}.html",
        root / "needs-alignment" / f"{slug}.md",
    ]
    reports = root / "reports"
    if reports.is_dir():
        paths.extend(sorted(reports.glob(f"{slug}-*")))
    return paths


def _remove(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def clean_chapter(root: Path, ref: str, confirmation: str) -> list[Path]:
    """Delete one chapter's LLM workflow and derived outputs, idempotently."""
    scope = cleanable_scope(ref, confirmation)
    root = root.resolve()
    removed = []
    for path in chapter_artifacts(root, scope):
        # All names above derive from a canonical BOOK.N reference. Keep this
        # assertion next to the deletion anyway: a later path addition must
        # remain inside the selected repository root.
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"refusing path outside repository: {path}") from error
        if _remove(path):
            removed.append(path)
    return removed

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .alignment import load_jsonl, prepare_alignment, validate_accepted_alignment
from .diagnostics import LARGE_SOURCE_UNIT_THRESHOLD, large_source_unit_candidates
from .pipeline import ROOT, verse_number
from .scope import chapter_scope


def convention_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Notes repeated across verses, which are conventions nobody recorded.

    The contract already says it: a note that would repeat across many units is
    a convention, and belongs in the log rather than retyped per verse. The
    reason to detect it mechanically is that a verse is supposed to depend on
    earlier verses only through that log. A judgment carried instead by one
    session's memory leaves the chapter consistent, unauditable, and impossible
    to reproduce from its own artifacts -- and invisible, because the ledger
    still validates. This is what makes it visible.
    """
    verses_by_note: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for unit in record["alignments"]:
            note = (unit.get("note") or "").strip()
            if note:
                verses_by_note[note].add(record["ref"])
    return [
        {"note": note, "verses": sorted(verses, key=verse_number)}
        for note, verses in sorted(verses_by_note.items())
        if len(verses) > 1
    ]


def confidence_markdown(confidences: Counter) -> str:
    """The confidence line, for a chapter old enough to have one.

    The field is not recorded past `nt-greek-verse-v58` / `ot-hebrew-verse-v14`,
    so a current chapter has nothing to report and says nothing rather than
    printing an empty mapping that reads like a missing count.
    """
    if not confidences:
        return ""
    return f"- Confidence (recorded before the field was dropped): {dict(sorted(confidences.items()))}\n"


def conventions_markdown(recorded: int, candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return (
            f"- Conventions: {recorded} recorded, and no note repeats across "
            "verses. Every cross-verse judgment travelled through the log."
        )
    repeated = (
        "1 note repeats" if len(candidates) == 1
        else f"{len(candidates)} notes repeat"
    )
    lines = [
        f"- Conventions: {recorded} recorded, and {repeated} across verses. "
        "Each repetition is a chapter-wide judgment that reached later verses "
        "without being recorded, so a verse aligned from the log alone "
        "would not have it:",
    ]
    for item in candidates:
        lines.append(
            f"  - “{item['note']}” — {', '.join(item['verses'])}"
        )
    return "\n".join(lines)


def large_source_units_markdown(candidates: list[dict[str, Any]]) -> str:
    """Render the size heuristic as candidates, never validation failures."""
    label = (
        f"Large-source-unit review candidates "
        f"({LARGE_SOURCE_UNIT_THRESHOLD}+ source morphemes)"
    )
    if not candidates:
        return f"- {label}: none"
    lines = [
        f"- {label}: {len(candidates)}. Size alone is not an error; check "
        "whether each unit is genuinely indivisible:"
    ]
    for item in candidates:
        target = item["target_head"] or "no target head"
        lines.append(
            f"  - {item['ref']} unit {item['unit']}: "
            f"{item['source_count']} source morphemes, {item['status']}, "
            f"target head {target}"
        )
    return "\n".join(lines)


def evaluate(root: Path = ROOT, chapter: str = "JHN.1") -> dict[str, Any]:
    scope = chapter_scope(chapter)
    packets = prepare_alignment(root, scope.ref)
    records = load_jsonl(root / f"data/alignments/{scope.slug}.jsonl")
    ledger = validate_accepted_alignment(root, records, packets, scope.ref)
    statuses = Counter()
    confidences = Counter()
    review_verses = []
    for record in records:
        if record.get("review_notes"):
            review_verses.append(record["ref"])
        for unit in record["alignments"]:
            statuses[unit["status"]] += 1
            # Only where a pre-v59/v15 verse recorded one. A chapter aligned
            # since carries none, and the line is dropped from its report
            # rather than printed as an empty count.
            if "confidence" in unit:
                confidences[unit["confidence"]] += 1
    variant_decisions = load_jsonl(
        root / f"data/output/{scope.slug}/applied-variant-decisions.jsonl"
    )
    conventions_path = root / f"data/alignments/{scope.slug}/conventions.md"
    conventions_recorded = (
        conventions_path.read_text(encoding="utf-8").count("\n- **")
        if conventions_path.exists() else 0
    )
    candidates = convention_candidates(records)
    large_source_units = large_source_unit_candidates(records)
    summary = {
        "ledger": ledger,
        "alignment_statuses": dict(sorted(statuses.items())),
        **({"confidence": dict(sorted(confidences.items()))} if confidences else {}),
        "review_verses": review_verses,
        "large_source_units": {
            "threshold": LARGE_SOURCE_UNIT_THRESHOLD,
            "candidates": large_source_units,
        },
        "conventions": {
            "recorded": conventions_recorded,
            "candidates": candidates,
        },
        "variants": {
            "units": len(variant_decisions),
            "selected": sum(item["status"] == "selected" for item in variant_decisions),
            "underdetermined": sum(item["status"] == "underdetermined" for item in variant_decisions),
            "non_base_selected": sum(
                item["status"] == "selected" and item["selected_reading"] != "r00-base"
                for item in variant_decisions
            ),
        },
    }
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{scope.slug}-validation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = f"""# {scope.book.name} {scope.chapter} alignment validation

The compiled graph contains {ledger['alignment_units']} alignment units covering all {ledger['source_tokens']} selected {scope.source_language['name']} tokens and all {ledger['target_tokens']} ASV tokens in {ledger['verses']} verses. The ledger validator found no missing or duplicated token IDs and every recorded head belongs to its unit.

## Alignment summary

- Statuses: {dict(sorted(statuses.items()))}
{confidence_markdown(confidences)}- Verses carrying review notes: {', '.join(review_verses) if review_verses else 'none'}
{large_source_units_markdown(large_source_units)}
{conventions_markdown(conventions_recorded, candidates)}
- Textual units: {summary['variants']['units']} total; {summary['variants']['selected']} selected and {summary['variants']['underdetermined']} underdetermined; {summary['variants']['non_base_selected']} selected non-base readings

"""
    (reports / f"{scope.slug}-validation.md").write_text(markdown, encoding="utf-8")
    return summary

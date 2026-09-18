"""Non-binding diagnostics that nominate alignments for human/model review."""

from __future__ import annotations

from typing import Any


LARGE_SOURCE_UNIT_THRESHOLD = 3

LARGE_SOURCE_REVIEW_CHECKLIST = (
    "For every flagged large-source-unit candidate, account for each source "
    "morpheme against the English rendering and retain the grouping only if "
    "it is genuinely indivisible."
)


def large_source_unit_candidates(
    records: list[dict[str, Any]],
    threshold: int = LARGE_SOURCE_UNIT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return large units without treating their size as a validity error."""
    candidates = []
    for record in records:
        for ordinal, unit in enumerate(record["alignments"], 1):
            if len(unit["source"]) < threshold:
                continue
            candidates.append(
                {
                    "ref": record["ref"],
                    "unit": ordinal,
                    "source_count": len(unit["source"]),
                    "source": unit["source"],
                    "source_head": unit["source_head"],
                    "target": unit["target"],
                    "target_head": unit["target_head"],
                    "status": unit["status"],
                    "note": unit.get("note", ""),
                }
            )
    return candidates

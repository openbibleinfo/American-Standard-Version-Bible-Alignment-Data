from __future__ import annotations

import difflib
import html
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .alignment import (
    anchor_chapter_variants,
    load_jsonl,
    ref_bounds,
    spanned_refs,
    validate_variant_decisions,
)
from .pipeline import (
    GREEK_WORD_RE,
    ROOT,
    display_surface,
    greek_record_belongs_to_scope,
    greek_source_chapters,
    official_entry_states_unit,
    parse_official_sblgnt,
    records_only_na_edition_change,
)
from .scope import chapter_scope
from .versification import load_supplied_verses


def reading_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        display_surface(token) for token in candidate["expanded_tokens"]
    ) or "∅"


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def terminal_newline(text: str) -> str:
    """Return generated text with exactly one newline at EOF."""
    return text.rstrip("\n") + "\n"


def inline_token_diff(
    base: list[str], projected: list[str], unit_id: str,
    candidate_id: str, title: str,
) -> str:
    attributes = (
        f'data-unit="{html.escape(unit_id, quote=True)}" '
        f'data-candidate="{html.escape(candidate_id, quote=True)}" '
        f'title="{html.escape(title, quote=True)}"'
    )
    pieces = []
    matcher = difflib.SequenceMatcher(a=base, b=projected, autojunk=False)
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        old = " ".join(base[a_start:a_end])
        new = " ".join(projected[b_start:b_end])
        if opcode == "equal":
            pieces.append(html.escape(old))
        elif opcode == "delete":
            pieces.append(f"<del {attributes}>{html.escape(old)}</del>")
        elif opcode == "insert":
            pieces.append(f"<ins {attributes}>{html.escape(new)}</ins>")
        else:
            pieces.append(f"<del {attributes}>{html.escape(old)}</del>")
            pieces.append(f"<ins {attributes}>{html.escape(new)}</ins>")
    return " ".join(piece for piece in pieces if piece)


# The sigla SBLGNT prints to point a word at its apparatus entry: ⸀ for a
# single word, ⸂ ⸃ around a span, and their dotted forms for a second entry in
# the same verse. They are pointers into the apparatus, not text, and this page
# marks the units it changes itself, so printing them too would say the same
# thing twice and claim a mark for every unit the page leaves alone. The
# editorial brackets ⟦ ⟧ and [ ] are kept: those state something about the
# text, not about where to look it up.
APPARATUS_SIGLA = re.compile(r"[\u2e00-\u2e0f]")

def macula_token_pieces(token: dict[str, Any]) -> tuple[str, str, str]:
    """What stands before a token, the token itself, and what follows it."""
    return "", display_surface(token), token.get("after", "")


def official_token_pieces(
    official_verses: dict[str, str],
) -> Callable[[dict[str, Any]], tuple[str, str, str]]:
    """Read each source token's text out of the official edition instead.

    MACULA and official SBLGNT print the same words, but not always the same
    way: in 28 of the New Testament's 7,939 verses they disagree about an
    accent, an elision mark or a sentence-initial capital, and once about a
    spelling. Those are the edition's business, not ours, so a report that says
    it shows SBLGNT shows SBLGNT -- punctuation, brackets and all.

    MACULA still supplies the coordinates. A token's id records where its word
    stands in the Greek verse -- `SBLGNT.JHN.8.10.014` is the fourteenth word
    of John 8:10 -- and that survives the ASV verse and clause restatements,
    which move a token's packet without touching its id. Every verse's
    positions run 1..n, against a word count `validate_greek_surface` holds the
    build to, so the correspondence is exact.
    """
    pieces: dict[str, tuple[str, str, str]] = {}
    for ref, text in official_verses.items():
        # NFC, not the file's own bytes. The edition writes its question mark
        # as an ASCII semicolon 939 times and as U+037E GREEK QUESTION MARK 28
        # -- twice in John against 168 -- and its raised stop as U+00B7 MIDDLE
        # DOT 4,255 times and as U+0387 GREEK ANO TELEIA 3. Both strays are
        # canonically equivalent to the form the edition otherwise uses, so
        # folding them prints what it meant rather than a legacy codepoint that
        # survived an editing pass. It also keeps them out of the Greek block,
        # where `GREEK_WORD_RE` would take them for letters.
        printed = APPARATUS_SIGLA.sub("", unicodedata.normalize("NFC", text))
        spans = [match.span() for match in GREEK_WORD_RE.finditer(printed)]
        for index, (start, end) in enumerate(spans):
            following = (
                printed[end:spans[index + 1][0]] if index + 1 < len(spans)
                # The verse's closing punctuation, and the single space that
                # keeps its last word off the first word of the verse after it.
                # The edition's own lines carry a trailing space of their own.
                else printed[end:].rstrip() + " "
            )
            pieces[f"SBLGNT.{ref}.{index + 1:03d}"] = (
                printed[:start] if index == 0 else "",
                printed[start:end],
                following,
            )

    def token_pieces(token: dict[str, Any]) -> tuple[str, str, str]:
        try:
            return pieces[token["id"]]
        except KeyError:
            raise ValueError(
                f"no official SBLGNT word for source token {token['id']}"
            ) from None

    return token_pieces


def render_diff_verse(
    tokens: list[dict[str, Any]], changes: list[dict[str, Any]],
    record_label: str = "official SBLGNT apparatus",
    token_pieces: Callable[[dict[str, Any]], tuple[str, str, str]] = macula_token_pieces,
) -> str:
    def render_token(index: int) -> str:
        before, word, after = token_pieces(tokens[index])
        if index < len(tokens) - 1 and after and not after.endswith(" "):
            after += " "
        return html.escape(before + word + after)

    pieces = []
    cursor = 0
    for change in sorted(changes, key=lambda item: item["start"]):
        start, end = change["start"], change["end"]
        if start < cursor:
            raise ValueError(f"overlapping selected apparatus units in {tokens[0]['ref']}")
        pieces.append("".join(render_token(index) for index in range(cursor, start)))
        title = (
            f"{change['unit_id']}; {record_label}: "
            f"{change['official_entry']}; ASV evidence: {change['evidence']}"
        )
        # A verse's opening bracket belongs to the verse, not to the reading
        # that happens to begin there, so it is written outside the diff.
        if start < len(tokens):
            pieces.append(html.escape(token_pieces(tokens[start])[0]))
        pieces.append(inline_token_diff(
            [token_pieces(token)[1] for token in tokens[start:end]],
            change["projected_tokens"],
            change["unit_id"], change["candidate_id"], title,
        ))
        # An empty base span inserts between two tokens; the token before it
        # has already been rendered with its own trailing material, so the
        # span has none of its own -- only the space that keeps the inserted
        # word off the one that follows it.
        after = token_pieces(tokens[end - 1])[2] if end > start else ""
        if end < len(tokens) and (end == start or (after and not after.endswith(" "))):
            after += " "
        pieces.append(html.escape(after))
        cursor = end
    pieces.append("".join(render_token(index) for index in range(cursor, len(tokens))))
    # The space a verse carries to hold off the verse after it has nothing to
    # sit before at the end of a section.
    return "".join(pieces).rstrip()


def diff_sections(
    source_tokens: list[dict[str, Any]], changes: list[dict[str, Any]],
    verse_attributes: str, record_label: str = "official SBLGNT apparatus",
    notes_label: str = "Apparatus",
    token_pieces: Callable[[dict[str, Any]], tuple[str, str, str]] = macula_token_pieces,
) -> str:
    """Render one section per verse, joining verses a single change spans.

    An apparatus unit may quote across a verse boundary, and a change is only
    legible whole, so the verses it covers are shown as one section rather
    than torn in two.
    """
    bounds = ref_bounds(source_tokens)
    starts = sorted(start for start, _ in bounds.values())
    crossed = {
        start for start in starts for change in changes
        if change["start"] < start < change["end"]
    }
    cuts = [start for start in starts if start not in crossed] + [len(source_tokens)]
    sections = []
    for block_start, block_end in zip(cuts, cuts[1:]):
        block_changes = [
            {**change, "start": change["start"] - block_start,
             "end": change["end"] - block_start}
            for change in changes
            if block_start <= change["start"] < block_end
        ]
        if not block_changes:
            continue
        tokens = source_tokens[block_start:block_end]
        refs = spanned_refs(tokens)
        label = refs[0] if len(refs) == 1 else (
            f"{refs[0]}–{refs[-1].rsplit('.', 1)[1]}"
        )
        notes = "<br>".join(
            f'<b>{html.escape(notes_label)}:</b> {html.escape(change["official_entry"])}; '
            f'<b>ASV evidence:</b> {html.escape(change["evidence"])}'
            for change in sorted(block_changes, key=lambda item: item["start"])
        )
        sections.append(
            f'<section id="{html.escape(refs[0], quote=True)}">'
            f'<h2>{html.escape(label)}</h2>'
            f'<p class="verse" {verse_attributes}>'
            f'{render_diff_verse(tokens, block_changes, record_label, token_pieces)}</p>'
            f'<p class="evidence">{notes}</p>'
            f'</section>'
        )
    return "\n  ".join(sections)


def official_apparatus_entries(
    path: Path, book_name: str, book_code: str, chapter: int
) -> dict[str, list[str]]:
    """Preserve each official plain-text apparatus entry without interpreting it."""
    header = re.compile(rf"^{re.escape(book_name)} (\d+):(\d+)$")
    entries: dict[str, list[str]] = defaultdict(list)
    active_ref: str | None = None
    active_verse: int | None = None
    lines: list[str] = []

    def flush() -> None:
        if active_ref is None or active_verse is None:
            return
        text = " ".join(lines).replace("\N{NO-BREAK SPACE}", " ")
        for part in re.split(r"\s*•\s*", text):
            compact = " ".join(part.split())
            compact = re.sub(
                rf"^(?:{chapter}:)?{active_verse}\s+", "", compact, count=1
            )
            if compact:
                entries[active_ref].append(compact)

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        match = header.match(raw_line.strip())
        if match:
            flush()
            found_chapter = int(match.group(1))
            active_verse = int(match.group(2)) if found_chapter == chapter else None
            active_ref = (
                f"{book_code}.{chapter}.{active_verse}"
                if active_verse is not None else None
            )
            lines = []
        elif active_ref is not None and raw_line.strip():
            lines.append(raw_line.strip())
    flush()
    return dict(entries)


# An apparatus entry that opens with a verse range, as Philippians 1:16-17 does
# under its `Php 1:16` heading. RP transposes those two verses whole, so the
# apparatus states them as one range rather than twice, while the TEI keeps a
# unit per verse. En dash, hyphen, and the ASCII forms all occur, and the range
# may carry the chapter with it, as Matthew prints `16:2–3` for the passage WH
# brackets whole.
ENTRY_RANGE = re.compile(r"^(?:\d+:)?(\d+)\s*[–—-]\s*(\d+)\s")


def ordered_official_entries(
    official_entries: dict[str, list[str]], scope: Any
) -> list[tuple[set[str], str]]:
    """The chapter's apparatus entries in text order, each with the refs it covers.

    The TEI units and the official entries are two statements of one apparatus,
    so they pair off in order. Pairing them per verse instead was almost the
    same thing and broke on the first entry that spans two verses: its second
    unit sits in the next verse, which then had no entry of its own and looked
    like an apparatus the report had failed to account for.

    Order is the pairing; the span is the check. Keeping both is what makes a
    genuine mismatch — a unit the apparatus does not have — still an error
    rather than a silent shift by one.

    Chapter before verse: a scope can hold entries from two source chapters,
    because SBLGNT Revelation 12:18 is the ASV's 13:1. Ordering on the verse
    number alone filed it after 13:17, and the whole chapter paired one entry
    out of step from there.
    """
    ordered: list[tuple[set[str], str]] = []
    for ref in sorted(
        official_entries,
        key=lambda value: tuple(int(part) for part in value.rsplit(".", 2)[1:]),
    ):
        book, chapter_number, verse = ref.rsplit(".", 2)
        span = {ref}
        for entry in official_entries[ref]:
            found = ENTRY_RANGE.match(entry)
            if found and int(found.group(1)) == int(verse):
                # Stated once for the heading, and its other entries are the
                # rest of the same transposition: Philippians prints the range
                # on `ἀγάπης … κεῖμαι` and leaves the answering
                # `ἐριθείας … δεσμοῖς μου` bullet unmarked.
                span = {
                    f"{book}.{chapter_number}.{number}"
                    for number in range(int(found.group(1)), int(found.group(2)) + 1)
                }
            ordered.append((span, entry))
    return ordered


def generate_textual_report(
    root: Path = ROOT, chapter: str = "JHN.1"
) -> dict[str, Any]:
    """Report how the chapter's closed textual candidates were decided."""
    if chapter_scope(chapter).is_hebrew:
        return generate_hebrew_textual_report(root, chapter)
    return generate_greek_textual_report(root, chapter)


def generate_greek_textual_report(
    root: Path = ROOT, chapter: str = "JHN.1"
) -> dict[str, Any]:
    scope = chapter_scope(chapter)
    source_lock = json.loads(
        (root / "config/sources.lock.json").read_text(encoding="utf-8")
    )["sources"]
    normalized = root / f"data/normalized/{scope.slug}"
    source_tokens = load_jsonl(normalized / "source-tokens.jsonl")
    variants = load_jsonl(normalized / "source-variants.jsonl")
    decisions_document = json.loads(
        (root / "config" / scope.variant_decisions_name).read_text(encoding="utf-8")
    )
    anchored = anchor_chapter_variants(scope, source_tokens, variants, root)
    anchored_by_id = {unit["id"]: unit for unit in anchored}
    decisions = validate_variant_decisions(anchored, decisions_document)
    official_path = (
        root / "data/vendor/sblgnt/data/sblgntapp/text"
        / f"{scope.book.sblgnt_name}.txt"
    )
    official_entries: dict[str, list[str]] = {}
    for source_chapter in greek_source_chapters(scope):
        source_entries = official_apparatus_entries(
            official_path, scope.book.sblgnt_apparatus_name,
            scope.book.code, source_chapter,
        )
        official_entries.update({
            ref: entries for ref, entries in source_entries.items()
            if greek_record_belongs_to_scope(ref, scope)
        })
    official_verses: dict[str, str] = {}
    for source_chapter in greek_source_chapters(scope):
        official_verses.update({
            ref: text
            for ref, text in parse_official_sblgnt(
                root / "data/vendor/sblgnt/data/sblgnt/text"
                / f"{scope.book.sblgnt_name}.txt",
                source_chapter, scope.book.sblgnt_name, scope.book.code,
            ).items()
            if greek_record_belongs_to_scope(ref, scope)
        })
    official_queue = list(ordered_official_entries(official_entries, scope))
    # Kept in the report and paired with nothing; see records_only_na_edition_change.
    na_only_entries: list[dict[str, Any]] = []
    consumed = 0

    def skip_unstated_entries(ref: str | None = None, lemma: str = "") -> None:
        """Set aside the entries the TEI states no unit for, in text order.

        Decided here rather than by filtering the queue up front, because the
        printed signature alone cannot tell an entry with no unit from one the
        TEI credits to a different edition: the unit standing in this position
        is the other half of the answer.
        """
        nonlocal consumed
        while consumed < len(official_queue):
            span, entry = official_queue[consumed]
            if not records_only_na_edition_change(entry):
                return
            if ref is not None and official_entry_states_unit(
                entry, span, ref, lemma
            ):
                return
            na_only_entries.append({
                "refs": sorted(span), "official_sblgnt_apparatus_entry": entry,
            })
            consumed += 1

    units = []
    for unit in anchored:
        decision = decisions[unit["id"]]
        candidates = {candidate["id"]: candidate for candidate in unit["candidates"]}
        operational_id = decision["selected_reading"] or "r00-base"
        base = candidates["r00-base"]
        operational = candidates[operational_id]
        if decision["status"] == "underdetermined":
            # An underdetermined unit normally keeps the base among the
            # readings the English still permits, and the fallback is then a
            # reading the ASV may well have had. Where the evidence excluded
            # the base outright, the fallback is a reading the ASV demonstrably
            # did not have, and the projection is known to differ from the
            # translators' Greek at that word. Both are underdetermined; only
            # one leaves English with no source token to answer to, so they are
            # counted apart rather than reported as one number.
            if "r00-base" in decision["compatible_readings"]:
                result = "underdetermined_base_fallback"
            else:
                result = "underdetermined_excluded_base_fallback"
        elif operational_id == "r00-base":
            result = "asv_evidenced_base"
        else:
            result = "asv_evidenced_non_base"
        official_ref = unit.get("source_ref", unit["ref"])
        skip_unstated_entries(official_ref, unit["lemma"])
        if consumed >= len(official_queue):
            raise ValueError(
                f"missing official SBLGNT apparatus entry for {unit['id']}"
            )
        span, official_entry = official_queue[consumed]
        consumed += 1
        if official_ref not in span:
            raise ValueError(
                f"official SBLGNT apparatus entry {consumed} covers {sorted(span)}, "
                f"but the TEI unit in that position is {unit['id']} at {official_ref}"
            )
        units.append({
            "unit_id": unit["id"],
            "ref": unit["ref"],
            "apparatus_location": unit["location"],
            "apparatus_source": "sblgnt_tei",
            "official_sblgnt_apparatus_entry": official_entry,
            "result": result,
            "decision_status": decision["status"],
            "sblgnt_reading": reading_text(base),
            "operational_reading": reading_text(operational),
            "operational_candidate": operational_id,
            "operational_tei_edition_labels": operational["witnesses"],
            "differs_from_sblgnt": operational_id != "r00-base",
            "apparatus_candidates": [
                {
                    "id": candidate["id"],
                    "text": reading_text(candidate),
                    "base": candidate["base"],
                    "tei_edition_labels": candidate["witnesses"],
                }
                for candidate in unit["candidates"]
            ],
            "compatible_readings": [
                {
                    "id": candidate_id,
                    "text": reading_text(candidates[candidate_id]),
                    "tei_edition_labels": candidates[candidate_id]["witnesses"],
                }
                for candidate_id in decision["compatible_readings"]
            ],
            "evidence": decision["evidence"],
        })

    skip_unstated_entries()
    available = len(official_queue)
    if consumed != available:
        raise ValueError(
            f"official SBLGNT apparatus entry count differs: used {consumed}, "
            f"found {available}"
        )

    differences = [unit for unit in units if unit["differs_from_sblgnt"]]
    summary = {
        "chapter": scope.ref,
        "base": "SBLGNT",
        "claim": (
            "SBLGNT-based operational Greek projection constrained by ASV wording "
            "and explicit ASV textual notes; not a claim to recover the translators' "
            "exact historical exemplar"
        ),
        "provenance": {
            "base_tokens": {
                "source_lock_key": "macula_greek",
                "role": "working SBLGNT tokenization and lexical enrichment",
                "commit": source_lock["macula_greek"]["commit"],
                "path": "data/vendor/macula-greek/SBLGNT/tsv/macula-greek-SBLGNT.tsv",
            },
            "base_text_validation": {
                "source_lock_key": "sblgnt",
                "role": "official SBLGNT surface-text validation",
                "commit": source_lock["sblgnt"]["commit"],
                "path": f"data/vendor/sblgnt/data/sblgnt/text/{scope.book.sblgnt_name}.txt",
            },
            "apparatus_candidates": {
                "source_lock_key": "sblgnt_tei",
                "role": "machine-readable transcription of SBLGNT apparatus candidates",
                "commit": source_lock["sblgnt_tei"]["commit"],
                "path": "data/vendor/sblgnt-tei/xml/sblgnt_tei.xml",
            },
            "apparatus_location_validation": {
                "source_lock_key": "sblgnt",
                "role": "official SBLGNT apparatus location validation",
                "commit": source_lock["sblgnt"]["commit"],
                "path": (
                    "data/vendor/sblgnt/data/sblgntapp/text/"
                    f"{scope.book.sblgnt_name}.txt"
                ),
            },
            "asv_decisions": {
                "path": f"config/{scope.variant_decisions_name}",
                "role": "versioned ASV-based selection or underdetermination",
            },
        },
        "apparatus_units": len(units),
        "official_entries_without_tei_unit": na_only_entries,
        "asv_evidenced_base": sum(
            unit["result"] == "asv_evidenced_base" for unit in units
        ),
        "asv_evidenced_non_base": len(differences),
        "underdetermined_base_fallback": sum(
            unit["result"] == "underdetermined_base_fallback" for unit in units
        ),
        "underdetermined_excluded_base_fallback": sum(
            unit["result"] == "underdetermined_excluded_base_fallback"
            for unit in units
        ),
        "excluded_fallback_refs": [
            unit["ref"] for unit in units
            if unit["result"] == "underdetermined_excluded_base_fallback"
        ],
        "difference_refs": [unit["ref"] for unit in differences],
        "differences": differences,
        "units": units,
    }

    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / f"{scope.slug}-textual-projection.json"
    markdown_path = reports / f"{scope.slug}-textual-projection.md"
    inline_path = reports / f"{scope.slug}-sblgnt-inline-diff.html"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Named inline rather than left to the table below, so that a reader of
    # the summary alone knows which verses to look at.
    excluded_fallback_note = (
        " (" + ", ".join(summary["excluded_fallback_refs"]) + ")"
        if summary["excluded_fallback_refs"] else ""
    )
    difference_rows = "\n".join(
        "| {ref} | {official} | {candidate} | {base} | {projection} | {evidence} |".format(
            ref=unit["ref"],
            official=markdown_cell(unit["official_sblgnt_apparatus_entry"]),
            candidate=markdown_cell(
                unit["operational_candidate"]
                + (
                    " [" + ", ".join(unit["operational_tei_edition_labels"]) + "]"
                    if unit["operational_tei_edition_labels"] else ""
                )
            ),
            base=markdown_cell(unit["sblgnt_reading"]),
            projection=markdown_cell(unit["operational_reading"]),
            evidence=markdown_cell(unit["evidence"]),
        )
        for unit in differences
    )
    na_only_section = "\n".join(
        "| {refs} | {entry} |".format(
            refs=", ".join(record["refs"]),
            entry=markdown_cell(record["official_sblgnt_apparatus_entry"]),
        )
        for record in na_only_entries
    ) if na_only_entries else "| — | none in this chapter |"
    labels = {
        "asv_evidenced_base": "ASV supports SBLGNT",
        "asv_evidenced_non_base": "ASV supports non-base",
        "underdetermined_base_fallback": "Underdetermined; SBLGNT fallback",
        "underdetermined_excluded_base_fallback":
            "Underdetermined; SBLGNT fallback the ASV excludes",
    }
    decision_rows = "\n".join(
        "| {ref} | {unit_id} | {official} | {result} | {base} | {candidate} | {operational} | {compatible} | {evidence} |".format(
            ref=unit["ref"],
            unit_id=unit["unit_id"],
            official=markdown_cell(unit["official_sblgnt_apparatus_entry"]),
            result=labels[unit["result"]],
            base=markdown_cell(unit["sblgnt_reading"]),
            candidate=markdown_cell(
                unit["operational_candidate"]
                + (
                    " [" + ", ".join(unit["operational_tei_edition_labels"]) + "]"
                    if unit["operational_tei_edition_labels"] else ""
                )
            ),
            operational=markdown_cell(unit["operational_reading"]),
            compatible=markdown_cell(
                "; ".join(
                    f"{reading['id']}: {reading['text']}"
                    + (
                        " [" + ", ".join(reading["tei_edition_labels"]) + "]"
                        if reading["tei_edition_labels"] else ""
                    )
                    for reading in unit["compatible_readings"]
                )
            ),
            evidence=markdown_cell(unit["evidence"]),
        )
        for unit in units
    )
    changes = []
    for difference in differences:
        anchored_unit = anchored_by_id[difference["unit_id"]]
        candidate = next(
            candidate for candidate in anchored_unit["candidates"]
            if candidate["id"] == difference["operational_candidate"]
        )
        changes.append({
            "start": anchored_unit["base_span"]["start"],
            "end": anchored_unit["base_span"]["end"],
            "unit_id": difference["unit_id"],
            "candidate_id": difference["operational_candidate"],
            "projected_tokens": [
                token["surface"] for token in candidate["expanded_tokens"]
            ],
            "official_entry": difference["official_sblgnt_apparatus_entry"],
            "evidence": difference["evidence"],
        })
    inline_body = diff_sections(
        source_tokens, changes, 'lang="grc"',
        token_pieces=official_token_pieces(official_verses),
    )
    inline_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{scope.ref} ASV-evidenced projection against SBLGNT</title>
  <style>
    body {{ max-width: 64rem; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, sans-serif; color: #202124; }}
    h1 {{ margin-bottom: .35rem; }}
    .legend {{ color: #555; }}
    section {{ border-top: 1px solid #ddd; margin-top: 1.5rem; padding-top: .5rem; }}
    .verse {{ font-size: 1.35rem; line-height: 1.9; }}
    del {{ background: #ffe0e0; color: #8b1a1a; padding: .08em .18em; }}
    ins {{ background: #ddf5df; color: #176b2c; padding: .08em .18em; text-decoration: none; border-bottom: 2px solid #3b9b52; }}
    .evidence {{ color: #444; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>{scope.book.name} {scope.chapter}: ASV-evidenced projection against SBLGNT</h1>
  <p class="legend">Only verses with positively selected non-SBLGNT readings are shown. <del>Red struck text</del> is the SBLGNT reading; <ins>green inserted text</ins> is the working projection. Hover over a change for its apparatus unit and ASV evidence. Underdetermined SBLGNT fallbacks are intentionally omitted.</p>
  {inline_body}
</body>
</html>
""",
        encoding="utf-8",
    )
    markdown_path.write_text(
        terminal_newline(f"""# {scope.book.name} {scope.chapter} textual projection

## What this report claims

This is a constrained, ASV-evidenced working projection, not a reconstruction
of the exact physical Greek exemplar used by the ASV translators. It begins
with SBLGNT and considers only the closed candidates in the supplied SBLGNT
apparatus. ASV wording or an explicit ASV textual note selects a reading only
when it distinguishes the candidates.

An underdetermined unit retains SBLGNT solely as an operational fallback.
That fallback is not evidence that the ASV translators read SBLGNT there.
Likewise, agreement outside the modeled apparatus is not proof that the
translators' exemplar agreed with SBLGNT.

A unit is counted apart when the ASV excludes the SBLGNT reading itself and
still leaves more than one candidate standing. The unit stays underdetermined,
because no single reading can be selected, but the fallback it retains is one
the English positively rules out. There the projection is known to differ from
the translators' Greek, and an alignment may find ASV wording with no source
token to answer to.

## Exact source chain

- **Working tokens and enrichment:** MACULA Greek's SBLGNT TSV at commit
  `{source_lock['macula_greek']['commit']}`. MACULA supplies token coordinates,
  lemmas, morphology, Strong's IDs, and other enrichment; it does **not** supply
  the variant apparatus used here.
- **Base-text validation:** official Faithlife SBLGNT at commit
  `{source_lock['sblgnt']['commit']}`, file
  `data/sblgnt/text/{scope.book.sblgnt_name}.txt` within that checkout.
- **Machine-readable apparatus candidates:** the pinned SBLGNT-TEI
  transcription at commit `{source_lock['sblgnt_tei']['commit']}`, file
  `xml/sblgnt_tei.xml`. Each row below preserves its `SBLGNT.…v###` unit ID and
  `r##` candidate ID.
- **Apparatus-location validation:** official Faithlife SBLGNT apparatus at the
  same SBLGNT commit, file
  `data/sblgntapp/text/{scope.book.sblgnt_name}.txt`. The build verifies that the
  TEI unit locations match the official apparatus locations. Candidate strings
  are read from the TEI transcription rather than reparsed from the plain-text
  official apparatus. The verbatim official plain-text entry is recorded beside
  each decision so that the TEI transcription can be checked directly.
- **ASV decisions:** `config/{scope.variant_decisions_name}`. The TEI labels
  such as `WH`, `Treg`, `NIV`, `NA`, and `RP` identify compared editions in that
  transcription; they are provenance metadata, not manuscript witnesses and
  do not choose the ASV reading.

## Summary

- Apparatus units reviewed: {summary['apparatus_units']}
- ASV evidence supports the SBLGNT reading: {summary['asv_evidenced_base']}
- ASV evidence supports a non-SBLGNT reading: {summary['asv_evidenced_non_base']}
- Underdetermined units using the SBLGNT fallback: {summary['underdetermined_base_fallback']}
- Underdetermined units whose SBLGNT fallback the ASV excludes: {summary['underdetermined_excluded_base_fallback']}{excluded_fallback_note}

## Where the working projection differs from SBLGNT

[Open the inline verse diff]({scope.slug}-sblgnt-inline-diff.html). It contains
only verses with positively selected non-SBLGNT readings; underdetermined
fallbacks are omitted.

| Reference | Official SBLGNT apparatus entry | TEI candidate | SBLGNT | Working projection | ASV evidence |
| --- | --- | --- | --- | --- | --- |
{difference_rows}

## Official apparatus entries with no TEI unit

These printed entries record only where NA28 departed from NA27. Every edition
the TEI transcription collates — WH, Treg, NIV, RP — stands with the lemma, so
the transcription states no unit and there is nothing for the ASV to decide.
They are listed here so that every entry in the official apparatus is
accounted for.

| Reference | Official SBLGNT apparatus entry |
| --- | --- |
{na_only_section}

## Complete apparatus decision ledger

| Reference | Apparatus unit | Official SBLGNT apparatus entry | Result | SBLGNT | TEI candidate | Operational reading | ASV-compatible readings | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{decision_rows}
"""),
        encoding="utf-8",
    )
    return summary


def generate_hebrew_textual_report(
    root: Path = ROOT, chapter: str = "GEN.1"
) -> dict[str, Any]:
    """Report the chapter's ketiv/qere decisions against the Leningrad Codex.

    The Hebrew counterpart of the apparatus report. The closed candidate set is
    not an editor's collation of manuscripts but the Masoretes' own record of
    what is written and what is to be read, so the report names both readings
    for every unit and says which one the ASV evidences. A chapter with no
    ketiv/qere still gets a report, because "none in this chapter" is a claim
    worth writing down rather than an absence of output.
    """
    scope = chapter_scope(chapter)
    source_lock = json.loads(
        (root / "config/sources.lock.json").read_text(encoding="utf-8")
    )["sources"]
    normalized = root / f"data/normalized/{scope.slug}"
    source_tokens = load_jsonl(normalized / "source-tokens.jsonl")
    variants = load_jsonl(normalized / "source-variants.jsonl")
    decisions_document = json.loads(
        (root / "config" / scope.variant_decisions_name).read_text(encoding="utf-8")
    )
    anchored = anchor_chapter_variants(scope, source_tokens, variants, root)
    anchored_by_id = {unit["id"]: unit for unit in anchored}
    decisions = validate_variant_decisions(anchored, decisions_document)

    units = []
    for unit in anchored:
        decision = decisions[unit["id"]]
        candidates = {candidate["id"]: candidate for candidate in unit["candidates"]}
        operational_id = decision["selected_reading"] or "r00-base"
        if decision["status"] == "underdetermined":
            # The Greek half of this module counts the same distinction for
            # the same reason: an underdetermined unit whose fallback the
            # English positively excludes leaves the projection knowingly
            # unlike the translators' Hebrew at that word.
            if "r00-base" in decision["compatible_readings"]:
                result = "underdetermined_qere_fallback"
            else:
                result = "underdetermined_excluded_qere_fallback"
        elif operational_id == "r00-base":
            result = "asv_evidenced_qere"
        else:
            result = "asv_evidenced_ketiv"
        units.append({
            "unit_id": unit["id"],
            "ref": unit["ref"],
            "apparatus_location": unit["location"],
            "apparatus_source": "oshb_ketiv_qere",
            # A qere that reads nothing occupies no word position, so the
            # written word it passes over is what the report names instead.
            "oshb_words": unit["oshb_words"] or unit.get("oshb_ketiv_words") or [],
            "oshb_ketiv_id": candidates["r01"]["oshb_id"],
            "oshb_qere_id": candidates["r00-base"]["oshb_id"],
            "result": result,
            "decision_status": decision["status"],
            "qere_reading": reading_text(candidates["r00-base"]),
            "ketiv_reading": reading_text(candidates["r01"]),
            "operational_reading": reading_text(candidates[operational_id]),
            "operational_candidate": operational_id,
            "differs_from_qere": operational_id != "r00-base",
            "apparatus_candidates": [
                {
                    "id": candidate["id"],
                    "text": reading_text(candidate),
                    "base": candidate["base"],
                    "masoretic_label": candidate["witnesses"][0],
                }
                for candidate in unit["candidates"]
            ],
            "compatible_readings": [
                {
                    "id": candidate_id,
                    "text": reading_text(candidates[candidate_id]),
                    "masoretic_label": candidates[candidate_id]["witnesses"][0],
                }
                for candidate_id in decision["compatible_readings"]
            ],
            "evidence": decision["evidence"],
        })

    differences = [unit for unit in units if unit["differs_from_qere"]]
    # A verse the pinned edition does not contain, and the parallel of the same
    # edition the chapter records as supplying it. This is the one place the
    # projection holds Hebrew that is not written in the chapter it aligns, so
    # the report that describes the projection has to say so.
    supplied_verses = [
        {
            "target": supply.target_ref,
            "source": supply.source_ref,
            "evidence": supply.evidence,
        }
        for _, supply in sorted(load_supplied_verses(
            root / "config" / scope.unsourced_verses_name, scope.ref
        ).items()) if supply.source_ref is not None
    ]
    summary = {
        "chapter": scope.ref,
        "base": "WLC qere",
        "claim": (
            "Westminster Leningrad Codex text as tokenized by MACULA Hebrew, "
            "which projects the qere, with each ketiv/qere pair decided from ASV "
            "wording and explicit ASV textual notes; not a claim to recover the "
            "translators' exact historical exemplar"
        ),
        "provenance": {
            "base_tokens": {
                "source_lock_key": "macula_hebrew",
                "role": "working WLC morpheme tokenization and lexical enrichment",
                "commit": source_lock["macula_hebrew"]["commit"],
                "path": (
                    "data/vendor/macula-hebrew/WLC/lowfat/"
                    f"{scope.macula_hebrew_lowfat_file}"
                ),
            },
            "base_text_validation": {
                "source_lock_key": "oshb",
                "role": "OSHB word-level WLC text and stable identifiers",
                "commit": source_lock["oshb"]["commit"],
                "path": f"data/vendor/oshb/wlc/{scope.book.oshb_file}",
            },
            "apparatus_candidates": {
                "source_lock_key": "oshb",
                "role": "ketiv and qere readings as encoded by OSHB",
                "commit": source_lock["oshb"]["commit"],
                "path": f"data/vendor/oshb/wlc/{scope.book.oshb_file}",
            },
            "asv_decisions": {
                "path": f"config/{scope.variant_decisions_name}",
                "role": "versioned ASV-based selection or underdetermination",
            },
        },
        "apparatus_units": len(units),
        "asv_evidenced_base": sum(
            unit["result"] == "asv_evidenced_qere" for unit in units
        ),
        "asv_evidenced_non_base": len(differences),
        "underdetermined_base_fallback": sum(
            unit["result"] == "underdetermined_qere_fallback" for unit in units
        ),
        "underdetermined_excluded_base_fallback": sum(
            unit["result"] == "underdetermined_excluded_qere_fallback"
            for unit in units
        ),
        "excluded_fallback_refs": [
            unit["ref"] for unit in units
            if unit["result"] == "underdetermined_excluded_qere_fallback"
        ],
        "difference_refs": [unit["ref"] for unit in differences],
        "differences": differences,
        # Carried only where there is one, like the optional token fields the
        # packet omits when the source does not supply them: every chapter but
        # Nehemiah 7 has none, and adding an empty list to each of their
        # committed reports would be churn that says nothing.
        **({"supplied_verses": supplied_verses} if supplied_verses else {}),
        "units": units,
    }

    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / f"{scope.slug}-textual-projection.json"
    markdown_path = reports / f"{scope.slug}-textual-projection.md"
    inline_path = reports / f"{scope.slug}-wlc-inline-diff.html"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    labels = {
        "asv_evidenced_qere": "ASV supports the qere",
        "asv_evidenced_ketiv": "ASV supports the ketiv",
        "underdetermined_qere_fallback": "Underdetermined; qere fallback",
        "underdetermined_excluded_qere_fallback":
            "Underdetermined; qere fallback the ASV excludes",
    }
    excluded_fallback_note = (
        " (" + ", ".join(summary["excluded_fallback_refs"]) + ")"
        if summary["excluded_fallback_refs"] else ""
    )
    # Named in the summary rather than left to the ledger below, because it is
    # a different kind of claim: not which of two recorded readings the ASV
    # evidences, but a verse whose Hebrew is not in this chapter at all.
    supplied_section = "".join(
        f"\n## Verses supplied from a parallel\n\n"
        f"The pinned edition does not contain {supply['target']}, which the ASV's "
        f"underlying text has. Its source is taken from {supply['source']}, the "
        f"parallel verse of the same edition, and every token of it keeps that "
        f"reference as its `source_ref`. This is the parallel in the pinned "
        f"edition, not a manuscript of the book being aligned.\n\n"
        f"| ASV verse | Supplied from | Evidence |\n| --- | --- | --- |\n"
        f"| {supply['target']} | {supply['source']} | "
        f"{markdown_cell(supply['evidence'])} |\n"
        for supply in supplied_verses
    )
    decision_rows = "\n".join(
        "| {ref} | {unit_id} | {word} | {result} | {ketiv} | {qere} | {operational} | {compatible} | {evidence} |".format(
            ref=unit["ref"],
            unit_id=unit["unit_id"],
            word=", ".join(str(w) for w in unit["oshb_words"]),
            result=labels[unit["result"]],
            ketiv=markdown_cell(unit["ketiv_reading"]),
            qere=markdown_cell(unit["qere_reading"]),
            operational=markdown_cell(unit["operational_reading"]),
            compatible=markdown_cell(
                "; ".join(
                    f"{reading['id']} ({reading['masoretic_label']}): {reading['text']}"
                    for reading in unit["compatible_readings"]
                )
            ),
            evidence=markdown_cell(unit["evidence"]),
        )
        for unit in units
    )
    difference_rows = "\n".join(
        "| {ref} | {unit_id} | {qere} | {ketiv} | {evidence} |".format(
            ref=unit["ref"],
            unit_id=unit["unit_id"],
            qere=markdown_cell(unit["qere_reading"]),
            ketiv=markdown_cell(unit["operational_reading"]),
            evidence=markdown_cell(unit["evidence"]),
        )
        for unit in differences
    )

    changes = []
    for difference in differences:
        anchored_unit = anchored_by_id[difference["unit_id"]]
        candidate = next(
            candidate for candidate in anchored_unit["candidates"]
            if candidate["id"] == difference["operational_candidate"]
        )
        changes.append({
            "start": anchored_unit["base_span"]["start"],
            "end": anchored_unit["base_span"]["end"],
            "unit_id": difference["unit_id"],
            "candidate_id": difference["operational_candidate"],
            "projected_tokens": [
                display_surface(token) for token in candidate["expanded_tokens"]
            ],
            # Either side of a pair can be absent: a ketiv velo qere is
            # written and not read, and its converse is read and not written.
            # The record says so rather than naming a missing identifier.
            "official_entry": (
                f"OSHB ketiv {difference['oshb_ketiv_id'] or '∅ (not written)'} / "
                f"qere {difference['oshb_qere_id'] or '∅ (not read)'} at word(s) "
                f"{', '.join(str(w) for w in difference['oshb_words'])}"
            ),
            "evidence": difference["evidence"],
        })
    inline_body = diff_sections(
        source_tokens, changes, 'lang="hbo" dir="rtl"',
        "Masoretic record", "Masoretic record",
    ) or (
        '<p class="legend">No ketiv reading was selected in this chapter.</p>'
    )
    inline_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{scope.ref} ASV-evidenced projection against the WLC qere</title>
  <style>
    body {{ max-width: 64rem; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, sans-serif; color: #202124; }}
    h1 {{ margin-bottom: .35rem; }}
    .legend {{ color: #555; }}
    section {{ border-top: 1px solid #ddd; margin-top: 1.5rem; padding-top: .5rem; }}
    .verse {{ font-size: 1.6rem; line-height: 2; font-family: "Noto Sans Hebrew", "SBL Hebrew", "Ezra SIL", serif; }}
    del {{ background: #ffe0e0; color: #8b1a1a; padding: .08em .18em; }}
    ins {{ background: #ddf5df; color: #176b2c; padding: .08em .18em; text-decoration: none; border-bottom: 2px solid #3b9b52; }}
    .evidence {{ color: #444; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>{scope.book.name} {scope.chapter}: ASV-evidenced projection against the WLC qere</h1>
  <p class="legend">Only verses where the ASV evidences the ketiv are shown. <del>Red struck text</del> is the qere that MACULA Hebrew projects; <ins>green inserted text</ins> is the working projection. Hover over a change for its Masoretic record and ASV evidence. Underdetermined qere fallbacks are intentionally omitted.</p>
  {inline_body}
</body>
</html>
""",
        encoding="utf-8",
    )
    markdown_path.write_text(
        terminal_newline(f"""# {scope.book.name} {scope.chapter} textual projection

## What this report claims

This is a constrained, ASV-evidenced working projection, not a reconstruction
of the exact Hebrew manuscript the ASV translators read. It begins with the
Westminster Leningrad Codex as tokenized by MACULA Hebrew, which projects the
qere, and considers only the closed candidates the Masoretes themselves
recorded: for each marked word, the ketiv that is written and the qere that is
to be read. ASV wording or an explicit ASV textual note selects between them
only when it distinguishes them.

An underdetermined unit retains the qere solely as an operational fallback.
That fallback is not evidence that the ASV translators read the qere there.
A unit is counted apart where the ASV excludes the qere itself and still leaves
more than one candidate standing: the unit stays underdetermined, but the
fallback it retains is one the English positively rules out, and an alignment
may find ASV wording with no source token to answer to.
There is no critical apparatus in this projection: conjectural emendations and
the ancient versions are outside the closed candidate set, so a disagreement
with the ASV that is not a ketiv/qere pair is not modeled here at all.

## Exact source chain

- **Working tokens and enrichment:** MACULA Hebrew at commit
  `{source_lock['macula_hebrew']['commit']}`, file
  `WLC/lowfat/{scope.macula_hebrew_lowfat_file}`. MACULA supplies the morpheme
  segmentation, lemmas, morphology, Strong's IDs, and glosses. Hebrew is
  segmented below the word, so a prefixed conjunction, preposition, or article
  and a suffixed pronoun are separate tokens.
- **Base-text validation and stable identifiers:** the Open Scriptures Hebrew
  Bible at commit `{source_lock['oshb']['commit']}`, file
  `wlc/{scope.book.oshb_file}`. The build checks that every MACULA word
  reassembles letter for letter into the OSHB word at the same position, and
  that MACULA covers every OSHB word except the ketiv forms it replaces with
  their qere.
- **Closed textual candidates:** the ketiv/qere pairs encoded in that same OSHB
  file. Each unit below keeps its `WLC.…v###` unit ID, the OSHB word position it
  anchors on, and both OSHB word identifiers.
- **ASV decisions:** `config/{scope.variant_decisions_name}`.

## Summary

- Ketiv/qere units reviewed: {summary['apparatus_units']}
- ASV evidence supports the qere: {summary['asv_evidenced_base']}
- ASV evidence supports the ketiv: {summary['asv_evidenced_non_base']}
- Underdetermined units using the qere fallback: {summary['underdetermined_base_fallback']}
- Underdetermined units whose qere fallback the ASV excludes: {summary['underdetermined_excluded_base_fallback']}{excluded_fallback_note}
{supplied_section}
## Where the working projection differs from the qere

[Open the inline verse diff]({scope.slug}-wlc-inline-diff.html). It contains
only verses where the ASV evidences the ketiv; underdetermined fallbacks are
omitted.

| Reference | Unit | Qere | Working projection | ASV evidence |
| --- | --- | --- | --- | --- |
{difference_rows}

## Complete ketiv/qere decision ledger

| Reference | Unit | OSHB word | Result | Ketiv | Qere | Operational reading | ASV-compatible readings | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{decision_rows}
"""),
        encoding="utf-8",
    )
    return summary

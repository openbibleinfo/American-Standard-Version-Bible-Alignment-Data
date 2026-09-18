from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline import (
    ROOT,
    build_chapter,
    jsonl_write,
    parse_asv_usx,
    hebrew_chapter_versification,
    hebrew_source_chapters,
    parse_oshb_ketiv_qere,
    parse_oshb_words,
    restate_in_target_versification,
    supplied_verse_files,
    supplied_verse_sources,
    verse_number,
    greek_record_belongs_to_scope,
    greek_source_chapters,
    parse_tei_variants,
    reading_tokens,
    strong_id,
    unit_anchor_word,
)
from .scope import ChapterScope, chapter_scope
from .diagnostics import large_source_unit_candidates
from .greek_boundaries import (
    boundary_mapping_sha256, chapter_moves, restate_selected_spans,
    validate_projection_coverage,
)
from .versification import divided_verses, load_supplied_verses
from .transliteration import greek_transliteration, hebrew_transliteration


# Each testament has its own verse contract. The alignment ledger is shared,
# but the linguistic instructions are not interchangeable, and a single shared
# version would stale every finished chapter in the other testament whenever
# one of them learned something. Keeping them separate lets Hebrew evolve
# without re-reviewing Greek.
ALIGNMENT_CONTRACTS = {
    "NT": {"instructions": "config/alignment-instructions-greek.md"},
    "OT": {"instructions": "config/alignment-instructions-hebrew.md"},
}

#: How each contract declares its own version, in its opening lines. This is
#: the only place a version is written. It used to be written twice — here and
#: in a registry literal above — with a test comparing them, because they had
#: drifted once and stamped two chapters with a contract older than the text
#: they were aligned against. A second copy is not worth keeping now that a
#: reconciliation can bump the version itself: an automatic edit that had to
#: rewrite a Python literal to stay honest would be one refactor away from
#: silently lying again. The document a pass reads is the authority.
CONTRACT_VERSION = re.compile(r"^This contract is `([^`]+)`", re.MULTILINE)


def contract_version(scope: ChapterScope, root: Path = ROOT) -> str:
    """The version the testament's contract declares of itself.

    Read rather than remembered. This stamps a run record and a review
    receipt with the contract in force when that artifact is written.
    """
    path = instructions_path(root, scope)
    declared = CONTRACT_VERSION.search(path.read_text(encoding="utf-8"))
    if declared is None:
        raise ValueError(f"{path} declares no contract version")
    return declared.group(1)


def instructions_path(root: Path, scope: ChapterScope) -> Path:
    return root / ALIGNMENT_CONTRACTS[scope.testament]["instructions"]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verse_responses_sha256(
    directory: Path, *, read_bytes: Callable[[Path], bytes] | None = None,
) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(read_bytes(path) if read_bytes is not None else path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


SOURCE_LOCK_KEYS = {
    "NT": ("asv", "macula_greek", "sblgnt", "sblgnt_tei"),
    "OT": ("asv", "macula_hebrew", "oshb"),
}


def source_lock_entries_sha256(
    root: Path, scope: ChapterScope, *, read_bytes: Callable[[Path], bytes] | None = None,
) -> str:
    path = root / "config/sources.lock.json"
    document = json.loads(read_bytes(path) if read_bytes is not None else path.read_text(encoding="utf-8"))
    sources = document["sources"]
    selected = {key: sources[key] for key in SOURCE_LOCK_KEYS[scope.testament]}
    payload = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verse_divisions_sha256(
    root: Path, scope: ChapterScope, *, read_bytes: Callable[[Path], bytes] | None = None,
) -> str:
    """Hash a chapter's verse divisions, or record that it has none.

    Five chapters in the whole Hebrew Bible divide a verse, so almost every
    run record states the absence rather than a digest. Stating it is the
    point: a chapter that later acquires a division must go stale, and one
    that never had a file must not be stale forever for lacking it.
    """
    path = root / "config" / scope.verse_divisions_name
    if read_bytes is None:
        return file_sha256(path) if path.exists() else "absent"
    try:
        return hashlib.sha256(read_bytes(path)).hexdigest()
    except FileNotFoundError:
        return "absent"


def run_record_is_fresh(
    root: Path, scope: ChapterScope, run_record: dict[str, Any],
    *, read_bytes: Callable[[Path], bytes] | None = None,
) -> bool:
    """Whether the compiled ledger still matches what it was compiled from.

    ``read_bytes`` optionally supplies a caller-owned, read-only input snapshot;
    the same digest and freshness rules apply to cached and ordinary reads.

    Everything checked here is something compilation actually reads, so a
    difference means the ledger on disk could differ from what the inputs now
    imply. Three documents are deliberately not among them, for one reason: no
    line of compilation opens any of them, so editing one cannot change a byte
    of any ledger. The testament contract is a prompt document, recorded as
    provenance under `aligned_under_contract_version` and never compared to the
    current one. The two schemas are the same case — nothing here loads them,
    and what actually gates a file is `validate_response` and `mark_reviewed`,
    which are code. Their hashes stay in the record as provenance, saying what
    shape a chapter's files were written to; comparing them would have marked
    all 456 finished chapters stale the day `confidence` left the schema, and
    a re-compile would have changed nothing in any of them.
    """
    paths = {
        "prepared_input_sha256": root / f"data/output/{scope.slug}/alignment-prep.jsonl",
        "variant_decisions_sha256": root / "config" / scope.variant_decisions_name,
    }
    if read_bytes is None and any(not path.exists() for path in paths.values()):
        return False
    try:
        if any(run_record.get(field) != (hashlib.sha256(read_bytes(path)).hexdigest()
               if read_bytes is not None else file_sha256(path)) for field, path in paths.items()):
            return False
    except FileNotFoundError:
        return False
    if run_record.get("verse_divisions_sha256") != verse_divisions_sha256(root, scope, read_bytes=read_bytes):
        return False
    if not scope.is_hebrew and run_record.get("greek_boundary_mapping_sha256", "absent") != boundary_mapping_sha256(root, scope.ref, read_bytes=read_bytes):
        return False
    return (
        run_record.get("source_lock_entries_sha256")
        == source_lock_entries_sha256(root, scope, read_bytes=read_bytes)
        and run_record.get("verse_responses_sha256")
        == verse_responses_sha256(root / f"data/alignments/{scope.slug}/verses", read_bytes=read_bytes)
        and run_record.get("review_receipts_sha256")
        == verse_responses_sha256(root / f"data/alignments/{scope.slug}/reviews", read_bytes=read_bytes)
    )


def normalized_form(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def apparatus_anchor_form(value: str) -> str:
    """Normalize a word for locating apparatus lemmas in the source stream.

    MACULA stores an elision mark as the preceding token's ``after``
    punctuation, while the SBLGNT apparatus keeps the Greek koronis attached
    to the word (for example ``παρ᾽`` versus surface ``παρ``).  Ignore only
    terminal apostrophe-like elision marks here; retaining accents and
    breathings is necessary to distinguish forms such as ὁ and ὅ.
    """
    return normalized_form(value).rstrip("᾽’ʼ'")


def lexical_form(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFD", value).casefold()
        if not unicodedata.combining(character)
    )


def find_span_across(
    source_tokens: list[dict[str, Any]], lemma_words: list[str],
    window_start: int, window_end: int, start_at: int,
) -> tuple[int, int]:
    """Locate a lemma whose first word stands inside a given window.

    The window bounds where the match may *begin*, not where it may end: a
    printed apparatus entry can quote across a verse boundary, and such an
    entry belongs to the verse it starts in. Requiring the start to fall
    there is what keeps a one-word lemma from finding a home several verses
    away.
    """
    wanted = [apparatus_anchor_form(word) for word in lemma_words]
    surfaces = [apparatus_anchor_form(token["surface"]) for token in source_tokens]
    starts = list(range(start_at, window_end)) + list(range(window_start, start_at))
    for start in starts:
        if surfaces[start : start + len(wanted)] == wanted:
            return start, start + len(wanted)
    raise ValueError(f"could not anchor apparatus lemma: {' '.join(lemma_words)}")


def find_span(
    source_tokens: list[dict[str, Any]], lemma_words: list[str], start_at: int = 0
) -> tuple[int, int]:
    return find_span_across(
        source_tokens, lemma_words, 0, len(source_tokens), start_at
    )


def lexical_index(source_tokens: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token in source_tokens:
        # Prefer the exact accented form.  A diacritic-insensitive fallback is
        # still useful for apparatus spellings, but cannot safely distinguish
        # forms such as the article ὁ from the relative pronoun ὅ.
        candidates[normalized_form(token["surface"])].append(token)
        candidates[f"folded:{lexical_form(token['surface'])}"].append(token)
    result = {}
    for form, tokens in candidates.items():
        resolved: dict[str, Any] = {}
        for field in ("lemma", "strong", "morph"):
            values = {token.get(field) for token in tokens if token.get(field)}
            if len(values) == 1:
                resolved[field] = values.pop()
        glosses = Counter(
            token["gloss"].casefold()
            for token in tokens
            if token.get("gloss") and token["gloss"] != "-"
        )
        if glosses:
            ranked = glosses.most_common()
            if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
                resolved["gloss"] = ranked[0][0]
        if resolved:
            result[form] = resolved
    return result


def alternative_token(
    unit: dict[str, Any], reading_id: str, position: int, surface: str,
    ref: str, lexicon: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    known = lexicon.get(
        normalized_form(surface), lexicon.get(f"folded:{lexical_form(surface)}", {})
    )
    return {
        "id": f"{unit['id']}.{reading_id}.{position:03d}",
        "source_xml_id": None,
        "ref": ref,
        "language": "grc",
        "edition": "SBLGNT-apparatus",
        "dataset": "SBLGNT-TEI",
        "surface": surface,
        "after": " ",
        "normalized": surface,
        "transliteration": greek_transliteration(surface),
        "transliteration_source": "derived",
        "lemma": known.get("lemma"),
        "strong": known.get("strong"),
        "morph": known.get("morph"),
        "gloss": known.get("gloss"),
        "role": None,
        "variant_unit": unit["id"],
        "reading": reading_id,
        "raw": {},
    }


def reading_token_refs(text: str, ref: str) -> list[tuple[str, str]]:
    """Keep verse labels embedded in an apparatus reading as token locations.

    Romans 16's doxology is printed inside the apparatus at 16:24, with
    explicit 25/26/27 labels. A bare reference such as Mark's ``+ 9–20``
    contributes no words and is not an inline verse boundary.
    """
    markers = list(re.finditer(r"(?<!\w)([1-9]\d*)\s+(?=[\u0370-\u03ff\u1f00-\u1fff])", text))
    if not markers:
        return [(word, ref) for word in reading_tokens(text)]
    words = [(word, ref) for word in reading_tokens(text[:markers[0].start()])]
    chapter = ref.rsplit(".", 1)[0]
    previous = None
    for index, marker in enumerate(markers):
        verse = int(marker.group(1))
        if previous is not None and verse != previous + 1:
            raise ValueError(f"nonconsecutive apparatus verse labels in {ref}: {text!r}")
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        words.extend((word, f"{chapter}.{verse}") for word in reading_tokens(text[marker.end():end]))
        previous = verse
    return words


def expand_candidate(
    unit: dict[str, Any], candidate: dict[str, Any], base_tokens: list[dict[str, Any]],
    lexicon: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    reading_id = candidate["id"]
    if reading_id == "r00-base":
        return [dict(token) for token in base_tokens]
    text = candidate["text"].strip()
    if not text:
        return []
    ref = unit["ref"]
    def token_at(index: int, word: str, token_ref: str) -> dict[str, Any]:
        token = alternative_token(unit, reading_id, index, word, token_ref, lexicon)
        if token_ref != ref:
            token["source_ref"] = ref
        return token

    if text.startswith("+"):
        added = [
            token_at(index, word, token_ref)
            for index, (word, token_ref) in enumerate(reading_token_refs(text, ref), 1)
        ]
        return [dict(token) for token in base_tokens] + added
    if "…" in text:
        left_text, right_text = text.split("…", 1)
        left_words = reading_tokens(left_text)
        right_words = reading_tokens(right_text)
        middle_start = len(left_words)
        middle_end = len(base_tokens) - len(right_words)
        if middle_end < middle_start:
            raise ValueError(f"invalid ellipsis reading in {unit['id']} {reading_id}")
        result = [
            alternative_token(unit, reading_id, index, word, ref, lexicon)
            for index, word in enumerate(left_words, 1)
        ]
        result.extend(dict(token) for token in base_tokens[middle_start:middle_end])
        offset = len(result)
        result.extend(
            alternative_token(unit, reading_id, offset + index, word, ref, lexicon)
            for index, word in enumerate(right_words, 1)
        )
        return result
    return [
        token_at(index, word, token_ref)
        for index, (word, token_ref) in enumerate(reading_token_refs(text, ref), 1)
    ]


def ref_bounds(source_tokens: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Where each verse begins and ends in the chapter's token stream."""
    bounds: dict[str, tuple[int, int]] = {}
    for index, token in enumerate(source_tokens):
        start = bounds[token["ref"]][0] if token["ref"] in bounds else index
        bounds[token["ref"]] = (start, index + 1)
    return bounds


def spanned_refs(base_tokens: list[dict[str, Any]]) -> list[str]:
    """The verses a unit's base span touches, in reading order."""
    refs = []
    for token in base_tokens:
        if token["ref"] not in refs:
            refs.append(token["ref"])
    return refs


def inline_span(
    source_tokens: list[dict[str, Any]], unit: dict[str, Any],
    lemma_words: list[str], ref_start: int, ref_end: int,
) -> tuple[int, int] | None:
    """The span the TEI itself addresses, when it can be trusted here.

    A printed apparatus quotes words, but the TEI writes each entry inline
    among them, so the word it addresses is recorded rather than guessed. That
    is the only thing that can distinguish the five occurrences of τὰ in
    1 Cor 1:28. The recorded offset counts from the start of the unit's own
    source verse, so it is unusable once a verse move has restated the unit
    into a different verse than the one the TEI counted in; such a unit carries
    ``source_ref`` and falls back to the search. The surfaces are checked
    either way: an offset that does not land on the lemma is not an anchor.
    """
    index = unit.get("lemma_word_index")
    if index is None or unit.get("source_ref"):
        return None
    start = ref_start + index
    end = start + len(lemma_words)
    # The lemma may run past the verse's end -- Jude 22-23 is quoted as one
    # entry -- but it has to begin inside the verse the TEI counted it in.
    if not ref_start <= start <= ref_end or end > len(source_tokens):
        return None
    found = [apparatus_anchor_form(token["surface"]) for token in source_tokens[start:end]]
    if found != [apparatus_anchor_form(word) for word in lemma_words]:
        return None
    return start, end


def anchor_variants(
    source_tokens: list[dict[str, Any]], variants: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anchor each apparatus unit against the chapter's token stream.

    Spans are chapter-relative because an apparatus entry is not obliged to
    respect the verse numbering: Jude 22-23 is quoted as one lemma. Anchoring
    per verse would leave such an entry unplaceable.
    """
    bounds = ref_bounds(source_tokens)
    lexicon = lexical_index(source_tokens)
    cursors: dict[str, int] = {}
    anchored = []
    for original in variants:
        unit = json.loads(json.dumps(original, ensure_ascii=False))
        ref_start, ref_end = bounds[unit["ref"]]
        cursor = cursors.get(unit["ref"], ref_start)
        words = reading_tokens(unit["lemma"])
        span = inline_span(source_tokens, unit, words, ref_start, ref_end)
        if span is not None:
            start, end = span
        else:
            try:
                # A lemma quoted from one verse is anchored within that verse,
                # so that a repeated phrase cannot reach across the boundary
                # for its match while an honest one is available at home.
                start, end = find_span(
                    source_tokens[ref_start:ref_end], words, cursor - ref_start
                )
                start, end = start + ref_start, end + ref_start
            except ValueError:
                start, end = find_span_across(
                    source_tokens, words, ref_start, ref_end, cursor
                )
        cursors[unit["ref"]] = min(end, ref_end)
        base_tokens = source_tokens[start:end]
        unit["base_span"] = {"start": start, "end": end}
        unit["base_refs"] = spanned_refs(base_tokens)
        unit["base_source_ids"] = [token["id"] for token in base_tokens]
        for candidate in unit["candidates"]:
            candidate["expanded_tokens"] = expand_candidate(unit, candidate, base_tokens, lexicon)
        anchored.append(unit)
    return anchored


def oshb_strong(lemma: str | None) -> str | None:
    """Turn an OSHB segment lemma into a Strong's citation.

    OSHB writes a prefix morpheme as a letter (``c`` for the conjunction) and a
    lexical one as a number that may carry a homograph letter after a space,
    as in ``1254 a``. Only the numeric ones are Strong's numbers.
    """
    match = re.fullmatch(r"(\d+)(?:\s+([a-z]))?", (lemma or "").strip())
    if not match:
        return None
    return strong_id(f"{match.group(1)}{match.group(2) or ''}", "H")


def hebrew_alternative_tokens(
    unit: dict[str, Any], candidate: dict[str, Any], base_tokens: list[dict[str, Any]],
    lemma_lookup: dict[str, str] | None = None,
    lexical_entries: dict[str, Any] | None = None,
    lexical_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project an unselected ketiv from OSHB, which is all it has.

    MACULA carries only the qere, so a ketiv has no morpheme analysis there.
    OSHB still segments it and records its lexical code and morphology, so the
    alternative reading is projected at that granularity and labelled as
    coming from OSHB rather than from MACULA. Its code remains in raw metadata;
    dictionary headwords come from the versioned lexical crosswalk, with an
    exact, unambiguous chapter-local citation as the fallback.
    """
    tokens = []
    pieces: list[tuple[str, str | None, str | None]] = []
    for word_index, word_segments in enumerate(candidate["word_segments"]):
        morphs = (candidate["word_morphs"][word_index] or "").removeprefix("H").split("/")
        lemmas = (candidate["word_lemmas"][word_index] or "").split("/")
        for position, segment in enumerate(segment for segment in word_segments if segment):
            pieces.append((
                segment,
                lemmas[position] if position < len(lemmas) else None,
                morphs[position] if position < len(morphs) else None,
            ))
    for index, (segment, lemma, morph) in enumerate(pieces, 1):
        token_id = f"{unit['id']}.{candidate['id']}.{index:03d}"
        language = base_tokens[0]["language"] if base_tokens else "hbo"
        lexical_entry = (lexical_overrides or {}).get(token_id) or (lexical_entries or {}).get(
            f"{language}:{lemma}")
        tokens.append({
            "id": token_id,
            "source_xml_id": candidate["oshb_id"],
            "ref": unit["ref"],
            "oshb_word": unit_anchor_word(unit),
            "language": language,
            "edition": "WLC-ketiv",
            "dataset": "OSHB",
            "surface": segment,
            "pointed": segment,
            "after": (
                base_tokens[-1]["after"]
                if base_tokens and index == len(pieces) else ""
            ),
            "normalized": segment,
            "transliteration": hebrew_transliteration(segment),
            "transliteration_source": "derived",
            "lemma": (lexical_entry["lemma"] if lexical_entry
                      else hebrew_oshb_lemma(lemma, lemma_lookup or {}, language)),
            "strong": (lexical_entry.get("strong", oshb_strong(lemma)) if lexical_entry
                       else oshb_strong(lemma)),
            "morph": morph,
            "gloss": None,
            "role": None,
            "variant_unit": unit["id"],
            "reading": candidate["id"],
            "raw": {"oshb_lemma": lemma, "oshb_strong": oshb_strong(lemma)},
        })
    return tokens


def hebrew_oshb_lemma(
    code: str | None, lookup: dict[str, str], language: str = "hbo",
) -> str | None:
    """Decode OSHB's lexical identifiers without passing them off as Hebrew.

    Prefix letters are OSHB's fixed morpheme codes. Numeric identifiers are
    resolved only by an unambiguous exact Strong's citation in the chapter's
    pinned MACULA input; a missing or ambiguous citation stays unknown. The
    inflected ketiv surface is never a substitute for a dictionary lemma.
    """
    prefixes = {"b": "בְּ", "c": "וְ", "d": "הַ", "k": "כְּ", "l": "לְ",
                "m": "מִן", "s": "שֶׁ"}
    return prefixes.get(code or "") or lookup.get(f"{language}:{oshb_strong(code) or ''}")


def anchor_hebrew_variants(
    source_tokens: list[dict[str, Any]], variants: list[dict[str, Any]],
    lexical_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Anchor each ketiv/qere unit by word position rather than by text.

    The Greek apparatus has to be anchored by matching its lemma against the
    running text, because a printed apparatus quotes words rather than
    addressing them. Hebrew is better served: MACULA and OSHB agree on the word
    position, so the span is exact even where a ketiv and its qere share no
    letters at all.
    """
    lexical_metadata = lexical_metadata or {}
    lemma_candidates: dict[str, set[str]] = defaultdict(set)
    for token in source_tokens:
        lemma = token.get("lemma")
        if token.get("strong") and lemma and re.search(r"[\u05d0-\u05ea]", lemma):
            lemma_candidates[f"{token['language']}:{token['strong']}"].add(lemma)
    lemma_lookup = {strong: next(iter(lemmas)) for strong, lemmas in lemma_candidates.items()
                    if len(lemmas) == 1}
    bounds = ref_bounds(source_tokens)
    anchored = []
    for original in variants:
        unit = json.loads(json.dumps(original, ensure_ascii=False))
        ref_start, ref_end = bounds[unit["ref"]]
        wanted = set(unit["oshb_words"])
        positions = [
            index for index in range(ref_start, ref_end)
            if source_tokens[index]["oshb_word"] in wanted
        ]
        if not wanted:
            # A ketiv velo qere reads nothing, so its base span is the empty
            # span where the unread word stands: everything before it is read,
            # everything after it is read, and selecting the ketiv splices the
            # written word back in at exactly that point.
            written = set(unit.get("oshb_ketiv_words") or [])
            if not written:
                raise ValueError(f"{unit['id']} has neither a qere nor a ketiv")
            present = sorted(
                index for index in range(ref_start, ref_end)
                if source_tokens[index]["oshb_word"] in written
            )
            if present:
                raise ValueError(
                    f"{unit['id']} is written but not read, yet OSHB word(s) "
                    f"{sorted(written)} have projected tokens in {unit['ref']}"
                )
            after = [
                index for index in range(ref_start, ref_end)
                if source_tokens[index]["oshb_word"] > max(written)
            ]
            start = end = after[0] if after else ref_end
        elif not positions:
            raise ValueError(
                f"{unit['id']} anchors on OSHB word(s) {unit['oshb_words']}, "
                f"which have no projected tokens in {unit['ref']}"
            )
        else:
            start, end = positions[0], positions[-1] + 1
            if end - start != len(positions):
                raise ValueError(f"{unit['id']} spans a discontiguous run of tokens")
        base_tokens = source_tokens[start:end]
        unit["base_span"] = {"start": start, "end": end}
        unit["base_refs"] = spanned_refs(base_tokens)
        unit["base_source_ids"] = [token["id"] for token in base_tokens]
        for candidate in unit["candidates"]:
            candidate["expanded_tokens"] = (
                [dict(token) for token in base_tokens]
                if candidate["id"] == "r00-base"
                else hebrew_alternative_tokens(
                    unit, candidate, base_tokens, lemma_lookup,
                    lexical_metadata.get("entries"), lexical_metadata.get("overrides"))
            )
        anchored.append(unit)
    return anchored


def anchor_chapter_variants(
    scope: ChapterScope, source_tokens: list[dict[str, Any]],
    variants: list[dict[str, Any]], root: Path = ROOT,
) -> list[dict[str, Any]]:
    if scope.is_hebrew:
        path = root / "config/hebrew-variant-lemmas.json"
        metadata = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for section in ("entries", "overrides"):
            for key, entry in metadata.get(section, {}).items():
                lemma = entry.get("lemma")
                if not isinstance(lemma, str) or not re.search(r"[\u05d0-\u05ea]", lemma):
                    raise ValueError(f"invalid Hebrew variant lemma in {path}: {key}")
        return anchor_hebrew_variants(source_tokens, variants, metadata)
    return anchor_variants(source_tokens, variants)


def validate_variant_decisions(
    units: list[dict[str, Any]], decisions_document: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    units_by_id = {unit["id"]: unit for unit in units}
    decisions = {decision["unit_id"]: decision for decision in decisions_document["decisions"]}
    if set(units_by_id) != set(decisions):
        missing = sorted(set(units_by_id) - set(decisions))
        extra = sorted(set(decisions) - set(units_by_id))
        raise ValueError(f"variant decision coverage mismatch; missing={missing}, extra={extra}")
    for unit_id, decision in decisions.items():
        reading_ids = {candidate["id"] for candidate in units_by_id[unit_id]["candidates"]}
        compatible = set(decision["compatible_readings"])
        if not compatible or not compatible <= reading_ids:
            raise ValueError(f"invalid compatible readings for {unit_id}")
        if decision["status"] == "selected":
            if decision["selected_reading"] not in compatible or len(compatible) != 1:
                raise ValueError(f"selected variant must have exactly one compatible reading: {unit_id}")
        elif decision["status"] == "underdetermined":
            if decision["selected_reading"] is not None or len(compatible) < 2:
                raise ValueError(f"underdetermined variant requires multiple readings: {unit_id}")
        else:
            raise ValueError(f"unknown variant status in {unit_id}")
    return decisions


def project_source_tokens(
    source_tokens: list[dict[str, Any]], units: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projected = [dict(token) for token in source_tokens]
    applied = []
    # Splicing from the end keeps every earlier span's indices valid while a
    # reading that adds or drops words changes the length of the stream.
    for unit in sorted(units, key=lambda value: value["base_span"]["start"], reverse=True):
        decision = decisions[unit["id"]]
        reading_id = decision["selected_reading"] or "r00-base"
        candidate = next(c for c in unit["candidates"] if c["id"] == reading_id)
        start = unit["base_span"]["start"]
        end = unit["base_span"]["end"]
        projected[start:end] = [dict(token) for token in candidate["expanded_tokens"]]
        applied.append({
            **decision,
            "ref": unit["ref"],
            "refs": list(dict.fromkeys([*unit["base_refs"], *spanned_refs(candidate["expanded_tokens"])])),
            "operational_reading": reading_id,
            "fallback_used": decision["status"] == "underdetermined",
            "base_source_ids": unit["base_source_ids"],
            "projected_source_ids": [token["id"] for token in candidate["expanded_tokens"]],
        })
    applied.sort(key=lambda value: int(value["unit_id"].rsplit("v", 1)[1]))
    return projected, applied


def compact_verse_packets(
    source_tokens: list[dict[str, Any]], base_packets: list[dict[str, Any]],
    applied_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decisions_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token in source_tokens:
        source_by_ref[token["ref"]].append(token)
    for decision in applied_decisions:
        # A unit whose lemma crosses a verse boundary is one judgment, and
        # every verse it touches needs it: the second verse's words are under
        # the same reading as the first's.
        packet_decision = {key: value for key, value in decision.items() if key != "refs"}
        for ref in decision.get("refs") or [decision["ref"]]:
            decisions_by_ref[ref].append(packet_decision)
    packets = []
    for base in base_packets:
        compact_source = []
        for index, token in enumerate(source_by_ref[base["ref"]], 1):
            compact = {
                "id": f"s{index:03d}",
                "project_id": token["id"],
                "surface": token["surface"],
                "lemma": token.get("lemma"),
                "gloss": token.get("gloss"),
                "morph": token.get("morph"),
                "strong": token.get("strong"),
                "role": token.get("role"),
                "variant_unit": token.get("variant_unit"),
                "reading": token.get("reading"),
            }
            # Hebrew-only facts. A morpheme can have an empty surface when it
            # is written as a vowel inside another one, and a transliteration
            # is what makes an unvocalized terminal render legible, so both are
            # carried when the source supplies them and omitted when it does
            # not.
            for optional in ("pointed", "transliteration"):
                if token.get(optional):
                    compact[optional] = token[optional]
            # Where this token is written, when that is not the verse being
            # aligned: a verse the pinned edition does not contain is filled
            # from its parallel, and the packet says so rather than presenting
            # another book's Hebrew as this chapter's own.
            if token.get("supplied_from"):
                compact["supplied_from"] = token["supplied_from"]
            compact_source.append(compact)
        packets.append({
            "ref": base["ref"],
            "source_tokens": compact_source,
            "target_tokens": base["target_tokens"],
            "variant_decisions": decisions_by_ref[base["ref"]],
            "target_notes": base["target_notes"],
        })
    validate_projection_coverage(source_tokens, packets)
    return packets


def preparation_inputs_sha256(root: Path, scope: ChapterScope) -> str:
    """Hash everything a prepared packet is derived from, including this code.

    Parsing the vendored sources costs seconds; hashing them costs a fraction
    of a second, and compile, validate, evaluate, and both renderers each ask
    for the same preparation. The module hashes are deliberately included so a
    change to the packet shape invalidates the cache even when no input moved.
    """
    book = scope.book
    if scope.is_hebrew:
        # A supplied verse is read from another book, and is as much an input
        # to this chapter's packets as its own Hebrew: a cache that did not
        # hash it would serve a stale packet after that book was re-fetched.
        supplied = supplied_verse_files(root, scope)
        sources = [
            root / f"data/vendor/macula-hebrew/WLC/lowfat/{scope.macula_hebrew_lowfat_file}",
            root / f"data/vendor/oshb/wlc/{book.oshb_file}",
            *(root / f"data/vendor/macula-hebrew/WLC/lowfat/{name}"
              for name in supplied["lowfat"]),
            *(root / f"data/vendor/oshb/wlc/{name}" for name in supplied["oshb"]),
        ]
    else:
        sources = [
            root / "data/vendor/macula-greek/SBLGNT/tsv/macula-greek-SBLGNT.tsv",
            root / f"data/vendor/sblgnt/data/sblgnt/text/{book.sblgnt_name}.txt",
            root / f"data/vendor/sblgnt/data/sblgntapp/text/{book.sblgnt_name}.txt",
            root / "data/vendor/sblgnt-tei/xml/sblgnt_tei.xml",
        ]
    paths = [
        root / "config" / scope.variant_decisions_name,
        root / "config" / scope.verse_divisions_name,
        root / "config" / scope.unsourced_verses_name,
        root / f"data/vendor/asv/usx/{book.usx_file}",
        *sources,
        Path(__file__).resolve(),
        Path(__file__).resolve().with_name("pipeline.py"),
    ]
    if scope.is_hebrew:
        paths.append(root / "config/hebrew-variant-lemmas.json")
    digest = hashlib.sha256(scope.ref.encode("utf-8"))
    if not scope.is_hebrew:
        digest.update(json.dumps(chapter_moves(root, scope.ref), sort_keys=True, ensure_ascii=False).encode("utf-8"))
        digest.update(file_sha256(Path(__file__).resolve().with_name("greek_boundaries.py")).encode("utf-8"))
    for path in paths:
        digest.update(b"\0")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((file_sha256(path) if path.exists() else "absent").encode("utf-8"))
    return digest.hexdigest()


def cached_preparation(
    root: Path, scope: ChapterScope, stamp: str
) -> list[dict[str, Any]] | None:
    """Return the prepared packets when the stamp and every output still match."""
    output = root / f"data/output/{scope.slug}"
    stamp_path = output / "prepare-stamp.json"
    prepared = output / "alignment-prep.jsonl"
    if not stamp_path.exists() or not prepared.exists():
        return None
    try:
        recorded = json.loads(stamp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if recorded.get("inputs_sha256") != stamp:
        return None
    packets = load_jsonl(prepared)
    verse_directory = output / "verses"
    for packet in packets:
        verse = int(packet["ref"].rsplit(".", 1)[1])
        try:
            isolated = json.loads((verse_directory / f"{verse:02d}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if isolated != packet:
            return None
    if not (output / "projected-source-tokens.jsonl").exists():
        return None
    validate_projection_coverage(load_jsonl(output / "projected-source-tokens.jsonl"), packets)
    return packets


def prepare_pass_directories(root: Path, scope: ChapterScope) -> None:
    """Create the durable destinations before any verse worker starts.

    Directory discovery and creation are deterministic preparation work, not a
    linguistic judgment. Doing it here keeps isolated workers from spending a
    model round trip checking paths that the pipeline already knows.
    """
    chapter_directory = root / f"data/alignments/{scope.slug}"
    (chapter_directory / "verses").mkdir(parents=True, exist_ok=True)
    (chapter_directory / "reviews").mkdir(parents=True, exist_ok=True)


def prepare_alignment(
    root: Path = ROOT, chapter: str = "JHN.1", force: bool = False
) -> list[dict[str, Any]]:
    scope = chapter_scope(chapter)
    prepare_pass_directories(root, scope)
    stamp = preparation_inputs_sha256(root, scope)
    if not force:
        cached = cached_preparation(root, scope, stamp)
        if cached is not None:
            return cached
    build_chapter(scope.ref, root)
    normalized = root / f"data/normalized/{scope.slug}"
    output = root / f"data/output/{scope.slug}"
    source_tokens = load_jsonl(normalized / "source-tokens.jsonl")
    variants = load_jsonl(normalized / "source-variants.jsonl")
    packets = load_jsonl(output / "alignment-input.jsonl")
    decisions_path = root / "config" / scope.variant_decisions_name
    if not decisions_path.exists():
        raise FileNotFoundError(
            f"missing {decisions_path.relative_to(root)}; review the closed apparatus "
            f"and record {scope.ref} decisions before preparing alignment packets"
        )
    decisions_document = json.loads(decisions_path.read_text(encoding="utf-8"))
    anchored = anchor_chapter_variants(scope, source_tokens, variants, root)
    decisions = validate_variant_decisions(anchored, decisions_document)
    projected, applied = project_source_tokens(source_tokens, anchored, decisions)
    if not scope.is_hebrew:
        projected, applied = restate_selected_spans(projected, applied, chapter_moves(root, scope.ref))
    compact_packets = compact_verse_packets(projected, packets, applied)
    jsonl_write(output / "projected-source-tokens.jsonl", projected)
    jsonl_write(output / "applied-variant-decisions.jsonl", applied)
    jsonl_write(output / "alignment-prep.jsonl", compact_packets)
    verse_directory = output / "verses"
    verse_directory.mkdir(parents=True, exist_ok=True)
    for packet in compact_packets:
        verse = int(packet["ref"].rsplit(".", 1)[1])
        (verse_directory / f"{verse:02d}.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (output / "prepare-stamp.json").write_text(
        json.dumps({"chapter": scope.ref, "inputs_sha256": stamp}, indent=2) + "\n",
        encoding="utf-8",
    )
    return compact_packets


#: The response envelope, which is the part a provider's structured-output mode
#: guarantees rather than the part a model reasons its way to. Named here
#: because two callers need it: the validator below, and the runner deciding
#: whether a rejection is worth resending.
RESPONSE_KEYS = ("alignments", "ref", "review_notes")


def envelope_is_intact(response: dict[str, Any]) -> bool:
    """Did the provider return the shape it was asked for at all?

    A model held to a response schema cannot get this wrong; only one whose
    schema was never applied can. So a wrong envelope is evidence about the
    request rather than about the verse, it is the same for every verse in the
    batch, and it is the one validation failure that resending cannot fix.
    """
    return tuple(sorted(response)) == RESPONSE_KEYS


def validate_response(packet: dict[str, Any], response: dict[str, Any]) -> list[str]:
    errors = []
    if not envelope_is_intact(response):
        errors.append(
            "response keys must be exactly "
            f"{list(RESPONSE_KEYS)}; got {sorted(response)}"
        )
    if response.get("ref") != packet["ref"]:
        errors.append(f"ref must be {packet['ref']}")
    if not isinstance(response.get("alignments"), list):
        errors.append("alignments must be an array")
        return errors
    review_notes = response.get("review_notes")
    if not isinstance(review_notes, list) or not all(isinstance(note, str) for note in review_notes):
        errors.append("review_notes must be an array of strings")
    source_ids = {token["id"] for token in packet["source_tokens"]}
    target_ids = {token["id"] for token in packet["target_tokens"]}
    used_source = []
    used_target = []
    for index, unit in enumerate(response.get("alignments", []), 1):
        label = f"unit {index}"
        expected_unit_keys = {
            "source", "source_head", "target", "target_head",
            "status", "note",
        }
        if not isinstance(unit, dict):
            errors.append(f"{label} must be an object")
            continue
        # A unit aligned before `nt-greek-verse-v59` / `ot-hebrew-verse-v15`
        # carries `confidence`, and 76,646 of them are committed. The field is
        # no longer asked for — it took two of its three values, 96% of them
        # `high`, nothing gated on it, and it was the one field no two models
        # ever agreed on — but a validator that refused it would mark every
        # finished chapter invalid and owed the moment `make finish` re-proved
        # it. So it is tolerated where it is found and required nowhere.
        if set(unit) - {"confidence"} != expected_unit_keys:
            errors.append(
                f"{label} keys must be exactly {sorted(expected_unit_keys)}; "
                f"got {sorted(unit)}"
            )
        source = unit.get("source", [])
        target = unit.get("target", [])
        status = unit.get("status")
        if not isinstance(source, list) or not all(isinstance(value, str) for value in source):
            errors.append(f"{label} source must be an array of strings")
            source = []
        if not isinstance(target, list) or not all(isinstance(value, str) for value in target):
            errors.append(f"{label} target must be an array of strings")
            target = []
        if unit.get("source_head") is not None and not isinstance(unit.get("source_head"), str):
            errors.append(f"{label} source_head must be a string or null")
        if unit.get("target_head") is not None and not isinstance(unit.get("target_head"), str):
            errors.append(f"{label} target_head must be a string or null")
        if "confidence" in unit and unit["confidence"] not in {"high", "medium", "low"}:
            errors.append(f"{label} has invalid confidence {unit['confidence']!r}")
        if not isinstance(unit.get("note"), str):
            errors.append(f"{label} note must be a string")
        if len(source) != len(set(source)) or len(target) != len(set(target)):
            errors.append(f"{label} contains a duplicate ID")
        unknown_source = sorted(set(source) - source_ids)
        unknown_target = sorted(set(target) - target_ids)
        if unknown_source or unknown_target:
            errors.append(f"{label} has unknown IDs source={unknown_source} target={unknown_target}")
        if status == "aligned":
            if not source or not target:
                errors.append(f"{label} aligned must have source and target")
            if unit.get("source_head") not in source or unit.get("target_head") not in target:
                errors.append(f"{label} aligned heads must belong to the unit")
        elif status == "unrepresented":
            if not source or target or unit.get("source_head") not in source or unit.get("target_head") is not None:
                errors.append(f"{label} has invalid unrepresented shape")
        elif status == "added":
            if source or not target or unit.get("source_head") is not None:
                errors.append(f"{label} has invalid added shape")
            if unit.get("target_head") is not None and unit.get("target_head") not in target:
                errors.append(f"{label} added target_head must be null or a target member")
        elif status == "noncompositional":
            if not source or not target:
                errors.append(f"{label} noncompositional must have source and target")
            if unit.get("source_head") not in source or unit.get("target_head") not in target:
                errors.append(f"{label} noncompositional heads must belong to the unit")
        else:
            errors.append(f"{label} has unknown status {status!r}")
        used_source.extend(source)
        used_target.extend(target)
    source_counts = Counter(used_source)
    target_counts = Counter(used_target)
    missing_source = sorted(source_ids - set(source_counts))
    missing_target = sorted(target_ids - set(target_counts))
    duplicate_source = sorted(key for key, count in source_counts.items() if count > 1)
    duplicate_target = sorted(key for key, count in target_counts.items() if count > 1)
    if missing_source:
        errors.append(f"missing source IDs: {missing_source}")
    if missing_target:
        errors.append(f"missing target IDs: {missing_target}")
    if duplicate_source:
        errors.append(f"source IDs used more than once: {duplicate_source}")
    if duplicate_target:
        errors.append(f"target IDs used more than once: {duplicate_target}")
    return errors


def expand_response(
    packet: dict[str, Any], response: dict[str, Any], scope: ChapterScope
) -> dict[str, Any]:
    source_map = {token["id"]: token["project_id"] for token in packet["source_tokens"]}
    units = []
    for index, unit in enumerate(response["alignments"], 1):
        units.append({
            "id": f"{packet['ref']}.a{index:03d}",
            "source": [source_map[value] for value in unit["source"]],
            "source_head": source_map.get(unit["source_head"]),
            "target": unit["target"],
            "target_head": unit["target_head"],
            "status": unit["status"],
            # Carried through where a pre-v59/v15 verse recorded one, so that
            # compiling an old chapter does not quietly drop data its ledger
            # already published.
            **({"confidence": unit["confidence"]} if "confidence" in unit else {}),
            "note": unit["note"],
        })
    return {
        "ref": packet["ref"],
        "source_edition": (
            f"ASV-alignment {scope.source_language['name']} projection"
        ),
        "target_edition": "ASV",
        "variant_decisions": packet["variant_decisions"],
        "alignments": units,
        "review_notes": response["review_notes"],
    }


# What ran a chapter, as opposed to what governed it. Every other field in a run
# record is derived from a file on disk, so a recompile recomputes it; these two
# cannot be — nothing in the finished artifacts says which model produced them.
# They are written once by the driver, at the end of the run that did the work,
# and from then on carried through every recompile untouched.
#
# The reviewer's *model* is deliberately absent. It is already recorded per
# verse, in each review receipt, which is finer than a chapter-level field and
# cannot disagree with itself when a pass is resumed on a different route.
#
# `reviewed_by` is stamped only when that is not the whole story — when the
# review was packed. A receipt reading `Independent review by claude-code` is
# true of a verse reviewed alone and of one reviewed with seven neighbours in
# the same context, and those are not the same check. So the field is absent on
# an ordinary review, present on a packed one, and its absence means unpacked.
PROVENANCE_FIELDS = ("aligned_by", "reviewed_by", "reconciled_by")


def carried_provenance(existing_run: dict[str, Any]) -> dict[str, str]:
    """The provenance a run record already holds, for a recompile to keep.

    A compile knows what every input hashes to and cannot know what wrote them,
    so a field it cannot recompute is a field it must preserve. Without this,
    `make compile-alignment` on a finished chapter would quietly drop the record of
    what aligned it — and `make finish` recompiles, so it would be dropped by
    the very run that wrote it.
    """
    return {
        field: existing_run[field]
        for field in PROVENANCE_FIELDS
        if existing_run.get(field)
    }


def record_run_provenance(root: Path, chapter: str, **fields: str) -> Path:
    """Stamp a compiled chapter with what ran it.

    Separate from the compile because the two know different things: the
    compile knows the artifacts, and only the driver knows which route produced
    them. Written after the fact from a fact the caller already holds — no model
    is ever asked to name itself, which is the same division every review
    receipt follows.
    """
    scope = chapter_scope(chapter)
    path = root / f"data/alignments/{scope.slug}-run.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{scope.ref} has no run record to stamp; compile it first"
        )
    unknown = sorted(set(fields) - set(PROVENANCE_FIELDS))
    if unknown:
        raise ValueError(
            f"unknown provenance field(s) {', '.join(unknown)}; "
            f"expected {', '.join(PROVENANCE_FIELDS)}"
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update({name: value for name, value in fields.items() if value})
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def existing_stamp(
    existing_run: dict[str, Any], field: str, stamp_now: Callable[[], str]
) -> str:
    """Keep a chapter's existing historical stamp, or stamp a new run now."""
    if f"aligned_under_{field}" in existing_run:
        return existing_run[f"aligned_under_{field}"]
    if f"adjudicated_under_{field}" in existing_run:
        return existing_run[f"adjudicated_under_{field}"]
    return stamp_now()


def compile_alignment(
    root: Path = ROOT, chapter: str = "JHN.1"
) -> list[dict[str, Any]]:
    scope = chapter_scope(chapter)
    packets = prepare_alignment(root, scope.ref)
    response_directory = root / f"data/alignments/{scope.slug}/verses"
    responses = {}
    failures = []
    for packet in packets:
        verse = int(packet["ref"].rsplit(".", 1)[1])
        path = response_directory / f"{verse:02d}.json"
        if not path.exists():
            failures.append(f"missing {path.relative_to(root)}")
            continue
        response = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_response(packet, response)
        if errors:
            failures.append(f"{packet['ref']}: {'; '.join(errors)}")
        else:
            responses[packet["ref"]] = response
    if failures:
        raise ValueError("verse alignment validation failed:\n" + "\n".join(failures))
    records = [
        expand_response(packet, responses[packet["ref"]], scope) for packet in packets
    ]
    accepted_dir = root / "data/alignments"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    jsonl_write(accepted_dir / f"{scope.slug}.jsonl", records)
    run_record_path = accepted_dir / f"{scope.slug}-run.json"
    if run_record_path.exists():
        existing_run = json.loads(run_record_path.read_text(encoding="utf-8"))
        created_at = existing_run.get("created_at")
    else:
        existing_run = {}
        created_at = datetime.now(timezone.utc).isoformat()
    aligned_version = existing_stamp(
        existing_run, "contract_version", lambda: contract_version(scope, root)
    )
    aligned_instructions = existing_stamp(
        existing_run, "instructions_sha256",
        lambda: file_sha256(instructions_path(root, scope)),
    )
    run_record = {
        "chapter": scope.ref,
        "created_at": created_at,
        **carried_provenance(existing_run),
        "generator": "atomic verse alignments compiled by scripts/generate_alignments.py",
        "aligned_under_contract_version": aligned_version,
        "aligned_under_instructions_sha256": aligned_instructions,
        "prepared_input_sha256": file_sha256(
            root / f"data/output/{scope.slug}/alignment-prep.jsonl"
        ),
        "response_schema_sha256": file_sha256(root / "config/alignment-response.schema.json"),
        "review_receipt_schema_sha256": file_sha256(
            root / "config/review-receipt.schema.json"
        ),
        "source_lock_entries_sha256": source_lock_entries_sha256(root, scope),
        "variant_decisions_sha256": file_sha256(
            root / "config" / scope.variant_decisions_name
        ),
        "verse_divisions_sha256": verse_divisions_sha256(root, scope),
        "verse_responses_sha256": verse_responses_sha256(response_directory),
        "review_receipts_sha256": verse_responses_sha256(
            root / f"data/alignments/{scope.slug}/reviews"
        ),
        "note": "Each committed verse file passed the exact source/target partition and head validators.",
    }
    if not scope.is_hebrew:
        mapping_digest = boundary_mapping_sha256(root, scope.ref)
        if mapping_digest != "absent":
            run_record["greek_boundary_mapping_sha256"] = mapping_digest
    run_record_path.write_text(
        json.dumps(run_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_accepted_alignment(root, records, packets, scope.ref)
    return records


def validate_accepted_alignment(
    root: Path = ROOT, records: list[dict[str, Any]] | None = None,
    packets: list[dict[str, Any]] | None = None, chapter: str = "JHN.1",
) -> dict[str, Any]:
    scope = chapter_scope(chapter)
    if packets is None:
        packets = prepare_alignment(root, scope.ref)
    if records is None:
        records = load_jsonl(root / f"data/alignments/{scope.slug}.jsonl")
    records_by_ref = {record["ref"]: record for record in records}
    if {packet["ref"] for packet in packets} != set(records_by_ref):
        raise ValueError("accepted alignment verse coverage differs from prepared packets")
    total_units = 0
    for packet in packets:
        source_reverse = {token["project_id"]: token["id"] for token in packet["source_tokens"]}
        response = {
            "ref": packet["ref"],
            "review_notes": records_by_ref[packet["ref"]].get("review_notes", []),
            "alignments": [],
        }
        for unit in records_by_ref[packet["ref"]]["alignments"]:
            response["alignments"].append({
                **{key: unit[key] for key in ("target", "target_head", "status", "note")},
                **({"confidence": unit["confidence"]} if "confidence" in unit else {}),
                "source": [source_reverse[value] for value in unit["source"]],
                "source_head": source_reverse.get(unit["source_head"]),
            })
        errors = validate_response(packet, response)
        if errors:
            raise ValueError(f"accepted alignment invalid for {packet['ref']}: {errors}")
        total_units += len(response["alignments"])
    summary = {
        "chapter": scope.ref,
        "verses": len(records),
        "alignment_units": total_units,
        "source_tokens": sum(len(packet["source_tokens"]) for packet in packets),
        "target_tokens": sum(len(packet["target_tokens"]) for packet in packets),
        "status": "valid",
    }
    return summary


def conventions_path(root: Path, scope: ChapterScope) -> Path:
    return root / f"data/alignments/{scope.slug}/conventions.md"


def recorded_conventions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line[2:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]


def record_convention(root: Path, ref: str, text: str) -> Path:
    """Append a chapter-wide judgment the moment it is made.

    A chapter is aligned verse by verse, so a decision taken at verse 10
    has to survive to verse 23 and to the reviewer. Verse notes record what was
    decided locally; this file records the rule and the verse that forced it.
    It is documentation rather than an input, so it is deliberately not hashed
    into the run record: appending a convention mid-chapter must not stale an
    already-compiled chapter.
    """
    pieces = ref.upper().split(".")
    if len(pieces) not in (2, 3):
        raise ValueError(f"unknown ref: {ref}")
    scope = chapter_scope(".".join(pieces[:2]))
    verse_ref = ref.upper() if len(pieces) == 3 else None
    if verse_ref is not None:
        int(pieces[2])
    text = " ".join(text.split())
    if not text:
        raise ValueError("a convention needs text")
    path = conventions_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            f"# {scope.book.name} {scope.chapter} alignment conventions\n\n"
            "Chapter-wide judgments, recorded as they were made. Read this "
            "before continuing an interrupted chapter or starting a review "
            "range. Conventions that generalize belong in the testament "
            "contract under `config/` once the chapter is finished.\n\n",
            encoding="utf-8",
        )
    entry = f"- **{verse_ref}** — {text}\n" if verse_ref else f"- {text}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return path


#: A contract rule, as it is written in both documents: a number, a period, a
#: space, and one paragraph on one line. Later rules cite earlier ones by
#: number, so a number is an address and is never reused or renumbered.
CONTRACT_RULE = re.compile(r"^(\d+)\. (.*)$", re.MULTILINE)

#: The trailing counter in a version like `nt-greek-verse-v65`.
CONTRACT_SERIAL = re.compile(r"^(?P<stem>.*-v)(?P<serial>\d+)$")

#: The shortest a promoted rule may be. Nothing near this length has ever been
#: a rule; what arrives this short is a label for a judgment rather than the
#: judgment, and the contract is read by passes that get no chance to ask what
#: it meant.
MINIMUM_RULE = 40


def promote_contract(
    root: Path,
    scope: ChapterScope,
    rules: Iterable[str] = (),
    amendments: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Revise a testament contract and bump the version it declares.

    This is how a chapter teaches the ones after it. A conventions log reaches
    the rest of its own chapter and stops there; the contract is the document
    every later verse pass actually reads, and a rule that stayed in a log was
    a rule the project paid to rediscover.

    Two kinds of edit, because the contract has always grown by both. A new
    rule is appended under the next free number. An **amendment** rewrites a
    rule already there, which is how most of this document was actually
    learned: rule 36 gained *an article on the vocative does not take that
    pronoun*, rule 53 gained the noun-phrase predicate it had not considered,
    rule 67 was narrowed when the corpus turned out to be divided. Those are
    the same judgment a new rule is, arriving where it belongs — as the
    qualification of the rule that was too broad, rather than as a satellite
    rule pointing back at it. A contract that could only append would answer
    every discovered exception with another cross-reference.

    What it will not do is renumber. Rules cite each other by number, in both
    documents and across them, so a number is an address: an amendment changes
    what rule 36 says and never which rule is 36. Numbering is checked rather
    than trusted for the same reason — the Hebrew contract carried two rules
    numbered 40 for three days, because a rule appended to the last section
    continued that section's count, and the two rules citing `rule 40` had
    become ambiguous without anything failing.

    Bumping the version is part of the same write. The version says which text
    governed a chapter, it is stamped into run records and receipts that are
    never rewritten, and editing without bumping it would make every chapter
    aligned afterwards claim a version whose text it never saw. Nothing is
    recompiled: no line of compilation opens this file.
    """
    path = instructions_path(root, scope)
    text = path.read_text(encoding="utf-8")
    declared = CONTRACT_VERSION.search(text)
    if declared is None:
        raise ValueError(f"{path} declares no contract version")
    serial = CONTRACT_SERIAL.match(declared.group(1))
    if serial is None:
        raise ValueError(
            f"{path} declares {declared.group(1)!r}, which has no version counter"
        )
    numbered = [int(n) for n, _ in CONTRACT_RULE.findall(text)]
    duplicates = {n for n in numbered if numbered.count(n) > 1}
    if duplicates:
        # Refused rather than resolved. Every citation of a doubled number is
        # already ambiguous, and picking one of them here would write an
        # amendment into whichever copy happened to come first.
        raise ValueError(
            f"{path} numbers {sorted(duplicates)} more than once; "
            "an amendment cannot name a rule unambiguously until that is fixed"
        )

    def rule_text(rule: str, why: str) -> str:
        rule = " ".join(rule.split())
        if len(rule) < MINIMUM_RULE:
            raise ValueError(f"{why} needs more than a label: {rule!r}")
        if CONTRACT_RULE.match(rule):
            # The document numbers its own rules. Text that arrives numbered
            # was written against a contract that has since moved, and taking
            # it at its word would either collide or renumber.
            raise ValueError(f"{why} must not number itself: {rule!r}")
        return rule

    amended: list[dict[str, Any]] = []
    for amendment in amendments:
        number = amendment.get("number")
        if number not in numbered:
            raise ValueError(
                f"amendment names rule {number!r}, which {path} does not have"
            )
        rule = rule_text(amendment.get("rule") or "", f"amended rule {number}")
        line = re.compile(rf"^{number}\. .*$", re.MULTILINE)
        before = line.search(text)
        assert before is not None, "numbered above, so it matches here"
        if before.group(0) == f"{number}. {rule}":
            continue
        text = text[:before.start()] + f"{number}. {rule}" + text[before.end():]
        amended.append({
            "number": number,
            "before": before.group(0)[len(f"{number}. "):],
            "text": rule,
            "why": (amendment.get("why") or "").strip(),
        })

    written: list[dict[str, Any]] = []
    number = max(numbered, default=0)
    body = ""
    for rule in rules:
        rule = rule_text(rule, "a promoted rule")
        # Compared rule by rule rather than against the whole document. A
        # substring test reads a new rule as already present whenever some
        # longer rule happens to contain its wording, which is exactly what a
        # rule being amended to say more than it did looks like.
        standing = {existing for _, existing in CONTRACT_RULE.findall(text)}
        if rule in standing or any(rule == kept["text"] for kept in written):
            continue
        number += 1
        body += f"{number}. {rule}\n"
        written.append({"number": number, "text": rule})

    if not written and not amended:
        return {
            "path": str(path.relative_to(root)),
            "version": declared.group(1),
            "rules": [],
            "amended": [],
        }

    version = f"{serial.group('stem')}{int(serial.group('serial')) + 1}"
    declared = CONTRACT_VERSION.search(text)
    assert declared is not None, "the version line is never what an edit rewrites"
    text = text[:declared.start()] + f"This contract is `{version}`" + (
        text[declared.end():]
    )
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + body, encoding="utf-8")
    return {
        "path": str(path.relative_to(root)),
        "version": version,
        "rules": written,
        "amended": amended,
    }


def compiled_chapters(root: Path = ROOT) -> list[str]:
    """Every chapter with a compiled ledger, in canonical order.

    A compiled `data/alignments/<CHAPTER>.jsonl` is what makes a chapter
    rebuildable without asking anyone which chapters exist, so it is the list
    the whole-corpus commands work from. The order is the order of the books,
    not the order the filesystem happens to return.
    """
    chapters = [
        chapter_scope(path.stem) for path in (root / "data/alignments").glob("*.jsonl")
    ]
    return [
        scope.ref for scope in sorted(
            chapters, key=lambda scope: (int(scope.book.number), scope.chapter)
        )
    ]


def review_receipt_is_fresh(
    receipt: dict[str, Any], ref: str, accepted_path: Path,
    *, read_bytes: Callable[[Path], bytes] | None = None,
) -> bool:
    """Whether a review still stands for the verse file in front of us.

    Only the reviewed verse's own accepted file can stale its receipt. The
    receipt also records the contract version it was made under, but that is
    provenance and deliberately not a freshness test: a chapter that has been
    aligned and reviewed is finished, and a later edit to a contract must never
    put it back into a state that asks for judgment. Clearing that would mean a
    model re-reading hundreds of settled verses to confirm judgments no edit
    could have changed. Contract edits cost a recompile, and nothing more.
    """
    return (
        receipt.get("ref") == ref
        and receipt.get("status") == "reviewed"
        and receipt.get("alignment_sha256") == (hashlib.sha256(read_bytes(accepted_path)).hexdigest()
            if read_bytes is not None else file_sha256(accepted_path))
    )


def alignment_status(root: Path = ROOT, chapter: str = "JHN.1") -> dict[str, Any]:
    """Report durable per-verse checkpoints without rebuilding any input."""
    scope = chapter_scope(chapter)
    prepared_directory = root / f"data/output/{scope.slug}/verses"
    accepted_directory = root / f"data/alignments/{scope.slug}/verses"
    review_directory = root / f"data/alignments/{scope.slug}/reviews"
    decisions_path = root / "config" / scope.variant_decisions_name
    decisions_state = "missing"
    if decisions_path.exists():
        decision_document = json.loads(decisions_path.read_text(encoding="utf-8"))
        decisions_state = (
            "needs_review"
            if any(item.get("status") == "needs_review" for item in decision_document["decisions"])
            else "resolved"
        )
    prepared_paths = sorted(prepared_directory.glob("*.json"))
    compiled = (root / f"data/alignments/{scope.slug}.jsonl").exists()
    run_record_path = root / f"data/alignments/{scope.slug}-run.json"
    compiled_fresh = False
    if compiled and run_record_path.exists():
        run_record = json.loads(run_record_path.read_text(encoding="utf-8"))
        compiled_fresh = run_record_is_fresh(root, scope, run_record)
    valid = []
    missing = []
    invalid: dict[str, list[str]] = {}
    reviewed = []
    unreviewed = []
    stale_reviews = []
    large_source_units = []
    for path in prepared_paths:
        packet = json.loads(path.read_text(encoding="utf-8"))
        accepted_path = accepted_directory / path.name
        if not accepted_path.exists():
            missing.append(packet["ref"])
            continue
        response = json.loads(accepted_path.read_text(encoding="utf-8"))
        errors = validate_response(packet, response)
        if errors:
            invalid[packet["ref"]] = errors
        else:
            valid.append(packet["ref"])
            large_source_units.extend(
                {
                    "ref": item["ref"],
                    "unit": item["unit"],
                    "source_count": item["source_count"],
                }
                for item in large_source_unit_candidates([response])
            )
            receipt_path = review_directory / path.name
            if not receipt_path.exists():
                unreviewed.append(packet["ref"])
            else:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if review_receipt_is_fresh(receipt, packet["ref"], accepted_path):
                    reviewed.append(packet["ref"])
                else:
                    stale_reviews.append(packet["ref"])
    if not prepared_paths:
        if decisions_state == "missing":
            next_action = f"make init-decisions CHAPTER={scope.ref}"
        elif decisions_state == "needs_review":
            next_action = f"review config/{scope.variant_decisions_name}"
        else:
            next_action = f"make prepare-alignment CHAPTER={scope.ref}"
    elif missing or invalid:
        next_action = f"complete and validate {((missing or list(invalid))[0])}"
    elif unreviewed or stale_reviews:
        next_action = f"review and mark {((unreviewed or stale_reviews)[0])}"
    elif not compiled_fresh:
        next_action = f"make compile-alignment CHAPTER={scope.ref}"
    else:
        next_action = "chapter checkpoints are compiled; run validation and render targets"
    conventions = recorded_conventions(conventions_path(root, scope))
    return {
        "chapter": scope.ref,
        "variant_decisions": decisions_state,
        "conventions_recorded": len(conventions),
        "prepared": len(prepared_paths),
        "valid_checkpoints": len(valid),
        "missing": missing,
        "invalid": invalid,
        "reviewed_checkpoints": len(reviewed),
        "unreviewed": unreviewed,
        "stale_reviews": stale_reviews,
        "large_source_unit_candidates": large_source_units,
        "next_ref": (missing or list(invalid) or [None])[0],
        "compiled": compiled,
        "compiled_fresh": compiled_fresh,
        "next_action": next_action,
        "checkpoint_rule": "write and validate each verse before starting the next",
    }


# How a review session is told what reviewed the verse. It is an environment
# variable rather than an argument in the prompt because it is a fact about the
# run, not about the verse: the driver knows which model it routed this pass to,
# and a session asked to name itself is a session trusted with a fact it has no
# better access to than its caller. The same reasoning as believing the
# filesystem over a session's exit code.
REVIEWER_ENV = "ALIGNMENT_REVIEWER"


def mark_reviewed(
    root: Path, ref: str, notes: list[str] | None = None,
    model: str | None = None,
) -> Path:
    book, chapter_number, verse_text = ref.upper().split(".")
    scope = chapter_scope(f"{book}.{chapter_number}")
    verse = int(verse_text)
    packet_path = root / f"data/output/{scope.slug}/verses/{verse:02d}.json"
    accepted_path = root / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
    if not packet_path.exists() or not accepted_path.exists():
        raise FileNotFoundError(f"missing prepared or accepted file for {ref}")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    response = json.loads(accepted_path.read_text(encoding="utf-8"))
    errors = validate_response(packet, response)
    if errors:
        raise ValueError(f"cannot mark invalid alignment reviewed: {errors}")
    # Which model reviewed this verse, where a receipt can still be read years
    # from now. Chapters are aligned and reviewed by different models, and
    # the run ledgers that would otherwise be the only record are local
    # machinery rather than a committed artifact. Omitted rather than guessed
    # when nothing said — a receipt with no provenance is honest, and one that
    # names the wrong model is worse than either.
    reviewer = model or os.environ.get(REVIEWER_ENV) or ""
    receipt = {
        "ref": packet["ref"],
        "status": "reviewed",
        "alignment_sha256": file_sha256(accepted_path),
        "contract_version": contract_version(scope),
        **({"reviewed_by": reviewer} if reviewer else {}),
        "notes": notes or [],
    }
    path = root / f"data/alignments/{scope.slug}/reviews/{verse:02d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def initialize_variant_decisions(
    root: Path = ROOT, chapter: str = "JHN.1"
) -> Path:
    """Create a review-required decision scaffold; never overwrite judgments."""
    scope = chapter_scope(chapter)
    path = root / "config" / scope.variant_decisions_name
    if path.exists():
        raise FileExistsError(f"will not overwrite {path.relative_to(root)}")
    if scope.is_hebrew:
        # Scaffolded in the ASV's verse numbering, exactly as the build states
        # them, so the identifiers recorded here keep matching. That includes
        # reading the same Hebrew chapters the build reads: where a neighbour
        # fills part of this chapter, its ketiv/qere pairs come with it.
        oshb: dict[str, list[dict[str, Any]]] = {}
        for source_chapter in hebrew_source_chapters(root, scope):
            oshb.update(parse_oshb_words(
                root / f"data/vendor/oshb/wlc/{scope.book.oshb_file}",
                source_chapter, scope.book.code,
            ))
        units = restate_in_target_versification(
            parse_oshb_ketiv_qere(oshb, scope.book.code),
            hebrew_chapter_versification(root, scope),
            scope.book.code,
            scope.chapter,
        )
        # And the same for a verse supplied from another book: its ketiv/qere
        # pairs are this chapter's to decide, because its words are this
        # chapter's to align. Merged exactly as the build merges them, so the
        # unit ids scaffolded here are the ids the build goes on to produce.
        supplied = load_supplied_verses(
            root / "config" / scope.unsourced_verses_name, scope.ref
        )
        if supplied:
            units = sorted(
                units + supplied_verse_sources(root, scope, supplied)["variants"],
                key=lambda unit: verse_number(unit["ref"]),
            )
        for position, unit in enumerate(units, 1):
            unit["id"] = f"WLC.{unit['ref']}.v{position:03d}"
        base_note = (
            "The base candidate is the qere, which is what MACULA Hebrew "
            "projects; the alternative is the ketiv written in the Leningrad "
            "Codex."
        )
    else:
        # A Greek chapter's apparatus is not always its own chapter's. Where a
        # verse crosses a chapter boundary between the Greek and the ASV — the
        # Greek Revelation 12:18 is the ASV's 13:1 — its variation units come
        # with it, so the scaffold reads exactly the chapters the build reads
        # and keeps exactly the units the build keeps. Scaffolding only
        # `scope.chapter` silently omitted them, and the omission surfaced
        # later as a coverage mismatch with no hint of where it came from.
        units = [
            unit
            for source_chapter in greek_source_chapters(scope)
            for unit in parse_tei_variants(
                root / "data/vendor/sblgnt-tei/xml/sblgnt_tei.xml",
                source_chapter, scope.book.code, scope.book.tei_id,
            )
            if greek_record_belongs_to_scope(unit["ref"], scope)
        ]
        base_note = "The base candidate is the SBLGNT reading."
    document = {
        "schema_version": 1,
        "chapter": scope.ref,
        "policy": (
            "Review every closed candidate set. Select only when ASV wording or an "
            "explicit mapped textual note distinguishes a reading; otherwise mark "
            "underdetermined and retain the base fallback. " + base_note
        ),
        "decisions": [
            {
                "unit_id": unit["id"],
                "status": "needs_review",
                "selected_reading": None,
                "compatible_readings": [candidate["id"] for candidate in unit["candidates"]],
                "evidence": "",
            }
            for unit in units
        ],
    }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def apply_variant_decision(
    document: dict[str, Any], name: str, unit_id: str, status: str,
    reading: str | None = None, compatible: list[str] | None = None,
    evidence: str = "",
) -> dict[str, Any]:
    """Judge one unit against the loaded document, without writing anything.

    Every rule `validate_variant_decisions` enforces at preparation time is
    enforced here, at the moment the judgment is made and while whoever made it
    is still there to correct it. Separating the judging from the writing is
    what lets a batch of them be refused whole: a file half-written by a batch
    that failed on its fourth unit is worse than one not written at all.
    """
    if status not in ("selected", "underdetermined"):
        raise ValueError(f"unknown status {status!r}; expected selected or underdetermined")
    if not evidence.strip():
        raise ValueError(f"{unit_id} needs evidence for its {status} decision")

    entry = next(
        (item for item in document["decisions"] if item["unit_id"] == unit_id), None
    )
    if entry is None:
        known = ", ".join(item["unit_id"] for item in document["decisions"])
        raise ValueError(f"no unit {unit_id} in {name}; it has {known}")

    # The scaffolded candidate set is the closed set of readings this unit ever
    # had, so a narrowed set is checked against it rather than against itself.
    scaffolded = set(entry["compatible_readings"])
    if status == "selected":
        if reading is None:
            raise ValueError(f"{unit_id} is selected and needs a reading")
        if reading not in scaffolded:
            raise ValueError(
                f"{reading} is not a candidate for {unit_id}: {sorted(scaffolded)}"
            )
        entry["selected_reading"] = reading
        entry["compatible_readings"] = [reading]
    else:
        if reading is not None:
            raise ValueError(f"{unit_id} is underdetermined and cannot name a reading")
        remaining = list(compatible) if compatible else sorted(scaffolded)
        unknown = sorted(set(remaining) - scaffolded)
        if unknown:
            raise ValueError(f"not candidates for {unit_id}: {unknown}")
        if len(remaining) < 2:
            raise ValueError(
                f"{unit_id} is underdetermined but keeps {len(remaining)} reading(s); "
                "a unit the ASV settles to one reading is selected, not underdetermined"
            )
        entry["selected_reading"] = None
        entry["compatible_readings"] = remaining
    entry["status"] = status
    entry["evidence"] = evidence.strip()
    return entry


def _decisions_document(root: Path, chapter: str) -> tuple[Path, dict[str, Any]]:
    scope = chapter_scope(chapter)
    path = root / "config" / scope.variant_decisions_name
    if not path.exists():
        raise FileNotFoundError(f"missing {path.relative_to(root)}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write_decisions(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def record_variant_decision(
    root: Path, chapter: str, unit_id: str, status: str,
    reading: str | None = None, compatible: list[str] | None = None,
    evidence: str = "",
) -> dict[str, Any]:
    """Record one textual judgment, refusing anything preparation would reject.

    A session resolves decisions through this rather than editing the file,
    for the same reason a verse pass goes through the validator: the rules
    are worth enforcing at the moment the judgment is written. It also keeps
    the decisions file a thing only scripts write, which is what makes
    concurrent unit sessions safe.

    Evidence is required because a decision without it is not reviewable, and
    both statuses are decisions: "the ASV does not distinguish these readings"
    is a finding about the ASV, not an absence of one.
    """
    path, document = _decisions_document(root, chapter)
    entry = apply_variant_decision(
        document, path.name, unit_id, status,
        reading=reading, compatible=compatible, evidence=evidence,
    )
    _write_decisions(path, document)
    return entry


def record_variant_decisions(
    root: Path, chapter: str, judgments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Record a whole chapter's judgments in one call, or none of them.

    A session pays for its entire context on every turn, so recording five
    units one command at a time costs five re-reads of the same briefing to
    write about two hundred tokens. The judgments are independent and were all
    reasoned out before the first was written, so there is nothing a serial
    recording buys — measured on PHP.4, the four extra turns were most of what
    that pass spent.

    Every judgment is checked before any is written. A batch that names a
    reading outside a candidate set fails naming every unit that was wrong, and
    leaves the file exactly as it found it, so the session corrects the batch
    rather than working out which half of it landed.
    """
    path, document = _decisions_document(root, chapter)
    if not judgments:
        raise ValueError("no judgments to record")

    recorded: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, judgment in enumerate(judgments):
        unknown = sorted(
            set(judgment) - {"unit_id", "status", "reading", "compatible", "evidence"}
        )
        if unknown:
            failures.append(f"judgment {index}: unexpected field(s) {unknown}")
            continue
        unit_id = judgment.get("unit_id")
        if not unit_id:
            failures.append(f"judgment {index}: no unit_id")
            continue
        try:
            recorded.append(apply_variant_decision(
                document, path.name, unit_id, judgment.get("status", ""),
                reading=judgment.get("reading"),
                compatible=judgment.get("compatible"),
                evidence=judgment.get("evidence", ""),
            ))
        except ValueError as error:
            failures.append(str(error))
    if failures:
        raise ValueError(
            f"{len(failures)} of {len(judgments)} judgment(s) refused, and none "
            "were written:\n  " + "\n  ".join(failures)
        )

    _write_decisions(path, document)
    return recorded


def unresolved_variant_units(root: Path, chapter: str) -> list[str]:
    """Which units of this chapter still owe a judgment."""
    scope = chapter_scope(chapter)
    path = root / "config" / scope.variant_decisions_name
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        entry["unit_id"] for entry in document["decisions"]
        if entry.get("status") == "needs_review"
    ]


def initialize_verse_divisions(
    root: Path = ROOT, chapter: str = "JHN.1"
) -> Path:
    """Scaffold the division decisions the versification table demands.

    The table says which Hebrew verses divide and where each of their two parts
    goes; it cannot say where the division falls, because it falls between two
    words. So the scaffold carries everything derivable and leaves exactly the
    judgment blank, with the verse's words listed beside it so a reviewer can
    choose an ordinal by reading rather than by counting in another window.

    A part does not always become a whole ASV verse. It may be the tail of the
    previous one, the head of the next, or -- at Isaiah 63:19 -- material the
    next ASV chapter aligns and this one does not. Each target is therefore a
    full reference, with a plain-language note saying which of those it is.
    """
    scope = chapter_scope(chapter)
    if not scope.is_hebrew:
        raise ValueError(f"{scope.ref} is a Greek chapter; only Hebrew verses divide")
    path = root / "config" / scope.verse_divisions_name
    if path.exists():
        raise FileExistsError(f"will not overwrite {path.relative_to(root)}")
    book = scope.book
    divided = divided_verses(
        root / "data/vendor/oshb/wlc/VerseMap.xml",
        Path(book.oshb_file).stem, scope.chapter, scope.ref,
    )
    if not divided:
        raise ValueError(f"{scope.ref} has no divided verse in the versification table")
    words = parse_oshb_words(
        root / f"data/vendor/oshb/wlc/{book.oshb_file}", scope.chapter, book.code
    )

    def role(target_ref: str, target_part: str | None) -> str:
        if not target_ref.startswith(f"{scope.ref}."):
            return (
                f"outside this chapter: {target_ref} aligns it, and {scope.ref} "
                "drops it"
            )
        if target_part == "a":
            return f"the head of ASV {target_ref}"
        if target_part == "b":
            return f"the tail of ASV {target_ref}"
        return f"the whole of ASV {target_ref}"

    document = {
        "schema_version": 2,
        "chapter": scope.ref,
        "policy": (
            "One Hebrew verse's words fill two different ASV places here. Read "
            "both texts, find the Hebrew word the second part begins at, and "
            "record its 1-based position within the Hebrew verse together with "
            "its consonants. The targets and the notes beside them are derived "
            "from the versification table; only the position is a judgment. The "
            "build checks the position and the consonants against each other and "
            "refuses the chapter until every entry is resolved."
        ),
        "divisions": [
            {
                "source_verse": source_verse,
                "status": "needs_review",
                "first_target": parts["a"][0],
                "first_target_is": role(*parts["a"]),
                "second_target": parts["b"][0],
                "second_target_is": role(*parts["b"]),
                "second_begins_at_word": None,
                "second_begins_with": None,
                "evidence": "",
                "hebrew_words": [
                    {"word": word["index"], "text": word["text"]}
                    for word in words.get(f"{book.code}.{scope.chapter}.{source_verse}", [])
                ],
            }
            for source_verse, parts in sorted(divided.items())
        ],
    }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare, checkpoint, compile, and validate a chapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("chapter")
    prepare.add_argument(
        "--force", action="store_true",
        help="rebuild even when the cached preparation stamp still matches",
    )
    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("chapter")
    validate_verse = subparsers.add_parser("validate-verse")
    validate_verse.add_argument("ref")
    validate = subparsers.add_parser("validate")
    validate.add_argument("chapter")
    status = subparsers.add_parser("status")
    status.add_argument("chapter")
    init_decisions = subparsers.add_parser("init-decisions")
    init_decisions.add_argument("chapter")
    init_divisions = subparsers.add_parser("init-divisions")
    init_divisions.add_argument("chapter")
    decide = subparsers.add_parser("decide")
    decide.add_argument("chapter")
    decide.add_argument("unit_id")
    decide.add_argument("--status", required=True,
                        choices=("selected", "underdetermined"))
    decide.add_argument("--reading", help="the selected reading id, for --status selected")
    decide.add_argument("--compatible",
                        help="comma-separated readings the ASV leaves open, for "
                             "--status underdetermined (default: every candidate)")
    decide.add_argument("--evidence", required=True,
                        help="the ASV wording, supply, or mapped note that decided it")
    decide_many = subparsers.add_parser(
        "decide-many",
        help="record every judgment for a chapter at once, from a JSON array",
    )
    decide_many.add_argument("chapter")
    decide_many.add_argument(
        "--judgments", default="-",
        help="a file of JSON objects with unit_id, status, and evidence, plus "
             "reading or compatible; defaults to stdin",
    )
    mark_reviewed_parser = subparsers.add_parser("mark-reviewed")
    mark_reviewed_parser.add_argument("ref")
    mark_reviewed_parser.add_argument("--note", action="append", default=[])
    mark_reviewed_parser.add_argument(
        "--model",
        help=f"what reviewed the verse; defaults to ${REVIEWER_ENV}, which the "
             "driver sets for a routed pass",
    )
    convention = subparsers.add_parser("record-convention")
    convention.add_argument("ref", help="a chapter such as EPH.1 or the verse that forced the rule")
    convention.add_argument("text")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        scope = chapter_scope(args.chapter)
        packets = prepare_alignment(ROOT, scope.ref, force=args.force)
        print(json.dumps({"chapter": scope.ref, "packets": len(packets)}, indent=2))
    elif args.command == "compile":
        scope = chapter_scope(args.chapter)
        records = compile_alignment(ROOT, scope.ref)
        print(json.dumps(
            validate_accepted_alignment(ROOT, records, chapter=scope.ref), indent=2
        ))
    elif args.command == "validate-verse":
        try:
            book, chapter_number, verse_text = args.ref.upper().split(".")
            scope = chapter_scope(f"{book}.{chapter_number}")
            verse = int(verse_text)
        except (ValueError, IndexError):
            parser.error(f"unknown ref: {args.ref}")
        input_path = ROOT / f"data/output/{scope.slug}/verses/{verse:02d}.json"
        if not input_path.exists():
            parser.error(f"missing prepared input: {input_path.relative_to(ROOT)}")
        packet = json.loads(input_path.read_text(encoding="utf-8"))
        if packet["ref"] != args.ref:
            parser.error(f"prepared input ref is {packet['ref']}, not {args.ref}")
        path = ROOT / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
        if not path.exists():
            parser.error(f"missing alignment: {path.relative_to(ROOT)}")
        response = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_response(packet, response)
        print(json.dumps({"ref": args.ref, "valid": not errors, "errors": errors}, indent=2))
        if errors:
            return 1
    elif args.command == "validate":
        print(json.dumps(validate_accepted_alignment(ROOT, chapter=args.chapter), indent=2))
    elif args.command == "status":
        print(json.dumps(alignment_status(ROOT, args.chapter), indent=2))
    elif args.command == "init-decisions":
        try:
            path = initialize_variant_decisions(ROOT, args.chapter)
        except FileExistsError as error:
            parser.error(str(error))
        print(json.dumps({"created": str(path), "status": "needs_review"}, indent=2))
    elif args.command == "decide":
        try:
            entry = record_variant_decision(
                ROOT, args.chapter, args.unit_id, args.status,
                reading=args.reading,
                compatible=(
                    [piece.strip() for piece in args.compatible.split(",") if piece.strip()]
                    if args.compatible else None
                ),
                evidence=args.evidence,
            )
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        remaining = unresolved_variant_units(ROOT, args.chapter)
        print(json.dumps(
            {"recorded": entry, "still_needs_review": remaining}, ensure_ascii=False,
            indent=2,
        ))
    elif args.command == "decide-many":
        source = (
            sys.stdin.read() if args.judgments == "-"
            else Path(args.judgments).read_text(encoding="utf-8")
        )
        try:
            judgments = json.loads(source)
        except json.JSONDecodeError as error:
            parser.error(f"--judgments is not JSON: {error}")
        if not isinstance(judgments, list):
            parser.error("--judgments must be a JSON array of judgment objects")
        try:
            entries = record_variant_decisions(ROOT, args.chapter, judgments)
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        # A receipt, not a copy: the session already knows what it decided, and
        # printing every entry back costs it the whole batch again on the next
        # turn. What it cannot know is what is still owed.
        print(json.dumps({
            "recorded": {
                entry["unit_id"]: " ".join(
                    [entry["status"], *entry["compatible_readings"]]
                )
                for entry in entries
            },
            "still_needs_review": unresolved_variant_units(ROOT, args.chapter),
        }, ensure_ascii=False, indent=2))
    elif args.command == "init-divisions":
        try:
            path = initialize_verse_divisions(ROOT, args.chapter)
        except (FileExistsError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps({"created": str(path), "status": "needs_review"}, indent=2))
    elif args.command == "record-convention":
        try:
            path = record_convention(ROOT, args.ref, args.text)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps({
            "ref": args.ref,
            "conventions": str(path),
            "recorded": len(recorded_conventions(path)),
        }, indent=2))
    else:
        try:
            path = mark_reviewed(ROOT, args.ref, args.note, args.model)
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps({"reviewed": args.ref, "receipt": str(path)}, indent=2))
    return 0

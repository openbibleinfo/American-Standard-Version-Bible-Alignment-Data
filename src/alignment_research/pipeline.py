from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shlex
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


from .scope import BOOKS, ChapterScope, chapter_scope
from .transliteration import greek_transliteration, hebrew_transliteration
from .versification import (
    TITLE_VERSE,
    chapter_versification,
    consonants,
    load_supplied_verses,
    load_verse_divisions,
    source_chapters,
)


ROOT = Path(__file__).resolve().parents[2]
WORD_RE = re.compile(r"[^\W_]+(?:[’'][^\W_]+)*", re.UNICODE)
GREEK_WORD_RE = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]+", re.UNICODE)
HEBREW_WORD_RE = re.compile(r"[\u0590-\u05ff\ufb1d-\ufb4f]+", re.UNICODE)
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
TEI = "{http://www.tei-c.org/ns/1.0}"
OSIS = "{http://www.bibletechnologies.net/2003/OSIS/namespace}"
# MACULA tags every Hebrew token with the language it is actually written in,
# which matters in the Aramaic sections of Daniel and Ezra.
MACULA_LANGUAGES = {"H": "hbo", "A": "arc"}


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def display_surface(token: dict[str, Any]) -> str:
    """The token's own letters, or its citation form when it has none.

    A Hebrew article assimilated into a pointed preposition is a real token
    with real morphology and no letters of its own. Showing nothing would hide
    it; showing its citation form unmarked would claim letters that are not in
    the text, so it is bracketed. MACULA leaves such a token's pointed form
    empty as well, in which case its lemma is the only citation form there is.
    """
    surface = token.get("surface") or ""
    if surface:
        return surface
    citation = token.get("pointed") or token.get("lemma")
    return f"[{citation}]" if citation else ""


def display_transliteration(token: dict[str, Any]) -> str:
    """The token's romanization, bracketed when it has no letters of its own.

    This is `display_surface` in the Latin alphabet, and it brackets for the
    same reason. MACULA romanizes a letterless morpheme as an en dash, which
    reads as nothing at all, so a display that shows the source only romanized
    would hide the token; its citation form is romanized and bracketed instead.
    Deriving that one form is not a second transliteration scheme: it is the
    same one applied to the letters the lexicon writes.
    """
    surface = token.get("surface") or ""
    transliteration = (token.get("transliteration") or "").strip()
    if surface and transliteration:
        return transliteration
    citation = token.get("pointed") or token.get("lemma") or ""
    romanized = (
        greek_transliteration(citation)
        if token.get("language") == "grc"
        else hebrew_transliteration(citation)
    )
    return f"[{romanized}]" if romanized else ""


def jsonl_write(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def strong_id(raw: str | None, prefix: str = "G") -> str | None:
    """Return Strong's metadata without numeric zero padding.

    MACULA supplies bare numeric values, while some imported formats supply
    prefixed values and may append a letter or put more than one value in an
    attribute. Strip padding from each G/H numeric component without disturbing
    those suffixes or separators. A bare value carries no testament of its own,
    so the caller names the prefix; MACULA Hebrew in particular writes `0871a`
    for what is cited as `H871a`.
    """
    if not raw:
        return None
    values = []
    for item in raw.split():
        match = re.fullmatch(r"(\d+)([A-Za-z]?)", item)
        if match:
            values.append(f"{prefix}{int(match.group(1))}{match.group(2)}")
        else:
            values.append(re.sub(
                r"([GH])0*(\d+)",
                lambda component: f"{component.group(1)}{int(component.group(2))}",
                item,
            ))
    return " ".join(values)


def classify_asv_note(text: str) -> str:
    lowered = text.casefold()
    if re.match(r"^(some|many|another).*(authorit|reading|read\b)", lowered):
        return "textual_variant"
    if lowered.startswith(("greek ", "gr. ")):
        return "greek_gloss"
    if lowered.startswith(("hebrew ", "heb. ")):
        return "hebrew_gloss"
    if "septuagint" in lowered or lowered.startswith(("sept. ", "syriac ", "vulgate ")):
        return "versional"
    if lowered.startswith("or,"):
        return "alternative_rendering"
    if re.fullmatch(r"(?:[1-3] ?[A-Za-z]+\.?)? ?\d[^A-Za-z]*", text):
        return "cross_reference"
    return "other"


def parse_asv_usx(
    path: Path, chapter: int, book_code: str = "JHN"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = ET.parse(path).getroot()
    segments: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    raw_notes: dict[int, list[str]] = defaultdict(list)
    state: dict[str, int | None] = {"chapter": None, "verse": None}
    # A note whose caller sits immediately before a verse milestone annotates
    # the verse that follows it, not the one before. That happens whenever the
    # note is on a verse's opening word, which is common in the poetic books.
    deferred: list[str] = []

    def add_text(value: str | None, added: bool) -> None:
        if value and state["chapter"] == chapter and state["verse"] is not None:
            segments[int(state["verse"])].append((value, added))

    def note_text(element: ET.Element) -> str:
        return compact_space("".join(element.itertext()))

    def walk(element: ET.Element, inherited_added: bool = False) -> None:
        if element.tag == "chapter":
            state["chapter"] = int(element.attrib["number"])
            state["verse"] = None
            return
        if element.tag == "verse":
            state["verse"] = int(re.match(r"\d+", element.attrib["number"]).group())
            if state["chapter"] == chapter:
                raw_notes[int(state["verse"])].extend(deferred)
            deferred.clear()
            return
        if element.tag == "note":
            if state["chapter"] == chapter and state["verse"] is not None:
                raw_notes[int(state["verse"])].append(note_text(element))
            return

        # A descriptive title is the ASV's rendering of a Hebrew
        # superscription, which the Hebrew numbers as a verse and the ASV does
        # not number at all. It is real translated text, so it is collected as
        # verse 0 rather than dropped with the editorial section headings.
        title = (
            element.tag == "para"
            and element.attrib.get("style") == "d"
            and state["chapter"] == chapter
            and state["verse"] is None
        )
        if title:
            state["verse"] = TITLE_VERSE

        added = inherited_added or (element.tag == "char" and element.attrib.get("style") == "add")
        add_text(element.text, added)
        children = list(element)
        for index, child in enumerate(children):
            if (
                child.tag == "note"
                and index + 1 < len(children)
                and children[index + 1].tag == "verse"
                and not (child.tail or "").strip()
            ):
                # Held regardless of chapter: the list is drained and cleared
                # at every verse milestone, so nothing leaks across chapters.
                deferred.append(note_text(child))
                continue
            walk(child, added)
            add_text(child.tail, added)

        if title:
            state["verse"] = None

    walk(root)
    verses: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for verse in sorted(segments):
        tokens: list[dict[str, Any]] = []
        for text, added in segments[verse]:
            for match in WORD_RE.finditer(text):
                tokens.append({
                    "id": f"t{len(tokens) + 1:03d}",
                    "text": match.group(),
                    "added": added,
                })
        ref = f"{book_code}.{chapter}.{verse}"
        verses.append({
            "ref": ref,
            "edition": "ASV",
            "text": compact_space("".join(text for text, _ in segments[verse])),
            "tokens": tokens,
        })
        for index, text in enumerate(raw_notes.get(verse, []), 1):
            notes.append({
                "id": f"{ref}.n{index:02d}",
                "ref": ref,
                "class": classify_asv_note(text),
                "text": text,
            })
    return verses, notes


# MACULA's TSV derivation glues a word's trailing mark onto the word itself in
# seven of the New Testament's 137,000-odd tokens, and in one of them writes the
# mark twice: `εἰσιν` carries an ASCII semicolon *and* the U+037E it moved. The
# two also spell the same mark differently -- elision, the question mark -- in
# 2,185 places. Left alone both reach the transliteration derived from the
# surface, the aligned USX, and the packet a verse pass reads: a pass on
# Galatians 3 was shown `ἀλλ’·` as a word and `all’·` as its sound.
#
# `config/macula-punctuation.json` holds the lowfat XML's reading of the glued
# tokens and the character equivalences, and
# `scripts/reconcile_macula_punctuation.py` derives it and reports what is left
# over -- the edition's own dashes, parentheses and brackets, which are its
# punctuation rather than a shared mark spelled differently, and which no
# mapping can invent.
@lru_cache(maxsize=None)
def macula_punctuation(root: Path = ROOT) -> tuple[dict[str, dict[str, str]], dict[int, str]]:
    document = json.loads(
        (root / "config/macula-punctuation.json").read_text(encoding="utf-8")
    )
    return document["glued_marks"], str.maketrans(document["characters"])


def repair_macula_row(
    xml_id: str, text: str, after: str, root: Path = ROOT
) -> tuple[str, str]:
    """Put a glued mark back where the lowfat keeps it, spelled as the edition does.

    A vendor bump that unglues one of these upstream is welcome and passes
    through; one that changes the surface to something neither the TSV nor the
    lowfat wrote is not, and stops the build rather than repairing a word it no
    longer recognizes.
    """
    glued, characters = macula_punctuation(root)
    repair = glued.get(xml_id)
    if repair is not None and text != repair["surface"]:
        if text != repair["broken"]:
            raise ValueError(
                f"MACULA token {xml_id} reads {text!r}, which is neither the "
                f"known broken surface {repair['broken']!r} nor the lowfat's "
                f"{repair['surface']!r}"
            )
        text, after = repair["surface"], repair["after"] + after
    return text.translate(characters), after.translate(characters)


def parse_macula_tsv(
    path: Path, chapter: int, book_code: str = "JHN", macula_code: str = "JHN"
) -> list[dict[str, Any]]:
    result = []
    prefix = f"{macula_code} {chapter}:"
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if not row["ref"].startswith(prefix):
                continue
            verse_ref, position = row["ref"].split("!")
            verse = int(verse_ref.split(":")[1])
            source_xml_id = row["xml:id"]
            surface, after = repair_macula_row(
                source_xml_id, row["text"], row["after"]
            )
            result.append({
                "id": f"SBLGNT.{book_code}.{chapter}.{verse}.{int(position):03d}",
                "source_xml_id": source_xml_id,
                "ref": f"{book_code}.{chapter}.{verse}",
                "language": "grc",
                "edition": "SBLGNT",
                "dataset": "MACULA-SBLGNT",
                "surface": surface,
                "after": after,
                "normalized": row["normalized"],
                # MACULA Greek publishes no transliteration column, so unlike
                # Hebrew this one is always derived for display.
                "transliteration": greek_transliteration(surface),
                "transliteration_source": "derived",
                "lemma": row["lemma"],
                "strong": strong_id(row["strong"]),
                "morph": row["morph"],
                "gloss": row["gloss"],
                "role": row["role"] or None,
                "raw": {key: value for key, value in row.items() if value and key not in {
                    "xml:id", "ref", "text", "after", "normalized", "lemma", "strong", "morph", "gloss", "role"
                }},
            })
    return result


def parse_official_sblgnt(
    path: Path, chapter: int, book_name: str = "John", book_code: str = "JHN"
) -> dict[str, str]:
    result: dict[str, str] = {}
    prefix = f"{book_name} {chapter}:"
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(prefix):
                ref, text = line.rstrip("\n").split("\t", 1)
                result[ref.replace(book_name, book_code).replace(" ", ".").replace(":", ".")] = text
    return result


# The editions the SBLGNT-TEI transcription collates. Written without the
# marginal sigla on purpose: `\b` does not break inside `WHmarg`, so `WH` here
# matches the edition and not its margin.
TEI_COLLATED_EDITIONS = re.compile(r"\b(?:WH|Treg|NIV|RP)\b")


def records_only_na_edition_change(entry: str) -> bool:
    """Is this official entry one the TEI transcription has no unit for?

    A handful of printed entries exist only to record where NA28 departed from
    NA27 — `7 δὲ WH Treg NA27 RP ] – NA28` in 1 John 1:7, and `τεκνία` in 3:7.
    Every edition the TEI collates stands with the lemma there, so the TEI has
    nothing to state and prints no unit. They are apparatus entries all the
    same, and the report keeps them; what they must not do is consume a TEI
    unit, because that shifts every later pairing in the chapter by one.

    The signature is both halves together: NA27 cited at all, and no collated
    edition on the far side of the bracket. An entry where NA27 merely joins a
    reading that WH, Treg, NIV, or RP also splits over — 3 John 5, Jude 5 — does
    have a TEI unit and is not this.

    The signature is necessary but not sufficient, because the two witness
    lists are not always the same list: 2 Peter 2:20 prints `+ ἡμῶν NA27` and
    the TEI gives that same reading to NIV, so a unit stands there after all.
    Confirm against the unit in hand with `official_entry_states_unit` before
    dropping an entry on this signature alone.
    """
    if "NA27" not in entry or "]" not in entry:
        return False
    return not TEI_COLLATED_EDITIONS.search(entry.split("]", 1)[1])


GREEK_LETTER = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")


def official_entry_lemma(entry: str) -> str:
    """The Greek an official entry quotes, without its verse number or sigla.

    Everything before the bracket is the lemma plus the editions that read it;
    only the lemma is Greek, so the ASCII at either end is what to drop.
    """
    words = entry.split("]", 1)[0].split()
    while words and not GREEK_LETTER.search(words[-1]):
        words.pop()
    while words and not GREEK_LETTER.search(words[0]):
        words.pop(0)
    return " ".join(words)


def official_entry_states_unit(
    entry: str, span: set[str], ref: str, lemma: str
) -> bool:
    """Is this official entry the one that states this TEI unit?

    Same verse and same opening word. The first word rather than the whole
    lemma, because an entry that spans several words may print them elided —
    `ἀγάπης … κεῖμαι` — where the TEI keeps every word it covers.
    """
    if ref not in span:
        return False
    quoted = official_entry_lemma(entry).split()
    stated = lemma.split()
    return bool(quoted) and bool(stated) and quoted[0] == stated[0]


def parse_official_apparatus_refs(
    path: Path, chapter: int, book_name: str = "John", book_code: str = "JHN"
) -> list[str]:
    """The refs of the apparatus entries the TEI is expected to state.

    One ref per printed entry, so that the count and order can be compared with
    the TEI units; entries the TEI deliberately omits are left out here for the
    same reason the report skips them.
    """
    refs = []
    current_verse: int | None = None
    active = False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith(f"{book_name} "):
                active = stripped.startswith(f"{book_name} {chapter}:")
                current_verse = int(stripped.split(":", 1)[1]) if active else None
            elif active and stripped and current_verse is not None:
                if records_only_na_edition_change(stripped):
                    continue
                refs.append(f"{book_code}.{chapter}.{current_verse}")
    return refs


def validate_greek_surface(tokens: list[dict[str, Any]], official: dict[str, str]) -> list[dict[str, Any]]:
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token in tokens:
        by_ref[token["ref"]].append(token)
    checks = []
    all_refs = sorted(set(by_ref) | set(official))
    for ref in all_refs:
        # MACULA's `after` contains either whitespace or punctuation.  Unlike the
        # lowfat XML's rendered paragraph, punctuation values do not themselves
        # carry the following space, so restore it for an edition-level check.
        macula = "".join(
            t["surface"] + t["after"] + ("" if t["after"].endswith(tuple(" \t\r\n")) else " ")
            for t in by_ref.get(ref, [])
        )
        canonical = official.get(ref, "")
        normalized_macula = unicodedata.normalize("NFC", macula)
        normalized_official = unicodedata.normalize("NFC", canonical)
        macula_words = [unicodedata.normalize("NFC", token["surface"]) for token in by_ref.get(ref, [])]
        # Normalize before tokenization.  The Greek question mark U+037E is
        # canonically equivalent to ASCII semicolon; matching first would keep
        # it inside the Greek Unicode range and then normalize it into a stray
        # suffix on the preceding word.
        official_words = GREEK_WORD_RE.findall(normalized_official)
        checks.append({
            "ref": ref,
            # The reports print the official edition's own words, locating each
            # one by the position MACULA gives its token, so the count is what
            # has to hold: a verse the two tokenize differently would silently
            # misplace an apparatus span. Their spelling need not agree, and in
            # 28 verses it does not -- the two editions accent, elide and
            # capitalize differently, which is punctuation, not text.
            "word_count_match": len(macula_words) == len(official_words),
            "lexical_match": macula_words == official_words,
            "exact_rendering_match": normalized_macula == normalized_official,
            "macula": macula,
            "official": canonical,
        })
    return checks


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    pieces: list[str] = []
    for node in element.iter():
        if node.tag in (f"{TEI}w", f"{TEI}pc") and node.text:
            pieces.append(node.text)
    return compact_space(" ".join(pieces) if pieces else "".join(element.itertext()))


def reading_tokens(text: str) -> list[str]:
    return GREEK_WORD_RE.findall(unicodedata.normalize("NFC", text))


def tei_apparatus_positions(
    book: ET.Element,
) -> dict[ET.Element, int]:
    """Each ``app`` in the book's running text, by word offset in its verse.

    An entry nested inside another entry's lemma stands at no position of its
    own and is absent here, which leaves it the search anchor.

    The TEI writes an apparatus entry inline, between the words it stands
    between, so the position it addresses is known exactly. Recovering the
    lemma by searching the running text instead throws that away, and a
    one-word lemma such as a bare article then anchors on whichever occurrence
    comes first: 1 Cor 1:28 read ``καὶ καὶ τὰ ἀγενῆ`` because the ``καὶ τὰ``
    of ``τὰ μὴ ὄντα`` was spliced in at the verse's opening article.

    The offset counts source words since the verse milestone, with each
    preceding lemma standing for the words it quotes, which is how the MACULA
    token stream counts them too.
    """
    positions: dict[ET.Element, int] = {}
    words = 0

    def walk(element: ET.Element) -> None:
        nonlocal words
        for child in element:
            if child.tag == f"{TEI}milestone" and child.attrib.get("unit") == "verse":
                words = 0
            elif child.tag == f"{TEI}app":
                positions[child] = words
                # The lemma is the text the apparatus replaces, so it occupies
                # the running text's next words. Do not descend: the readings'
                # own words are alternatives, not more text.
                words += len(reading_tokens(element_text(child.find(f"{TEI}lem"))))
            elif child.tag == f"{TEI}w":
                words += 1
            else:
                walk(child)

    walk(book)
    return positions


def parse_tei_variants(
    path: Path, chapter: int, book_code: str = "JHN", tei_book_id: str = "B04"
) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    book = root.find(f".//{TEI}div[@{XML_ID}='{tei_book_id}']")
    if book is None:
        raise ValueError(f"{book_code} (TEI {tei_book_id}) not found")
    units = []
    # Enumerated from the same walk as ever, so that the unit numbering every
    # decision file and alignment cites is untouched. An entry nested inside
    # another entry's lemma -- Matthew 16:2-3 brackets one -- stands at no
    # position of its own in the running text and keeps the search anchor.
    positions = tei_apparatus_positions(book)
    for app in book.findall(f".//{TEI}app"):
        location = app.attrib.get("loc", "")
        if not location.startswith(f"{chapter}:"):
            continue
        verse = int(location.split(":", 1)[1].split("-", 1)[0])
        ref = f"{book_code}.{chapter}.{verse}"
        lemma_text = element_text(app.find(f"{TEI}lem"))
        candidates: list[dict[str, Any]] = [{
            "id": "r00-base",
            "text": lemma_text,
            "tokens": reading_tokens(lemma_text),
            "base": True,
            "witnesses": [],
        }]
        seen = {lemma_text: candidates[0]}
        for rdg in app.findall(f"{TEI}rdg"):
            text = compact_space("".join(rdg.itertext()))
            witnesses = [w.removeprefix("#") for w in rdg.attrib.get("wit", "").split()]
            if text == lemma_text:
                candidates[0]["witnesses"] = sorted(set(candidates[0]["witnesses"] + witnesses))
            elif text in seen:
                seen[text]["witnesses"] = sorted(set(seen[text]["witnesses"] + witnesses))
            else:
                candidate = {
                    "id": f"r{len(candidates):02d}",
                    "text": text,
                    "tokens": reading_tokens(text),
                    "base": False,
                    "witnesses": witnesses,
                }
                candidates.append(candidate)
                seen[text] = candidate
        units.append({
            "id": f"SBLGNT.{ref}.v{len(units) + 1:03d}",
            "ref": ref,
            "location": location,
            "lemma": lemma_text,
            "lemma_word_index": positions.get(app),
            "candidates": candidates,
        })
    return units


def file_signature(path: Path) -> tuple[int, int, int, int, int]:
    """Cheaply identify one on-disk revision for parser-local caches."""
    stat = path.stat()
    return (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    )


def parse_macula_hebrew(
    path: Path, chapter: int, book_code: str = "GEN", macula_code: str = "GEN"
) -> list[dict[str, Any]]:
    """Read one MACULA Hebrew lowfat chapter as an ordered morpheme list.

    Hebrew is segmented below the orthographic word: a prefixed conjunction,
    preposition, or article and a suffixed pronoun are separate tokens sharing
    the word position recorded in ``ref``.  That is the granularity the
    alignment contract wants, so it is preserved rather than collapsed.

    A morpheme may also have no letters of its own — the article assimilated
    into a pointed preposition is written only as a vowel — so ``surface`` can
    be empty while ``pointed`` still carries MACULA's citation form.
    """
    root = ET.parse(path).getroot()
    # The chapter is verified from the per-word references rather than from the
    # file's own `id` attribute, because that attribute is not reliable: the
    # Psalm 10 lowfat file is labelled `PSA 1` upstream while every word in it
    # is correctly referenced `PSA 10:...`. The references are also the field
    # this parser consumes, so checking them checks what is actually used.
    expected_prefix = f"{macula_code} {chapter}:"
    words = list(root.iter("w"))
    wrong = {
        word.attrib["ref"].split("!")[0] for word in words
        if not word.attrib.get("ref", "").startswith(expected_prefix)
    }
    if not words or wrong:
        raise ValueError(
            f"{path.name} does not contain {expected_prefix.rstrip(':')!r}; "
            f"found {sorted(wrong)[:3] or 'no words'}. Check the MACULA book "
            "code in alignment_research.scope"
        )
    counters: dict[str, int] = defaultdict(int)
    result = []
    # A lowfat file is a syntax tree, so its document order is constituent
    # order and not always text order: in Genesis 1:4 the object clause marker
    # `כִּי` precedes the object it introduces. The identifier encodes book,
    # chapter, verse, word, and morpheme in fixed-width fields, so sorting on
    # it restores the order the text is actually written in.
    for word in sorted(root.iter("w"), key=lambda element: element.attrib[XML_ID]):
        verse_ref, word_position = word.attrib["ref"].split("!")
        verse = int(verse_ref.split(":")[1])
        ref = f"{book_code}.{chapter}.{verse}"
        counters[ref] += 1
        attributes = dict(word.attrib)
        xml_id = attributes.pop(XML_ID)
        # MACULA's own transliteration is morphologically informed, so it wins
        # wherever it exists. It is absent for the pieces of a word that was
        # split before a pronominal suffix, and those gaps are filled from the
        # pointing with the same conventions.
        supplied = attributes.get("transliteration")
        pointed = attributes.get("unicode") or (word.text or "")
        result.append({
            "id": f"WLC.{book_code}.{chapter}.{verse}.{counters[ref]:03d}",
            "source_xml_id": xml_id,
            "ref": ref,
            "oshb_word": int(word_position),
            "language": MACULA_LANGUAGES.get(attributes.get("lang"), "hbo"),
            "edition": "WLC",
            "dataset": "MACULA-Hebrew",
            "surface": word.text or "",
            "pointed": pointed,
            "after": attributes.get("after", ""),
            "normalized": pointed,
            "transliteration": supplied or hebrew_transliteration(pointed),
            "transliteration_source": "macula" if supplied else "derived",
            "lemma": attributes.get("lemma"),
            "strong": strong_id(attributes.get("strongnumberx"), "H"),
            "morph": attributes.get("morph"),
            "gloss": attributes.get("gloss"),
            "role": attributes.get("role") or None,
            "raw": {
                key: value for key, value in attributes.items()
                if value and key not in {
                    "ref", "after", "unicode", "transliteration", "lemma",
                    "strongnumberx", "morph", "gloss", "role", "lang",
                }
            },
        })
    return result


def parse_oshb_words(
    path: Path, chapter: int, book_code: str = "GEN"
) -> dict[str, list[dict[str, Any]]]:
    """Read the OSHB chapter as ordered words, keeping ketiv and qere apart.

    OSHB writes the ketiv in the running text and its qere inside a note.  Both
    are ``w`` elements with stable OSHB identifiers, and both are counted in
    document order, because that order is what MACULA's word position indexes.
    """
    root = ET.parse(path).getroot()
    words: dict[str, list[dict[str, Any]]] = {}
    # OSHB names its divisions after its own file stem, such as `Gen.1`.
    for element in root.iter(f"{OSIS}chapter"):
        if element.attrib.get("osisID") != f"{path.stem}.{chapter}":
            continue
        for verse_element in element.iter(f"{OSIS}verse"):
            verse = int(verse_element.attrib["osisID"].rsplit(".", 1)[1])
            ref = f"{book_code}.{chapter}.{verse}"
            collected: list[dict[str, Any]] = []
            pairs = {"next": 0}

            def walk(node: ET.Element, inside_qere: bool = False, pair: int | None = None) -> None:
                for child in node:
                    # A variant note holds the qere for the written form just
                    # before it. The two sides need not be one word each: at
                    # Psalm 10:10 a single written word is read as two, so the
                    # note is what groups them rather than adjacency.
                    if (
                        child.tag == f"{OSIS}note"
                        and child.attrib.get("type") == "variant"
                        and not inside_qere
                    ):
                        pairs["next"] += 1
                        group = pairs["next"]
                        position = len(collected) - 1
                        while (
                            position >= 0
                            and collected[position]["kind"] == "ketiv"
                            and collected[position]["pair"] is None
                        ):
                            collected[position]["pair"] = group
                            position -= 1
                        walk(child, inside_qere, group)
                        continue
                    qere = inside_qere or (
                        child.tag == f"{OSIS}rdg"
                        and child.attrib.get("type") == "x-qere"
                    )
                    if child.tag == f"{OSIS}w":
                        kind = "qere" if qere else (
                            "ketiv" if child.attrib.get("type") == "x-ketiv" else "word"
                        )
                        collected.append({
                            "index": len(collected) + 1,
                            "ref": ref,
                            "oshb_id": child.attrib.get("id"),
                            "kind": kind,
                            "text": child.text or "",
                            "segments": (child.text or "").split("/"),
                            "lemma": child.attrib.get("lemma"),
                            "morph": child.attrib.get("morph"),
                            "pair": pair if qere else None,
                        })
                    walk(child, qere, pair)

            walk(verse_element)
            words[ref] = collected
    if not words:
        raise ValueError(f"{path.name} has no chapter {chapter}")
    return words


def validate_hebrew_surface(
    tokens: list[dict[str, Any]], oshb: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Check MACULA morphemes against the OSHB words they were split from.

    Two things have to hold for the projection to be trustworthy. Every MACULA
    word must reassemble, letter for letter, into the OSHB word at the same
    position; and MACULA must cover every OSHB word except the ketiv forms it
    deliberately replaces with their qere.
    """
    by_word: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for token in tokens:
        by_word[token["ref"]][token["oshb_word"]].append(token)
    checks = []
    for ref in sorted(
        set(by_word) | set(oshb),
        key=lambda value: (chapter_number(value), verse_number(value)),
    ):
        words = oshb.get(ref, [])
        macula_words = by_word.get(ref, {})
        mismatches = []
        for word in words:
            joined = "".join(
                token["surface"] for token in macula_words.get(word["index"], [])
            )
            expected = word["text"].replace("/", "")
            present = word["index"] in macula_words
            if word["kind"] == "ketiv":
                if present:
                    mismatches.append(f"ketiv {word['index']} was not replaced by its qere")
                continue
            if not present:
                mismatches.append(f"OSHB word {word['index']} has no MACULA tokens")
            elif unicodedata.normalize("NFC", joined) != unicodedata.normalize("NFC", expected):
                mismatches.append(f"OSHB word {word['index']}: {joined!r} != {expected!r}")
        extra = sorted(set(macula_words) - {word["index"] for word in words})
        mismatches.extend(f"MACULA word {index} is not in OSHB" for index in extra)
        checks.append({
            "ref": ref,
            "word_match": not mismatches,
            "mismatches": mismatches,
            "oshb_words": len(words),
            "macula_words": len(macula_words),
            "macula_morphemes": sum(len(items) for items in macula_words.values()),
        })
    return checks


def ketiv_qere_group_anchors(words: list[dict[str, Any]]) -> dict[int, int]:
    """Each note group in a verse and the OSHB word position it anchors on.

    The first word of the qere, or -- where the note reads nothing at all, the
    ketiv velo qere -- the first written word the reading passes over. This is
    the anchor ``parse_oshb_ketiv_qere`` gives the unit, so a count taken here
    and the units built there cannot drift apart.
    """
    sides: dict[int, dict[str, list[int]]] = defaultdict(
        lambda: {"ketiv": [], "qere": []}
    )
    for word in words:
        if word["kind"] in ("ketiv", "qere") and word["pair"] is not None:
            sides[word["pair"]][word["kind"]].append(word["index"])
    return {
        pair: (group["qere"] or group["ketiv"])[0]
        for pair, group in sides.items()
    }


def unit_anchor_word(unit: dict[str, Any]) -> int:
    """The OSHB word position a ketiv/qere unit sits at.

    The qere's own words, ordinarily. Where the qere reads nothing, the
    written words it passes over say where the unit belongs, because a unit
    with no position at all could not be ordered or anchored.
    """
    words = unit["oshb_words"] or unit.get("oshb_ketiv_words") or []
    if not words:
        raise ValueError(f"{unit['id']} has no OSHB word position to anchor on")
    return int(words[0])


def parse_oshb_ketiv_qere(
    oshb: dict[str, list[dict[str, Any]]], book_code: str = "GEN"
) -> list[dict[str, Any]]:
    """Model each ketiv/qere pair as a closed two-candidate variation unit.

    This is the Hebrew counterpart of the SBLGNT apparatus: a small, closed,
    fully enumerated set of readings that the manuscript tradition itself
    records. MACULA projects the qere, so the qere is the base candidate and
    the ketiv is the alternative, and a decision is made the same way as in
    Greek — selected only when ASV wording or an explicit ASV note
    distinguishes them.
    """
    def reading(words: list[dict[str, Any]], identifier: str, base: bool, label: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "text": " ".join(word["text"].replace("/", "") for word in words),
            "tokens": [word["text"].replace("/", "") for word in words],
            "base": base,
            "witnesses": [label],
            "oshb_id": " ".join(word["oshb_id"] or "" for word in words).strip() or None,
            "segments": [segment for word in words for segment in word["segments"]],
            "lemma": " ".join(word["lemma"] or "" for word in words).strip() or None,
            "morph": " ".join(word["morph"] or "" for word in words).strip() or None,
            "word_segments": [word["segments"] for word in words],
            "word_lemmas": [word["lemma"] for word in words],
            "word_morphs": [word["morph"] for word in words],
        }

    units: list[dict[str, Any]] = []
    for ref, words in oshb.items():
        chapter = int(ref.split(".")[1])
        verse = int(ref.rsplit(".", 1)[1])
        groups: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: {"ketiv": [], "qere": []}
        )
        for word in words:
            if word["kind"] in ("ketiv", "qere") and word["pair"] is not None:
                groups[word["pair"]][word["kind"]].append(word)
            elif word["kind"] in ("ketiv", "qere"):
                raise ValueError(
                    f"{ref} has an unpaired {word['kind']} ({word['oshb_id']}); "
                    "the Masoretic note structure was not what this parser expects"
                )
        for group in sorted(groups):
            ketiv = groups[group]["ketiv"]
            qere = groups[group]["qere"]
            # A written form whose note holds no reading is the Masoretic
            # *ketiv velo qere*: the word stands in the text but is not read,
            # as at Jeremiah 51:3. That is still a closed two-candidate set --
            # the reading is the omission -- so it is modelled like any other
            # pair, with an empty base candidate and no qere word positions.
            units.append({
                "id": f"WLC.{ref}.v{len(units) + 1:03d}",
                "ref": ref,
                "location": f"{chapter}:{verse}",
                "lemma": " ".join(word["text"].replace("/", "") for word in qere),
                # MACULA projects the qere, so the unit is anchored on the word
                # positions the qere occupies, which may be more than one. A
                # qere that reads nothing occupies none, and the written words
                # it passes over are what locates the unit instead.
                "oshb_words": [word["index"] for word in qere],
                "oshb_ketiv_words": [word["index"] for word in ketiv],
                "candidates": [
                    reading(qere, "r00-base", True, "qere"),
                    reading(ketiv, "r01", False, "ketiv"),
                ],
            })
    units.sort(key=lambda unit: (
        int(unit["ref"].rsplit(".", 1)[1]), unit_anchor_word(unit),
    ))
    for position, unit in enumerate(units, 1):
        unit["id"] = f"WLC.{unit['ref']}.v{position:03d}"
    return units


# SBLGNT numbers this clause as Revelation 12:18, while the ASV places the
# complete clause at the beginning of 13:1.  Keep the source occurrence IDs,
# but restate the packet reference in the ASV frame just as the Hebrew pipeline
# does for source/target versification differences.
GREEK_ASV_VERSE_MOVES = {"REV.12.18": "REV.13.1"}

# The same divergence, but partial: SBLGNT closes Revelation 17:9 with καὶ
# βασιλεῖς ἑπτά εἰσιν, which the ASV renders at the head of 17:10.  Only the
# tail of the source verse changes hands, so unlike a whole-verse move the
# entry has to say where the verse divides.  Every token from `first_source_id`
# onward moves and the rest stays put; `surface` is checked against those
# tokens, so a repinned SBLGNT that renumbers the verse fails the build instead
# of quietly moving the wrong words.  A variant unit anchored in the moved tail
# must move with it, or it would anchor against the wrong verse.
GREEK_ASV_CLAUSE_MOVES = {
    "REV.17.9": {
        "target": "REV.17.10",
        "first_source_id": "SBLGNT.REV.17.9.019",
        "surface": "καὶ βασιλεῖς ἑπτά εἰσιν",
        "variant_units": ["SBLGNT.REV.17.9.v011"],
    },
}


def greek_clause_move(ref: str) -> dict[str, Any] | None:
    """The recorded partial move out of this source verse, if it has one."""
    move = GREEK_ASV_CLAUSE_MOVES.get(ref)
    if move is None:
        return None
    if move["target"].rsplit(".", 1)[0] != ref.rsplit(".", 1)[0]:
        raise ValueError(
            f"clause move {ref} -> {move['target']} crosses a chapter boundary, "
            "which the Greek pipeline does not model"
        )
    return move


def split_greek_clause_moves(
    tokens: list[dict[str, Any]], scope: ChapterScope
) -> list[dict[str, Any]]:
    """Restate the tail of a source verse the ASV writes as the next verse.

    The token IDs are untouched: `SBLGNT.REV.17.9.019` still records where the
    word stands in the Greek, and `source_ref` names its Greek verse. Only the
    packet holding it changes, so the clause is aligned against the
    English that actually renders it.
    """
    moved = []
    for token in tokens:
        source_ref = token.get("source_ref", token["ref"])
        move = greek_clause_move(source_ref)
        if move is None or move["target"].rsplit(".", 1)[0] != scope.ref:
            moved.append(token)
            continue
        if token["id"] < move["first_source_id"]:
            moved.append(token)
            continue
        restated = dict(token)
        restated["source_ref"] = source_ref
        restated["ref"] = move["target"]
        moved.append(restated)
    for source_ref, move in GREEK_ASV_CLAUSE_MOVES.items():
        if move["target"].rsplit(".", 1)[0] != scope.ref:
            continue
        tail = [
            token for token in moved
            if token.get("source_ref") == source_ref and token["ref"] == move["target"]
        ]
        surface = " ".join(token["surface"] for token in tail)
        if surface != move["surface"]:
            raise ValueError(
                f"clause move {source_ref} -> {move['target']} expected "
                f"{move['surface']!r} from {move['first_source_id']}, got {surface!r}"
            )
    return moved


def split_greek_clause_variants(
    units: list[dict[str, Any]], scope: ChapterScope
) -> list[dict[str, Any]]:
    """Move a variant unit into the verse its base tokens now sit in."""
    moved = []
    for unit in units:
        move = greek_clause_move(unit["ref"])
        if (
            move is None
            or move["target"].rsplit(".", 1)[0] != scope.ref
            or unit["id"] not in move["variant_units"]
        ):
            moved.append(unit)
            continue
        restated = dict(unit)
        restated["source_ref"] = unit["ref"]
        restated["ref"] = move["target"]
        moved.append(restated)
    return moved


def greek_source_chapters(scope: ChapterScope) -> list[int]:
    chapters = {scope.chapter}
    for source_ref, target_ref in GREEK_ASV_VERSE_MOVES.items():
        if target_ref.rsplit(".", 1)[0] == scope.ref:
            chapters.add(int(source_ref.split(".")[1]))
    return sorted(chapters)


def greek_record_belongs_to_scope(ref: str, scope: ChapterScope) -> bool:
    target_ref = GREEK_ASV_VERSE_MOVES.get(ref, ref)
    return target_ref.rsplit(".", 1)[0] == scope.ref


def restate_greek_asv_boundaries(
    records: list[dict[str, Any]], scope: ChapterScope
) -> list[dict[str, Any]]:
    restated = []
    for record in records:
        source_ref = record["ref"]
        if not greek_record_belongs_to_scope(source_ref, scope):
            continue
        target_ref = GREEK_ASV_VERSE_MOVES.get(source_ref, source_ref)
        moved = dict(record)
        if target_ref != source_ref:
            moved["source_ref"] = source_ref
            moved["ref"] = target_ref
        restated.append(moved)
    return restated


def build_greek_chapter(
    ref: str = "JHN.1", root: Path = ROOT
) -> dict[str, Any]:
    scope = chapter_scope(ref)
    book = scope.book
    with (root / "config/sources.lock.json").open(encoding="utf-8") as handle:
        source_lock = json.load(handle)["sources"]
    normalized = root / f"data/normalized/{scope.slug}"
    output = root / f"data/output/{scope.slug}"
    asv, notes = parse_asv_usx(
        root / f"data/vendor/asv/usx/{book.usx_file}", scope.chapter, book.code
    )
    raw_greek: list[dict[str, Any]] = []
    raw_official: dict[str, str] = {}
    raw_variants: list[dict[str, Any]] = []
    raw_official_apparatus_refs: list[str] = []
    for source_chapter in greek_source_chapters(scope):
        raw_greek.extend(parse_macula_tsv(
            root / "data/vendor/macula-greek/SBLGNT/tsv/macula-greek-SBLGNT.tsv",
            source_chapter, book.code, book.macula_code,
        ))
        raw_official.update(parse_official_sblgnt(
            root / f"data/vendor/sblgnt/data/sblgnt/text/{book.sblgnt_name}.txt",
            source_chapter, book.sblgnt_name, book.code,
        ))
        raw_official_apparatus_refs.extend(parse_official_apparatus_refs(
            root / f"data/vendor/sblgnt/data/sblgntapp/text/{book.sblgnt_name}.txt",
            source_chapter, book.sblgnt_apparatus_name, book.code,
        ))
        raw_variants.extend(parse_tei_variants(
            root / "data/vendor/sblgnt-tei/xml/sblgnt_tei.xml",
            source_chapter, book.code, book.tei_id,
        ))

    selected_greek = [
        token for token in raw_greek if greek_record_belongs_to_scope(token["ref"], scope)
    ]
    selected_official = {
        ref: text for ref, text in raw_official.items()
        if greek_record_belongs_to_scope(ref, scope)
    }
    selected_variants = [
        unit for unit in raw_variants if greek_record_belongs_to_scope(unit["ref"], scope)
    ]
    selected_official_apparatus_refs = [
        ref for ref in raw_official_apparatus_refs
        if greek_record_belongs_to_scope(ref, scope)
    ]
    greek_checks = validate_greek_surface(selected_greek, selected_official)
    greek = split_greek_clause_moves(restate_greek_asv_boundaries(selected_greek, scope), scope)
    variants = split_greek_clause_variants(
        restate_greek_asv_boundaries(selected_variants, scope), scope
    )
    jsonl_write(normalized / "asv-target-tokens.jsonl", asv)
    jsonl_write(normalized / "asv-notes.jsonl", notes)
    jsonl_write(normalized / "source-tokens.jsonl", greek)
    jsonl_write(normalized / "source-variants.jsonl", variants)

    greek_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    variants_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    notes_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token in greek:
        greek_by_ref[token["ref"]].append(token)
    for unit in variants:
        variants_by_ref[unit["ref"]].append(unit)
    for note in notes:
        notes_by_ref[note["ref"]].append(note)
    packets = [{
        "ref": verse["ref"],
        "source_edition": "SBLGNT",
        "target_edition": "ASV",
        "source_tokens": greek_by_ref[verse["ref"]],
        "target_tokens": verse["tokens"],
        "variant_units": variants_by_ref[verse["ref"]],
        "target_notes": notes_by_ref[verse["ref"]],
    } for verse in asv]
    jsonl_write(output / "alignment-input.jsonl", packets)

    summary = {
        "chapter": scope.ref,
        "testament": scope.testament,
        "source_edition": "SBLGNT",
        "status": "ready_for_alignment",
        "counts": {
            "asv_verses": len(asv),
            "asv_tokens": sum(len(v["tokens"]) for v in asv),
            "asv_notes": len(notes),
            "source_verses": len({t["ref"] for t in greek}),
            "source_tokens": len(greek),
            "source_variant_units": len(variants),
            # A Greek verse never merges, but one can divide: where the ASV
            # writes the tail of a source verse as the next verse, the clause
            # is restated into the packet that renders it.
            "divided_verses": len([
                source_ref for source_ref, move in GREEK_ASV_CLAUSE_MOVES.items()
                if move["target"].rsplit(".", 1)[0] == scope.ref
            ]),
            "merged_verses": 0,
            "dropped_segments": 0,
            # An ASV verse the pinned edition does not contain, as the
            # Leningrad Codex does not contain the ASV's Nehemiah 7:68. The
            # SBLGNT has a verse for every verse the ASV prints -- where the
            # critical text omits one the ASV omits it too -- so this is
            # stated rather than derived from a shortfall: a Greek chapter
            # that came up a verse short would be a build to fix, and
            # `validate_summary` still fails it.
            "supplied_verses": 0,
            "unsourced_verses": 0,
            "excluded_source_tokens": 0,
        },
        "checks": {
            "source_tokens_match_reference_edition": all(
                c["word_count_match"] for c in greek_checks
            ),
            "source_token_mismatches": [
                c["ref"] for c in greek_checks if not c["word_count_match"]
            ],
            # Reported rather than failed; see validate_greek_surface.
            "macula_lexical_form_differences": [
                c["ref"] for c in greek_checks if not c["lexical_match"]
            ],
            "macula_exact_rendering_differences": [
                c["ref"] for c in greek_checks if not c["exact_rendering_match"]
            ],
            "variant_units_match_reference_apparatus": (
                [unit["ref"] for unit in selected_variants]
                == selected_official_apparatus_refs
            ),
        },
        "versification": {
            "source_to_target": [
                {"source": source_ref, "target": target_ref}
                for source_ref, target_ref in GREEK_ASV_VERSE_MOVES.items()
                if source_ref.rsplit(".", 1)[0] == scope.ref
                or target_ref.rsplit(".", 1)[0] == scope.ref
            ],
            "clause_to_target": [
                {
                    "source": source_ref,
                    "target": move["target"],
                    "from_source_id": move["first_source_id"],
                    "surface": move["surface"],
                }
                for source_ref, move in GREEK_ASV_CLAUSE_MOVES.items()
                if move["target"].rsplit(".", 1)[0] == scope.ref
            ],
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "build-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def verse_number(ref: str) -> int:
    return int(ref.rsplit(".", 1)[1])


def chapter_number(ref: str) -> int:
    return int(ref.rsplit(".", 2)[1])


def source_key(record: dict[str, Any]) -> tuple[int, int]:
    """The Hebrew chapter and verse a record came from.

    ``source_ref`` is present once a record has been restated in the ASV's
    numbering and holds where it came from; before that, ``ref`` is still the
    Hebrew reference. Almost every record's chapter is the one being built, but
    a chapter filled in part by its neighbour's Hebrew has records that are not.
    """
    ref = record.get("source_ref") or record["ref"]
    return (chapter_number(ref), verse_number(ref))


def record_word_ordinal(record: dict[str, Any]) -> int | None:
    """The OSHB word position a record starts at, where it has one.

    A morpheme carries ``oshb_word``; a ketiv/qere unit carries the list of
    positions its reading occupies and begins at the first of them. Everything
    else may have none, and a record with no
    position is placed by its verse alone.
    """
    word = record.get("oshb_word")
    if word is not None:
        return int(word)
    words = record.get("oshb_words") or record.get("oshb_ketiv_words")
    return int(words[0]) if words else None


def restate_in_target_versification(
    records: list[dict[str, Any]], mapping: Any, book_code: str, chapter: int,
    prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Renumber records from Hebrew verse numbering into the ASV's.

    The Hebrew numbering is kept as ``source_ref`` rather than discarded: it is
    what the OSHB and MACULA identifiers mean, and a reader checking this
    alignment against a Hebrew text needs it. Identifiers are rebuilt in the
    ASV frame so that a token's name and its reference cannot disagree.

    A record the table sends to another ASV chapter is dropped here, because
    this chapter genuinely does not align it. That is never silent: the build
    lists the dropped segments and checks the count against this result.
    """
    restated = []
    for index, record in enumerate(records):
        moved = dict(record)
        source_chapter, source_verse = source_key(record)
        # Only a divided verse consults the word ordinal, so a stream that has
        # none -- a whole-verse reference record, for instance -- keeps landing
        # on the first of the segments filling its target, and every undivided
        # chapter is untouched by this argument existing.
        target_verse = mapping.locate(
            source_verse, record_word_ordinal(record), source_chapter
        )
        if target_verse is None:
            continue
        moved["source_ref"] = record["ref"]
        moved["ref"] = f"{book_code}.{chapter}.{target_verse}"
        restated.append((target_verse, index, moved))
    restated.sort(key=lambda item: (item[0], item[1]))
    counters: dict[str, int] = defaultdict(int)
    result = []
    for _, _, record in restated:
        if prefix:
            counters[record["ref"]] += 1
            record["id"] = f"{prefix}.{record['ref']}.{counters[record['ref']]:03d}"
        result.append(record)
    return result


def merge_title_into_first_verse(
    asv: list[dict[str, Any]], notes: list[dict[str, Any]],
    book_code: str, chapter: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fold an unnumbered title into the verse whose Hebrew source holds it.

    Where the MT writes the superscription into the verse it numbers 1 instead
    of numbering it separately, there is no Hebrew verse for a verse 0 to be
    aligned against. The title is still the ASV's rendering of that
    superscription, so its words are aligned alongside the rest of the verse
    that contains them: one packet, one partition of one Hebrew verse. The
    aligned USX and the interlinear still find the title in its own
    paragraph, because the tokens are folded in document order.
    """
    title_ref = f"{book_code}.{chapter}.{TITLE_VERSE}"
    first_ref = f"{book_code}.{chapter}.1"
    title = next((verse for verse in asv if verse["ref"] == title_ref), None)
    first = next((verse for verse in asv if verse["ref"] == first_ref), None)
    if title is None or first is None:
        return asv, notes
    first["text"] = compact_space(f"{title['text']} {first['text']}")
    first["tokens"] = [
        {**token, "id": f"t{position:03d}"}
        for position, token in enumerate(title["tokens"] + first["tokens"], 1)
    ]
    merged_notes = []
    position = 0
    for note in notes:
        if note["ref"] in (title_ref, first_ref):
            position += 1
            note = {**note, "ref": first_ref, "id": f"{first_ref}.n{position:02d}"}
        merged_notes.append(note)
    return [verse for verse in asv if verse["ref"] != title_ref], merged_notes


def oshb_chapters(path: Path) -> set[int]:
    """Which chapters the Hebrew book actually has.

    The ASV's chapter numbering is not the Hebrew's, and in Malachi it runs one
    chapter past it. OSHB publishes a book at a time and is fetched before the
    per-chapter MACULA files are asked for, so it is the thing on disk that can
    answer whether a chapter of Hebrew exists to read at all.
    """
    root = ET.parse(path).getroot()
    chapters = set()
    # OSHB names its divisions after its own file stem, such as `Mal.3`.
    for element in root.iter(f"{OSIS}chapter"):
        osis_id = element.attrib.get("osisID", "")
        if osis_id.startswith(f"{path.stem}."):
            chapters.add(int(osis_id.rsplit(".", 1)[1]))
    return chapters


def hebrew_source_chapters(root: Path, scope: ChapterScope) -> tuple[int, ...]:
    """Which Hebrew chapters an ASV chapter's text is drawn from.

    Read from the versification table and the Hebrew book's own chapter list,
    both of which the fetch script has on disk before any per-chapter source
    has been fetched.
    """
    oshb_file = root / f"data/vendor/oshb/wlc/{scope.book.oshb_file}"
    return source_chapters(
        root / "data/vendor/oshb/wlc/VerseMap.xml",
        Path(scope.book.oshb_file).stem,
        scope.chapter,
        scope.ref,
        has_own_chapter=scope.chapter in oshb_chapters(oshb_file),
    )


def hebrew_chapter_versification(
    root: Path, scope: ChapterScope,
    asv: list[dict[str, Any]] | None = None,
    source_verses: set[int] | None = None,
) -> Any:
    """Resolve one Hebrew chapter's verse mapping from the pinned table.

    Both the chapter build and the decision scaffold need this, and they must
    agree: a unit identifier that named a Hebrew verse number in one place and
    an ASV verse number in the other would not match itself.

    ``source_verses`` are this chapter's own Hebrew verses. Where a neighbouring
    chapter fills part of this one, that chapter's recorded divisions are read
    from its own decision file, because a division is decided once by the
    chapter whose verse divides.
    """
    book = scope.book
    if asv is None:
        asv, _ = parse_asv_usx(
            root / f"data/vendor/asv/usx/{book.usx_file}", scope.chapter, book.code
        )
    if source_verses is None:
        # An ASV chapter that runs past the end of its Hebrew book has none of
        # its own: the ASV's Malachi 4 is Hebrew Malachi 3:19-24 entire, and
        # every segment of it arrives from the chapter before.
        oshb_file = root / f"data/vendor/oshb/wlc/{book.oshb_file}"
        source_verses = {
            verse_number(ref)
            for ref in parse_oshb_words(oshb_file, scope.chapter, book.code)
        } if scope.chapter in oshb_chapters(oshb_file) else set()
    target_verses = {verse_number(verse["ref"]) for verse in asv}
    incoming_divisions = {}
    for source_chapter in hebrew_source_chapters(root, scope):
        if source_chapter == scope.chapter:
            continue
        neighbour = chapter_scope(f"{book.code}.{source_chapter}")
        incoming_divisions[source_chapter] = load_verse_divisions(
            root / "config" / neighbour.verse_divisions_name, neighbour.ref
        )
    return chapter_versification(
        root / "data/vendor/oshb/wlc/VerseMap.xml",
        Path(book.oshb_file).stem,
        scope.chapter,
        scope.ref,
        source_verses,
        target_verses - {TITLE_VERSE},
        has_title=TITLE_VERSE in target_verses,
        divisions=load_verse_divisions(
            root / "config" / scope.verse_divisions_name, scope.ref
        ),
        incoming_divisions=incoming_divisions,
    )


def verify_verse_divisions(
    tokens: list[dict[str, Any]], versification: Any, ref: str,
) -> None:
    """Prove a recorded division still points at the word it was approved for.

    The decision is a word ordinal, and an ordinal is only as stable as the
    edition it was counted in. Re-fetching a source could in principle shift
    it, and a division that lands one word early or late produces a chapter
    that validates perfectly and is wrong. So the word is named in the decision
    and checked here against the text actually in hand.

    A chapter holds every word of its own verses, so its own divisions are
    always checkable. Of a neighbour's divided verse it holds only the part
    that came to it, so the anchor word may not be here at all; that division
    is checked by the chapter that owns the verse, and skipped here.
    """
    for (source_chapter, source_verse), division in sorted(versification.divisions.items()):
        in_verse = [
            token for token in tokens if source_key(token) == (source_chapter, source_verse)
        ]
        at_word = [
            token for token in in_verse
            if token.get("oshb_word") == division.second_begins_at_word
        ]
        if not at_word:
            if source_chapter != versification.chapter:
                continue
            raise ValueError(
                f"{ref} divides Hebrew verse {source_verse} at word "
                f"{division.second_begins_at_word}, but that verse has no such word"
            )
        # A decision names an orthographic word, but a source token is a
        # morpheme, so the word has to be reassembled before it can be
        # compared: `וַ/יֹּ֤אמֶר` is two tokens and one recorded anchor.
        found = consonants("".join(token["surface"] for token in at_word))
        if found != division.second_begins_with:
            owner = f"{ref.rsplit('.', 1)[0]}.{source_chapter}"
            raise ValueError(
                f"{ref} divides Hebrew verse {source_verse} at word "
                f"{division.second_begins_at_word}, recorded as "
                f"{division.second_begins_with!r}, but that word is now {found!r}; "
                f"review config/{owner}-verse-divisions.json against the source"
            )


def excluded_source_tokens(
    tokens: list[dict[str, Any]], versification: Any, ref: str,
) -> list[dict[str, Any]]:
    """The morphemes this chapter deliberately does not align, checked.

    Isaiah 63:19 divides, and the ASV puts its second part at Isaiah 64:1. That
    material is real Hebrew this chapter must not claim, so it is dropped — but
    dropping is exactly the kind of thing that should never happen quietly. The
    segments the table sends elsewhere are named in the build summary, and each
    one has to account for at least one morpheme actually present, or the model
    is describing a text that is not in hand.
    """
    excluded = [
        token for token in tokens
        if versification.locate(
            source_key(token)[1], record_word_ordinal(token), source_key(token)[0]
        ) is None
    ]
    for segment in versification.dropped:
        claimed = [token for token in excluded if source_key(token) == segment.key]
        if not claimed:
            raise ValueError(
                f"{ref} drops Hebrew {segment.label} to {segment.target_ref}, but no "
                "source token falls in it; the versification table and the source "
                "text disagree"
            )
    return excluded


def incoming_source_tokens(
    tokens: list[dict[str, Any]], versification: Any, ref: str,
) -> list[dict[str, Any]]:
    """The morphemes this chapter aligns that another Hebrew chapter contains.

    The mirror of ``excluded_source_tokens``, and checked for the same reason.
    Isaiah 64:1 is the ASV's name for Hebrew Isaiah 63:19b, so Isaiah 64 aligns
    words it does not itself contain; borrowing text is exactly the kind of
    thing that must not happen quietly, so every incoming segment is named in
    the build summary and has to account for at least one morpheme actually in
    hand.
    """
    incoming = [
        token for token in tokens if source_key(token)[0] != versification.chapter
    ]
    for segment in versification.incoming:
        if not [token for token in incoming if source_key(token) == segment.key]:
            raise ValueError(
                f"{ref} takes Hebrew {segment.reference} from another chapter as "
                f"{segment.target_ref}, but no source token falls in it; the "
                "versification table and the source text disagree"
            )
    return incoming


def supplied_verse_files(root: Path, scope: Any) -> dict[str, list[str]]:
    """Which other book's files a chapter's supplied verses are read from.

    Three callers need this before any of them can read a source: the fetch
    script, which has to check the other book out; the packet cache, which has
    to hash it, since a supplied verse is as much an input as the chapter's own
    Hebrew; and the build itself. All three read the same recorded decision.
    """
    if not scope.is_hebrew:
        return {"oshb": [], "lowfat": []}
    supplied = load_supplied_verses(
        root / "config" / scope.unsourced_verses_name, scope.ref
    )
    oshb_files: list[str] = []
    lowfat_files: list[str] = []
    for verse in sorted(supplied):
        source_ref = supplied[verse].source_ref
        if source_ref is None:
            continue
        book_code, chapter_text, _ = source_ref.rsplit(".", 2)
        source_scope = chapter_scope(f"{book_code}.{chapter_text}")
        oshb_file = source_scope.book.oshb_file
        lowfat = source_scope.macula_hebrew_lowfat_file_for(source_scope.chapter)
        if oshb_file and oshb_file not in oshb_files:
            oshb_files.append(oshb_file)
        if lowfat not in lowfat_files:
            lowfat_files.append(lowfat)
    return {"oshb": oshb_files, "lowfat": lowfat_files}


def supplied_verse_sources(
    root: Path, scope: Any, supplied: dict[int, Any],
) -> dict[str, Any]:
    """Read the pinned parallel that fills a verse this edition does not have.

    The ASV's Nehemiah 7:68 is not in the Leningrad Codex, and the same muster
    list is: Ezra 2:66 has the horses and the mules, in a chapter WLC does
    contain, which the ASV renders in exactly the same English. So the verse is
    filled from there rather than left as English aligned to nothing — the
    reconstruction this project publishes is of the Hebrew the ASV evidences,
    and a hole in it is a claim about the ASV that is not true.

    What comes back is already in the ASV's frame: the tokens carry the target
    ref, so they align like any other verse, and keep `source_ref` and
    `supplied_from` naming the verse they are written in, so nothing downstream
    can mistake them for Nehemiah's own Hebrew. The words are checked against
    what the decision recorded before any of it is used.
    """
    tokens: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    oshb_words = 0
    apparatus_units = 0
    for verse in sorted(supplied):
        supply = supplied[verse]
        if supply.source_ref is None:
            continue
        book_code, chapter_text, verse_text = supply.source_ref.rsplit(".", 2)
        source_book = BOOKS.get(book_code)
        if source_book is None or source_book.testament != "OT":
            raise ValueError(
                f"{scope.ref} supply for {supply.target_ref} names {supply.source_ref}, "
                f"which is not an Old Testament book this build knows"
            )
        source_chapter, source_verse = int(chapter_text), int(verse_text)
        source_scope = chapter_scope(f"{book_code}.{source_chapter}")
        source_ref = f"{book_code}.{source_chapter}.{source_verse}"
        oshb = {
            ref: words for ref, words in parse_oshb_words(
                root / f"data/vendor/oshb/wlc/{source_book.oshb_file}",
                source_chapter, book_code,
            ).items() if ref == source_ref
        }
        if not oshb:
            raise ValueError(
                f"{scope.ref} supply for {supply.target_ref} names {source_ref}, "
                "which the pinned WLC does not contain"
            )
        # The recorded words are the check, exactly as a division records the
        # word it begins at: a re-fetched edition that moved would otherwise
        # supply different Hebrew under the same approved decision.
        found = tuple(consonants(word["text"]) for word in oshb[source_ref]
                      if word["kind"] != "ketiv")
        if found != supply.words:
            raise ValueError(
                f"{scope.ref} supply for {supply.target_ref} recorded "
                f"{list(supply.words)} at {source_ref}, but the pinned WLC now reads "
                f"{list(found)}; re-approve the supply before building"
            )
        verse_tokens = [
            token for token in parse_macula_hebrew(
                root / "data/vendor/macula-hebrew/WLC/lowfat"
                / source_scope.macula_hebrew_lowfat_file_for(source_chapter),
                source_chapter, book_code, source_book.macula_code,
            ) if token["ref"] == source_ref
        ]
        checks.extend(validate_hebrew_surface(verse_tokens, oshb))
        oshb_words += len(oshb[source_ref])
        verse_variants = parse_oshb_ketiv_qere(oshb, book_code)
        apparatus_units += len(ketiv_qere_group_anchors(oshb[source_ref]))
        for position, token in enumerate(verse_tokens, 1):
            moved = dict(token)
            moved["source_ref"] = source_ref
            moved["supplied_from"] = source_ref
            moved["ref"] = supply.target_ref
            moved["id"] = f"WLC.{supply.target_ref}.{position:03d}"
            tokens.append(moved)
        for unit in verse_variants:
            moved = dict(unit)
            moved["source_ref"] = source_ref
            moved["supplied_from"] = source_ref
            moved["ref"] = supply.target_ref
            variants.append(moved)
    return {
        "tokens": tokens,
        "variants": variants,
        "checks": checks,
        "oshb_words": oshb_words,
        "apparatus_units": apparatus_units,
    }


def build_hebrew_chapter(ref: str = "GEN.1", root: Path = ROOT) -> dict[str, Any]:
    """Normalize one Old Testament chapter into the same packet shape.

    The source side differs from the New Testament in what plays each role:
    MACULA Hebrew supplies the morpheme tokenization and enrichment where
    MACULA Greek supplied words, OSHB supplies the base-text validation that
    the official SBLGNT edition supplied, and the ketiv/qere pairs recorded in
    OSHB supply the closed textual candidates that the SBLGNT apparatus
    supplied.  Everything downstream of the packet is deliberately identical.
    """
    scope = chapter_scope(ref)
    book = scope.book
    with (root / "config/sources.lock.json").open(encoding="utf-8") as handle:
        source_lock = json.load(handle)["sources"]
    normalized = root / f"data/normalized/{scope.slug}"
    output = root / f"data/output/{scope.slug}"
    asv, notes = parse_asv_usx(
        root / f"data/vendor/asv/usx/{book.usx_file}", scope.chapter, book.code
    )
    # Almost always one chapter of Hebrew, and occasionally two: an ASV chapter
    # can be filled in part by the Hebrew chapter before or after it, and the
    # table says so before any of this is read.
    hebrew: list[dict[str, Any]] = []
    oshb: dict[str, list[dict[str, Any]]] = {}
    for source_chapter in hebrew_source_chapters(root, scope):
        hebrew.extend(parse_macula_hebrew(
            root / "data/vendor/macula-hebrew/WLC/lowfat"
            / scope.macula_hebrew_lowfat_file_for(source_chapter),
            source_chapter, book.code, book.macula_code,
        ))
        oshb.update(parse_oshb_words(
            root / f"data/vendor/oshb/wlc/{book.oshb_file}", source_chapter, book.code
        ))

    # Everything above is in Hebrew verse numbering. Everything below is in
    # the ASV's.
    versification = hebrew_chapter_versification(
        root, scope, asv, {
            verse_number(token["ref"]) for token in hebrew
            if chapter_number(token["ref"]) == scope.chapter
        }
    )

    def belongs(ref_: str, word: int | None) -> bool:
        """Whether a record read above is this ASV chapter's to consider.

        Its own Hebrew chapter is kept entire, including the segments it goes
        on to drop, which are counted and reported rather than filtered away.
        A neighbouring chapter is kept only where it fills a verse here.
        """
        source_chapter, source_verse = chapter_number(ref_), verse_number(ref_)
        if source_chapter == scope.chapter:
            return True
        return versification.locate(source_verse, word, source_chapter) is not None

    hebrew = [token for token in hebrew if belongs(token["ref"], token["oshb_word"])]
    oshb = {
        oshb_ref: kept for oshb_ref, kept in (
            (oshb_ref, [word for word in words if belongs(oshb_ref, word["index"])])
            for oshb_ref, words in oshb.items()
        ) if kept
    }
    hebrew_checks = validate_hebrew_surface(hebrew, oshb)
    variants = parse_oshb_ketiv_qere(oshb, book.code)

    if versification.title_in_first_verse:
        asv, notes = merge_title_into_first_verse(asv, notes, book.code, scope.chapter)
    # These checks run while everything is still in Hebrew numbering, which is
    # the frame the recorded division and the moved segments are stated in.
    verify_verse_divisions(hebrew, versification, scope.ref)
    excluded = excluded_source_tokens(hebrew, versification, scope.ref)
    incoming = incoming_source_tokens(hebrew, versification, scope.ref)
    hebrew = restate_in_target_versification(
        hebrew, versification, book.code, scope.chapter, prefix="WLC"
    )
    variants = restate_in_target_versification(
        variants, versification, book.code, scope.chapter
    )
    # An ASV verse the pinned edition does not contain is resolved by a
    # recorded decision, not by silence: either a parallel verse of the same
    # edition supplies it, or the chapter states that nothing does. Both are
    # read here, in the ASV's frame, and merged in verse order so that the
    # supplied verse sits where it is read.
    supplied = load_supplied_verses(
        root / "config" / scope.unsourced_verses_name, scope.ref
    )
    unresolved = sorted(set(versification.unsourced) - set(supplied))
    if unresolved:
        raise ValueError(
            f"{scope.ref} has ASV verse(s) {unresolved} that the pinned WLC does "
            f"not contain, and config/{scope.unsourced_verses_name} does not "
            "resolve them; record whether a parallel verse supplies each one"
        )
    stray = sorted(set(supplied) - set(versification.unsourced))
    if stray:
        raise ValueError(
            f"config/{scope.unsourced_verses_name} resolves ASV verse(s) {stray}, "
            "which the pinned WLC does contain; a verse with Hebrew of its own is "
            "never supplied from elsewhere"
        )
    supply = supplied_verse_sources(root, scope, supplied)
    hebrew = sorted(
        hebrew + supply["tokens"], key=lambda token: verse_number(token["ref"])
    )
    variants = sorted(
        variants + supply["variants"], key=lambda unit: verse_number(unit["ref"])
    )
    hebrew_checks.extend(supply["checks"])
    for position, unit in enumerate(variants, 1):
        unit["id"] = f"WLC.{unit['ref']}.v{position:03d}"
    jsonl_write(normalized / "asv-target-tokens.jsonl", asv)
    jsonl_write(normalized / "asv-notes.jsonl", notes)
    jsonl_write(normalized / "source-tokens.jsonl", hebrew)
    jsonl_write(normalized / "source-variants.jsonl", variants)

    hebrew_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    variants_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    notes_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token in hebrew:
        hebrew_by_ref[token["ref"]].append(token)
    for unit in variants:
        variants_by_ref[unit["ref"]].append(unit)
    for note in notes:
        notes_by_ref[note["ref"]].append(note)
    # A packet's source_ref names what it actually holds. `1KI.18.33+34a` is a
    # whole Hebrew verse followed by the first part of the next; `ISA.63.19a` is
    # a part whose remainder Isaiah 64 aligns. For an ordinary verse it is the
    # Hebrew number, which is not always the ASV's.
    def packet_source_ref(target_verse: int) -> str | None:
        """What the packet holds: this chapter's Hebrew, or the verse supplying it."""
        supply = supplied.get(target_verse)
        if supply is not None:
            return supply.source_ref
        return versification.source_ref(target_verse)

    packets = [{
        "ref": verse["ref"],
        "source_ref": packet_source_ref(verse_number(verse["ref"])),
        "source_edition": "WLC",
        "target_edition": "ASV",
        "source_tokens": hebrew_by_ref[verse["ref"]],
        "target_tokens": verse["tokens"],
        "variant_units": variants_by_ref[verse["ref"]],
        "target_notes": notes_by_ref[verse["ref"]],
    } for verse in asv]
    jsonl_write(output / "alignment-input.jsonl", packets)

    # A note group is the unit, not a word: neither side of a ketiv/qere pair is
    # necessarily one word (1 Kings 17:15 has two of each), and a ketiv velo
    # qere has no qere side at all. Each group is scoped by the word it anchors
    # on, which is the side the packet carries and the side
    # ``restate_in_target_versification`` moves across a verse division.
    apparatus_units = sum(
        1
        for oshb_ref, words in oshb.items()
        for anchor in ketiv_qere_group_anchors(words).values()
        if versification.locate(
            verse_number(oshb_ref), anchor, chapter_number(oshb_ref)
        ) is not None
    ) + supply["apparatus_units"]
    summary = {
        "chapter": scope.ref,
        "testament": scope.testament,
        "source_edition": "WLC",
        "status": "ready_for_alignment",
        "counts": {
            "asv_verses": len(asv),
            "asv_tokens": sum(len(v["tokens"]) for v in asv),
            "asv_notes": len(notes),
            "source_verses": len({t["ref"] for t in hebrew}),
            "source_tokens": len(hebrew),
            "source_variant_units": len(variants),
            "reference_apparatus_units": apparatus_units,
            "divided_verses": len(versification.divisions),
            "merged_verses": sum(
                1 for group in versification.by_target.values() if len(group) > 1
            ),
            "dropped_segments": len(versification.dropped),
            # What the pinned edition does not contain, after the chapter's
            # recorded resolutions: a supplied verse has Hebrew and is counted
            # as sourced, one resolved the other way still has none.
            "supplied_verses": sum(
                1 for supply in supplied.values() if supply.source_ref is not None
            ),
            "unsourced_verses": sum(
                1 for supply in supplied.values() if supply.source_ref is None
            ),
            "excluded_source_tokens": len(excluded),
            "incoming_segments": len(versification.incoming),
            "incoming_source_tokens": len(incoming),
            "oshb_words": sum(len(words) for words in oshb.values()) + supply["oshb_words"],
            "macula_words": sum(check["macula_words"] for check in hebrew_checks),
        },
        "checks": {
            "source_tokens_match_reference_edition": all(
                check["word_match"] for check in hebrew_checks
            ),
            "source_token_mismatches": [
                check["ref"] for check in hebrew_checks if not check["word_match"]
            ],
            "macula_word_mismatch_detail": [
                {"ref": check["ref"], "mismatches": check["mismatches"]}
                for check in hebrew_checks if not check["word_match"]
            ],
            "variant_units_match_reference_apparatus": len(variants) == apparatus_units,
            "excluded_source_token_ids": [token["id"] for token in excluded],
            "incoming_source_token_ids": [token["id"] for token in incoming],
        },
        "versification": {
            **versification.summary(),
            "supplied": [
                supplied[verse].summary() for verse in sorted(supplied)
                if supplied[verse].source_ref is not None
            ],
        },
    }
    # Reading the lock proves the fetched tree is the pinned one rather than a
    # stray checkout, exactly as the Greek build does.
    for key in ("macula_hebrew", "oshb"):
        if key not in source_lock:
            raise ValueError(f"config/sources.lock.json has no {key} entry")
    output.mkdir(parents=True, exist_ok=True)
    (output / "build-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def build_chapter(ref: str = "JHN.1", root: Path = ROOT) -> dict[str, Any]:
    """Normalize a chapter from whichever original-language sources it needs."""
    scope = chapter_scope(ref)
    if scope.is_hebrew:
        return build_hebrew_chapter(scope.ref, root)
    return build_greek_chapter(scope.ref, root)


def validate_summary(summary: dict[str, Any]) -> None:
    counts = summary["counts"]
    checks = summary["checks"]
    edition = summary["source_edition"]
    failures = []
    expected = counts["asv_verses"]
    if expected < 1:
        failures.append("ASV chapter contains no verses")
    # Every ASV verse has source text but the ones the pinned edition does not
    # contain, which the versification names rather than counts up from a
    # shortfall: the ASV's Nehemiah 7:68 is absent from the Leningrad Codex.
    sourced = expected - counts["unsourced_verses"]
    if counts["source_verses"] != sourced:
        failures.append(f"source_verses: expected {sourced}, got {counts['source_verses']}")
    if not checks["source_tokens_match_reference_edition"]:
        failures.append(
            f"MACULA tokens do not match the {edition} reference edition: "
            f"{checks['source_token_mismatches']}"
        )
    if not checks["variant_units_match_reference_apparatus"]:
        failures.append(
            f"variation units do not match the {edition} apparatus record: "
            f"expected {counts.get('reference_apparatus_units')}, "
            f"got {counts['source_variant_units']}"
        )
    if failures:
        raise ValueError("; ".join(failures))


def hebrew_lowfat_files(ref: str, root: Path = ROOT) -> list[str]:
    """Every MACULA Hebrew chapter file this chapter has to be built from.

    Usually one. It is a separate command from ``book-env`` because it reads
    the versification table, which lives in the OSHB checkout: the fetch script
    has to have pulled that down before it can ask.
    """
    scope = chapter_scope(ref)
    if not scope.is_hebrew:
        raise ValueError(f"{scope.ref} is not an Old Testament chapter")
    own = [
        scope.macula_hebrew_lowfat_file_for(source_chapter)
        for source_chapter in hebrew_source_chapters(root, scope)
    ]
    # A verse the pinned edition does not contain is supplied from a parallel
    # in another book, so that book's chapter is fetched too.
    return own + [
        lowfat for lowfat in supplied_verse_files(root, scope)["lowfat"]
        if lowfat not in own
    ]


def hebrew_oshb_files(ref: str, root: Path = ROOT) -> list[str]:
    """Every OSHB book file this chapter has to be built from.

    Its own, and the book a supplied verse is read from. Unlike the lowfat
    list this needs nothing that has been fetched yet — only the chapter's
    recorded decision — which is why the fetch script can ask for it before
    it has checked anything out.
    """
    scope = chapter_scope(ref)
    if not scope.is_hebrew:
        raise ValueError(f"{scope.ref} is not an Old Testament chapter")
    own = [scope.book.oshb_file]
    return own + [
        oshb_file for oshb_file in supplied_verse_files(root, scope)["oshb"]
        if oshb_file not in own
    ]


def book_environment(ref: str) -> str:
    """Emit the per-book fetch coordinates as shell assignments.

    Both testaments' variables are always emitted, empty where they do not
    apply, so the fetch script can branch on ``TESTAMENT`` without testing
    whether each name is set. Values are quoted because book names such as
    ``1 Samuel`` contain spaces.
    """
    scope = chapter_scope(ref)
    book = scope.book
    values = {
        "TESTAMENT": book.testament,
        "BOOK_CODE": book.code,
        "BOOK_NAME": book.name,
        "BOOK_USX_FILE": book.usx_file,
        "BOOK_LOWFAT_FILE": book.macula_lowfat_file or "",
        "BOOK_SBLGNT_NAME": book.sblgnt_name or "",
        "BOOK_HEBREW_LOWFAT_FILE": (
            scope.macula_hebrew_lowfat_file if scope.is_hebrew else ""
        ),
        "BOOK_OSHB_FILE": book.oshb_file or "",
        "CHAPTER_NUMBER": str(scope.chapter),
        "CHAPTER_SLUG": scope.slug,
    }
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the alignment-research test slice")
    parser.add_argument(
        "command",
        choices=[
            "build", "check", "book-env",
            "hebrew-lowfat-files", "hebrew-oshb-files",
        ],
    )
    parser.add_argument("chapter")
    args = parser.parse_args(argv)
    if args.command == "book-env":
        try:
            print(book_environment(args.chapter))
        except ValueError as error:
            parser.error(str(error))
        return 0
    if args.command == "hebrew-lowfat-files":
        try:
            print("\n".join(hebrew_lowfat_files(args.chapter)))
        except ValueError as error:
            parser.error(str(error))
        return 0
    if args.command == "hebrew-oshb-files":
        try:
            print("\n".join(hebrew_oshb_files(args.chapter)))
        except ValueError as error:
            parser.error(str(error))
        return 0
    try:
        summary = build_chapter(args.chapter)
    except ValueError as error:
        parser.error(str(error))
    validate_summary(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0

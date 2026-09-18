from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

from .alignment import load_jsonl, prepare_alignment
from .pipeline import ROOT, WORD_RE, display_surface, display_transliteration
from .versification import TITLE_VERSE
from .scope import chapter_scope


def footnote_source_attributes(packet: dict[str, Any]) -> dict[str, str]:
    provenance = packet.get("source_provenance", {})
    attributes = {"x-source-citation": packet["source_citation"]}
    if provenance.get("projection_label"):
        attributes["x-source-projection"] = provenance["projection_label"]
    if provenance.get("limitations"):
        attributes["x-projection-limitations"] = " ".join(provenance["limitations"])
    return attributes


def chapter_note_elements(document: ET.Element, chapter: str) -> dict[str, ET.Element]:
    """Identify notes with the same host/ordinal convention as ASV preparation."""
    scope = chapter_scope(chapter)
    active_chapter = None
    verse = None
    counts: dict[str, int] = defaultdict(int)
    result = {}
    deferred: list[ET.Element] = []

    def add(note: ET.Element) -> None:
        if active_chapter == scope.chapter and verse is not None:
            ref = f"{scope.ref}.{verse}"
            counts[ref] += 1
            result[f"{ref}.n{counts[ref]:02d}"] = note

    def walk(element: ET.Element) -> None:
        nonlocal active_chapter, verse
        if element.tag == "chapter":
            if element.get("number"):
                active_chapter = int(element.get("number"))
                verse = None
            return
        if element.tag == "verse":
            if element.get("number"):
                verse = int(re.match(r"\d+", element.get("number")).group())
                for note in deferred:
                    add(note)
                deferred.clear()
            return
        if element.tag == "note":
            add(element)
            return
        title = element.tag == "para" and element.get("style") == "d" and verse is None
        if title:
            verse = TITLE_VERSE
        children = list(element)
        for index, child in enumerate(children):
            if (child.tag == "note" and index + 1 < len(children)
                    and children[index + 1].tag == "verse"
                    and children[index + 1].get("number")
                    and not (child.tail or "").strip()):
                deferred.append(child)
            else:
                walk(child)
        if title:
            verse = None

    walk(document)
    return result


def annotate_footnote(note: ET.Element, bundle: dict[str, Any]) -> None:
    """Wrap only the selected quotation tokens, retaining all mixed content."""
    from .usx3 import strip_alignment

    packet, record = bundle["packet"], bundle["record"]
    # Consolidation may read a chapter projection that already carries this
    # supplement. Remove its wrappers before reapplying the fresh ledger.
    strip_alignment(note)
    slots = []
    offset = 0

    def collect(element: ET.Element) -> None:
        nonlocal offset
        if element.text:
            slots.append((element, None, "text", offset, element.text))
            offset += len(element.text)
        for child in element:
            collect(child)
            if child.tail:
                slots.append((child, element, "tail", offset, child.tail))
                offset += len(child.tail)

    collect(note)
    raw = "".join(slot[4] for slot in slots)
    # Packet offsets refer to whitespace-normalized note text. Keep a map to
    # the original characters rather than normalizing the XML itself.
    normalized = []
    raw_positions = []
    for match in re.finditer(r"\S+", raw):
        if normalized:
            normalized.append(" ")
            raw_positions.append(match.start() - 1)
        normalized.extend(match.group())
        raw_positions.extend(range(match.start(), match.end()))
    if "".join(normalized) != packet["note_text"]:
        raise ValueError(f"{packet['ref']}: published ASV note text mismatch")
    source_by_id = {token["project_id"]: token for token in packet["source_tokens"]}
    attributes = annotation_attributes(record, source_by_id)
    spans = []
    for target in packet["target_tokens"]:
        start = raw_positions[target["note_start"]]
        end = raw_positions[target["note_end"] - 1] + 1
        if raw[start:end] != target["text"]:
            raise ValueError(f"{packet['ref']}: quotation token mismatch")
        attrs = dict(attributes[target["id"]])
        attrs["x-id"] = f"{packet['ref']}.{target['id']}"
        spans.append((start, end, attrs))
    consumed = 0
    for node, parent, kind, offset, value in slots:
        selected = [(a-offset, b-offset, attrs) for a, b, attrs in spans
                    if offset <= a and b <= offset + len(value)]
        if not selected:
            continue
        prefix = value[:selected[0][0]]
        wrappers = []
        for index, (start, end, attrs) in enumerate(selected):
            wrapper = ET.Element("char", attrs)
            wrapper.text = value[start:end]
            next_start = selected[index+1][0] if index+1 < len(selected) else len(value)
            wrapper.tail = value[end:next_start]
            wrappers.append(wrapper)
        consumed += len(wrappers)
        if kind == "text":
            node.text = prefix
            for index, wrapper in enumerate(wrappers):
                node.insert(index, wrapper)
        else:
            node.tail = prefix
            insertion = list(parent).index(node) + 1
            for index, wrapper in enumerate(wrappers):
                parent.insert(insertion + index, wrapper)
    if consumed != len(spans):
        raise ValueError(f"{packet['ref']}: quotation token crosses an XML boundary")
    note.set("x-footnote-id", packet["ref"])
    note.set("x-host-ref", packet["host_ref"])
    note.attrib.update(footnote_source_attributes(packet))
    omitted = [source_by_id[sid] for unit in record["alignments"]
               if unit["status"] == "unrepresented" for sid in unit["source"]]
    if omitted:
        note.set("x-unrepresented-source", " ".join(display_surface(token) for token in omitted))
        note.set("x-unrepresented-translit", " ".join(display_transliteration(token) for token in omitted))
        note.set("x-unrepresented-src", " ".join(token["project_id"] for token in omitted))


def annotate_footnotes(root: Path, document: ET.Element, chapter: str) -> None:
    # Supplementary note alignments are an internal, optional extension. The
    # focused process release deliberately omits that workflow; ordinary
    # chapter projection therefore has no supplementary bundles to annotate.
    try:
        from .footnotes import load_published_footnotes
    except ModuleNotFoundError as error:
        if error.name != f"{__package__}.footnotes":
            raise
        return

    bundles = load_published_footnotes(root, chapter)
    if not bundles:
        return
    notes = chapter_note_elements(document, chapter)
    for bundle in bundles:
        note_id = bundle["packet"]["note_id"]
        if note_id not in notes:
            raise ValueError(f"missing ASV note {note_id} in chapter projection")
        annotate_footnote(notes[note_id], bundle)


def annotation_attributes(
    record: dict[str, Any], source_by_id: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    result = {}
    for unit in record["alignments"]:
        display_target = unit["target_head"] or (unit["target"][0] if unit["target"] else None)
        source_surface = " ".join(
            display_surface(source_by_id[source_id]) for source_id in unit["source"]
        )
        # One romanized word per source token, in the same order as
        # `x-source`, so a consumer can pair them up and mark the source head.
        source_translit = " ".join(
            display_transliteration(source_by_id[source_id]) for source_id in unit["source"]
        )
        for target_id in unit["target"]:
            attributes = {
                "style": "w",
                "x-id": target_id,
                "x-align": unit["id"],
            }
            if unit["status"] != "aligned":
                attributes["x-status"] = unit["status"]
            if target_id == display_target:
                # Keep the rendered source text in the finished USX so review
                # tools do not need normalized or preparation intermediates.
                if unit["source"]:
                    attributes["x-src"] = " ".join(unit["source"])
                attributes["x-source"] = source_surface or "∅"
                if source_translit:
                    # Display only: a reader who cannot read the source script
                    # still needs to know what the aligned word sounds like.
                    attributes["x-translit"] = source_translit
                if unit["source_head"]:
                    attributes["srcloc"] = unit["source_head"]
            if unit["target_head"] == target_id:
                attributes["x-head"] = "true"
                if unit["source_head"]:
                    head = source_by_id[unit["source_head"]]
                    if head.get("lemma"):
                        attributes["lemma"] = head["lemma"]
                    if head.get("strong"):
                        attributes["strong"] = head["strong"]
            result[target_id] = attributes
    return result


def generate_aligned_usx(root: Path = ROOT, chapter: str = "JHN.1") -> Path:
    scope = chapter_scope(chapter)
    packets = prepare_alignment(root, scope.ref)
    records = load_jsonl(root / f"data/alignments/{scope.slug}.jsonl")
    records_by_ref = {record["ref"]: record for record in records}
    source = load_jsonl(root / f"data/output/{scope.slug}/projected-source-tokens.jsonl")
    source_by_id = {token["id"]: token for token in source}
    target_by_ref = {
        packet["ref"]: {token["id"]: token for token in packet["target_tokens"]}
        for packet in packets
    }
    attrs_by_ref = {
        ref: annotation_attributes(record, source_by_id)
        for ref, record in records_by_ref.items()
    }

    input_path = root / f"data/vendor/asv/usx/{scope.book.usx_file}"
    tree = ET.parse(input_path)
    document = tree.getroot()

    # The converters downstream read the finished USX alone, so it has to say
    # which language its source side is and which way that language runs.
    language = scope.source_language
    for element in document.iter("book"):
        element.set("x-source-language", language["code"])
        element.set("x-source-name", language["name"])
        element.set("x-source-dir", language["direction"])

    # Source-only units have no target word on which to hang their display
    # text. Store their IDs and surfaces on the verse milestone so a consumer
    # of the finished USX can still expose them during review.
    unrepresented_by_ref: dict[str, list[str]] = defaultdict(list)
    unrepresented_translit_by_ref: dict[str, list[str]] = defaultdict(list)
    unrepresented_ids_by_ref: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for unit in record["alignments"]:
            if unit["status"] == "unrepresented":
                unrepresented_by_ref[record["ref"]].extend(
                    display_surface(source_by_id[source_id]) for source_id in unit["source"]
                )
                unrepresented_translit_by_ref[record["ref"]].extend(
                    display_transliteration(source_by_id[source_id])
                    for source_id in unit["source"]
                )
                unrepresented_ids_by_ref[record["ref"]].extend(unit["source"])
    # Where a verse's source is not written in this book at all. The pinned
    # edition has no Nehemiah 7:68, and the chapter supplies it from the
    # parallel at Ezra 2:66; a finished USX that said nothing would present
    # another verse's Hebrew as this one's.
    supplied_by_ref = {
        packet["ref"]: sorted({
            token["supplied_from"] for token in packet["source_tokens"]
            if token.get("supplied_from")
        })
        for packet in packets
    }
    # A chapter has a verse 0 only where the MT numbers its superscription
    # separately. Where it writes the superscription into the verse it numbers
    # 1, the title paragraph carries that verse's opening words instead.
    title_verse = TITLE_VERSE if f"{scope.ref}.{TITLE_VERSE}" in target_by_ref else 1
    active_chapter: int | None = None
    seen_verse = False
    for element in document.iter():
        if element.tag == "chapter":
            active_chapter = int(element.attrib["number"])
            seen_verse = False
            continue
        if active_chapter != scope.chapter:
            continue
        # A descriptive title has no verse milestone to carry its metadata, so
        # the title paragraph itself carries verse 0's.
        if element.tag == "para" and element.attrib.get("style") == "d" and not seen_verse:
            if title_verse != TITLE_VERSE:
                # The verse milestone below already carries this verse's
                # source-only units; naming them twice would double them.
                continue
            ref = f"{scope.ref}.{TITLE_VERSE}"
        elif element.tag == "verse":
            seen_verse = True
            ref = f"{scope.ref}.{element.attrib['number'].split('-', 1)[0]}"
        else:
            continue
        if supplied_by_ref.get(ref):
            element.set("x-supplied-from", " ".join(supplied_by_ref[ref]))
        if unrepresented_by_ref[ref]:
            element.set("x-unrepresented-source", " ".join(unrepresented_by_ref[ref]))
            element.set(
                "x-unrepresented-translit",
                " ".join(unrepresented_translit_by_ref[ref]),
            )
            element.set("x-unrepresented-src", " ".join(unrepresented_ids_by_ref[ref]))

    state: dict[str, int | None] = {"chapter": None, "verse": None}
    positions: dict[str, int] = defaultdict(int)
    text_nodes: list[tuple[str, ET.Element, ET.Element | None, str, str]] = []

    def active_ref() -> str | None:
        if state["chapter"] == scope.chapter and state["verse"] is not None:
            return f"{scope.ref}.{state['verse']}"
        return None

    def collect(element: ET.Element, parent: ET.Element | None = None) -> None:
        if element.tag == "chapter":
            state["chapter"] = int(element.attrib["number"])
            state["verse"] = None
            return
        if element.tag == "verse":
            state["verse"] = int(element.attrib["number"].split("-", 1)[0])
            return
        if element.tag == "note":
            return
        title = (
            element.tag == "para"
            and element.attrib.get("style") == "d"
            and state["chapter"] == scope.chapter
            and state["verse"] is None
        )
        if title:
            state["verse"] = title_verse
        ref = active_ref()
        if ref and element.text:
            text_nodes.append(("text", element, parent, ref, element.text))
        for child in list(element):
            collect(child, element)
            ref = active_ref()
            if ref and child.tail:
                text_nodes.append(("tail", child, element, ref, child.tail))
        if title:
            state["verse"] = None

    collect(document)

    def wrappers_for(ref: str, value: str) -> tuple[str, list[ET.Element]]:
        matches = list(WORD_RE.finditer(value))
        if not matches:
            return value, []
        prefix = value[: matches[0].start()]
        wrappers = []
        for index, match in enumerate(matches):
            positions[ref] += 1
            target_id = f"t{positions[ref]:03d}"
            expected = target_by_ref[ref][target_id]["text"]
            if match.group() != expected:
                raise ValueError(
                    f"USX token mismatch {ref} {target_id}: {match.group()!r} != {expected!r}"
                )
            wrapper = ET.Element("char", attrs_by_ref[ref][target_id])
            wrapper.text = match.group()
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            wrapper.tail = value[match.end() : next_start]
            wrappers.append(wrapper)
        return prefix, wrappers

    for kind, node, parent, ref, original in text_nodes:
        prefix, wrappers = wrappers_for(ref, original)
        if not wrappers:
            continue
        if kind == "text":
            node.text = prefix
            for index, wrapper in enumerate(wrappers):
                node.insert(index, wrapper)
        else:
            if parent is None:
                raise ValueError("tail node has no parent")
            node.tail = prefix
            insertion = list(parent).index(node) + 1
            for offset, wrapper in enumerate(wrappers):
                parent.insert(insertion + offset, wrapper)

    for ref, targets in target_by_ref.items():
        if positions[ref] != len(targets):
            raise ValueError(f"USX annotation count mismatch for {ref}")
    annotate_footnotes(root, document, scope.ref)
    output = root / f"data/output/{scope.slug}/asv-aligned/{scope.book.usx_file}"
    output.parent.mkdir(parents=True, exist_ok=True)
    # USX is mixed-content XML. ``ET.indent`` inserts whitespace into elements
    # such as notes and character spans, changing their text when reparsed.
    # Preserve the parsed text/tail values and let ElementTree serialize the
    # document without cosmetic indentation.
    write_usx(document, output)
    return output


def write_usx(document: ET.Element, output: Path) -> Path:
    """Serialize USX with a conventional, stable XML declaration."""
    output.parent.mkdir(parents=True, exist_ok=True)
    body = ET.tostring(document, encoding="utf-8", short_empty_elements=True)
    output.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>\n' + body)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project a chapter alignment into ASV USX")
    parser.add_argument("chapter")
    args = parser.parse_args(argv)
    path = generate_aligned_usx(ROOT, args.chapter)
    root = ET.parse(path).getroot()
    words = [element for element in root.iter("char") if element.attrib.get("style") == "w"]
    print(json.dumps({"output": str(path), "annotated_tokens": len(words)}, indent=2))
    return 0

"""USX 3.0 milestones for the published projections.

The vendor ASV is USX 2.0, where a verse is an empty ``<verse number>``
marker inside the paragraph it opens and nothing says where that verse
stops. USX 3.0 replaces it with a start/end milestone pair carrying
``sid``/``eid``. This module performs that conversion, and it places the
pair so that a verse encloses everything a reader needs to render it on
its own: the paragraph break that opens it, any section heading or blank
line that introduces it, and any trailing structure before the next verse
begins.

A verse milestone stays inside block markup, as the schema requires, so the
enclosure is expressed by moving the milestone to the front of the first
block of the verse — the section heading or blank line where there is one,
otherwise the paragraph the verse opens — and to the end of the last. The
start milestone carries the verse number, so it always precedes the point
where that number was written in the source.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable

USX_VERSION = "3.0"

# Blocks that introduce the verse after them rather than continue the verse
# before them: the blank line, section and major-section headings, the
# reference and acrostic lines, and the Psalm's descriptive title.
_HEADING_STYLES = frozenset({
    "b", "cd", "cl", "d", "mr", "pb", "qa", "qd", "r", "sp", "sr",
})
_HEADING_PATTERN = re.compile(r"m?s\d?$")


def chapter_id(book: str, chapter: str | int) -> str:
    return f"{book} {chapter}"


def verse_id(book: str, chapter: str | int, verse: str) -> str:
    return f"{book} {chapter}:{verse}"


def book_code(document: ET.Element) -> str:
    for element in document:
        if element.tag == "book":
            return element.attrib["code"]
    raise ValueError("USX document has no book element")


def introduces_next_verse(block: ET.Element) -> bool:
    """Whether a verse start may be hoisted back over this block.

    A block that carries its own ``x-ref`` is a verse in its own right —
    the original-language projection writes a numbered superscription that
    way — and never belongs to the verse that follows it.
    """
    if block.tag != "para" or "x-ref" in block.attrib:
        return False
    style = block.attrib.get("style", "")
    return style in _HEADING_STYLES or bool(_HEADING_PATTERN.match(style))


def _opens_paragraph(para: ET.Element, position: int) -> bool:
    """Whether nothing before this verse marker belongs to an earlier verse."""
    if (para.text or "").strip():
        return False
    for child in list(para)[:position]:
        # A footnote before the verse marker annotates the verse it opens.
        if child.tag != "note" or (child.tail or "").strip():
            return False
    return True


def _detach(para: ET.Element, marker: ET.Element) -> None:
    """Remove a verse marker, leaving the paragraph's text where it was."""
    position = list(para).index(marker)
    text = marker.tail or ""
    if position == 0:
        para.text = (para.text or "") + text
    else:
        previous = para[position - 1]
        previous.tail = (previous.tail or "") + text
    para.remove(marker)


def _move_to_front(block: ET.Element, para: ET.Element, marker: ET.Element) -> None:
    """Put a verse start first in its block, ahead of everything it renders."""
    if block.tag != "para":
        raise ValueError(f"a verse cannot start inside <{block.tag}>")
    _detach(para, marker)
    # Text before the first child renders before it, so it becomes the tail
    # of the milestone that now stands in front of it.
    marker.tail = block.text
    block.text = None
    block.insert(0, marker)


def indent_blocks(document: ET.Element, indent: str = "\t") -> ET.Element:
    """Put each root-level block on its own line, as the ASV source USX does.

    Only the root's mixed content is touched: whitespace between block
    elements is insignificant, whereas whitespace inside a para is the text.
    """
    document.text = f"\n{indent}"
    for element in document:
        element.tail = f"\n{indent}"
    return document


def _chapter_number(open_chapter: str | None) -> str:
    if open_chapter is None:
        raise ValueError("a verse appears before the first chapter")
    return open_chapter.split(" ", 1)[1]


def add_milestones(document: ET.Element) -> ET.Element:
    """Rewrite USX 2.0 chapter and verse markers as USX 3.0 milestones."""
    book = book_code(document)
    blocks = list(document)
    end = len(blocks)

    # Chapter markers and verse markers, in document order. A verse marker
    # is ordered by where it stands now, not by where its milestone goes.
    events: list[tuple[tuple[int, int], ET.Element, ET.Element | None]] = []
    for index, block in enumerate(blocks):
        if block.tag == "chapter":
            events.append(((index, -1), block, None))
        elif block.tag == "para":
            for position, child in enumerate(block):
                if child.tag == "verse":
                    events.append(((index, position), child, block))
    events.sort(key=lambda event: event[0])

    # Planned edits, applied once the whole document has been read so that
    # no insertion moves a marker out from under a position recorded above.
    chapter_ends: dict[int, ET.Element] = {}
    inline_inserts: list[tuple[ET.Element, ET.Element, ET.Element]] = []
    block_ends: list[tuple[ET.Element, ET.Element]] = []
    moves: list[tuple[ET.Element, ET.Element, ET.Element]] = []

    open_verse: str | None = None
    open_chapter: str | None = None

    def close_verse(
        last_block: ET.Element | None = None,
        before: tuple[ET.Element, ET.Element] | None = None,
    ) -> None:
        """End the open verse before a verse marker, or after its last block."""
        nonlocal open_verse
        if open_verse is None:
            return
        closer = ET.Element("verse", {"eid": open_verse})
        if before is not None:
            inline_inserts.append((*before, closer))
        elif last_block is not None and last_block.tag == "para":
            block_ends.append((last_block, closer))
        else:
            raise ValueError(f"verse {open_verse} ends outside a paragraph")
        open_verse = None

    for (index, position), marker, para in events:
        if para is None:
            close_verse(blocks[index - 1])
            if open_chapter is not None:
                chapter_ends[index] = ET.Element("chapter", {"eid": open_chapter})
            open_chapter = chapter_id(book, marker.attrib["number"])
            marker.set("sid", open_chapter)
            continue
        identifier = verse_id(
            book, _chapter_number(open_chapter), marker.attrib["number"]
        )
        if _opens_paragraph(para, position):
            # The verse owns the paragraph it opens, and any heading or
            # blank line standing between it and the verse before it. The
            # milestone moves to the front of the first of those blocks.
            first = index
            while first > 0 and introduces_next_verse(blocks[first - 1]):
                first -= 1
            close_verse(blocks[first - 1])
            moves.append((blocks[first], para, marker))
        else:
            close_verse(before=(para, marker))
        marker.set("sid", identifier)
        open_verse = identifier

    close_verse(blocks[-1])
    if open_chapter is not None:
        chapter_ends[end] = ET.Element("chapter", {"eid": open_chapter})

    for para, marker, closer in inline_inserts:
        para.insert(list(para).index(marker), closer)
    for block, closer in block_ends:
        block.append(closer)
    for block, para, marker in moves:
        _move_to_front(block, para, marker)

    if chapter_ends:
        rebuilt: list[ET.Element] = []
        for index, block in enumerate(blocks):
            if index in chapter_ends:
                rebuilt.append(chapter_ends[index])
            rebuilt.append(block)
        if end in chapter_ends:
            rebuilt.append(chapter_ends[end])
        for block in blocks:
            document.remove(block)
        document.extend(rebuilt)

    document.set("version", USX_VERSION)
    return indent_blocks(document)


def strip_alignment(document: ET.Element) -> ET.Element:
    """Drop every alignment annotation, leaving the ASV text as published USX."""
    for element in document.iter():
        for name in [name for name in element.attrib if name.startswith("x-")]:
            del element.attrib[name]
    _unwrap_words(document)
    return document


def _unwrap_words(parent: ET.Element) -> None:
    for child in list(parent):
        _unwrap_words(child)
    index = 0
    while index < len(parent):
        child = parent[index]
        if child.tag == "char" and child.attrib.get("style") == "w":
            if len(child):
                raise ValueError("an alignment word wrapper has element children")
            text = (child.text or "") + (child.tail or "")
            if index == 0:
                parent.text = (parent.text or "") + text
            else:
                previous = parent[index - 1]
                previous.tail = (previous.tail or "") + text
            parent.remove(child)
        else:
            index += 1


def block_text(element: ET.Element) -> str:
    """Every character of text a block renders, in document order."""
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        parts.append(node.text or "")
        for child in node:
            walk(child)
            parts.append(child.tail or "")

    walk(element)
    return "".join(parts)


def chapter_texts(document: ET.Element) -> dict[str, list[tuple[str, str]]]:
    """Per chapter, the style and rendered text of each paragraph block.

    Root-level verse milestones are structure, not text, so their tails are
    the whitespace between blocks and are ignored; a milestone still inside
    a paragraph contributes its tail like any other node.
    """
    result: dict[str, list[tuple[str, str]]] = {}
    active: str | None = None
    for element in document:
        if element.tag == "chapter":
            if "sid" in element.attrib or "eid" not in element.attrib:
                active = element.attrib["number"]
                result.setdefault(active, [])
            continue
        if element.tag in {"book", "verse"}:
            continue
        if active is not None:
            result[active].append((element.attrib.get("style", ""), block_text(element)))
    return result


def chapter_verses(document: ET.Element) -> dict[str, list[str]]:
    """Per chapter, the verse numbers in order."""
    result: dict[str, list[str]] = {}
    active: str | None = None
    for element in document.iter():
        if element.tag == "chapter":
            if "eid" in element.attrib and "sid" not in element.attrib:
                continue
            active = element.attrib["number"]
            result.setdefault(active, [])
        elif element.tag == "verse" and "number" in element.attrib and active:
            result[active].append(element.attrib["number"])
    return result


def verify_milestones(document: ET.Element) -> int:
    """Every milestone is paired, in order, and never nested."""
    open_verse: str | None = None
    open_chapter: str | None = None
    closed = 0
    for element in document.iter():
        if element.tag == "chapter":
            if "eid" in element.attrib:
                if open_verse is not None:
                    raise ValueError(f"chapter {element.attrib['eid']} ends inside {open_verse}")
                if open_chapter != element.attrib["eid"]:
                    raise ValueError(f"unbalanced chapter end {element.attrib['eid']}")
                open_chapter = None
            else:
                if open_chapter is not None:
                    raise ValueError(f"chapter {element.attrib['sid']} starts inside {open_chapter}")
                open_chapter = element.attrib["sid"]
        elif element.tag == "verse":
            if "eid" in element.attrib:
                if open_verse != element.attrib["eid"]:
                    raise ValueError(f"unbalanced verse end {element.attrib['eid']}")
                open_verse = None
                closed += 1
            else:
                if open_verse is not None:
                    raise ValueError(f"verse {element.attrib['sid']} starts inside {open_verse}")
                if open_chapter is None:
                    raise ValueError(f"verse {element.attrib['sid']} is outside a chapter")
                open_verse = element.attrib["sid"]
    if open_verse is not None or open_chapter is not None:
        raise ValueError("a document ends with an unclosed milestone")
    return closed


def verify_english_text(
    source: ET.Element, published: ET.Element, label: str, chapters: Iterable[str]
) -> int:
    """The published English is the source ASV text, block for block."""
    expected = chapter_texts(source)
    actual = chapter_texts(published)
    blocks = 0
    for chapter in chapters:
        if chapter not in actual:
            raise ValueError(f"{label}: chapter {chapter} is missing")
        if actual[chapter] != expected[chapter]:
            for index, (was, now) in enumerate(zip(expected[chapter], actual[chapter])):
                if was != now:
                    raise ValueError(
                        f"{label}: chapter {chapter} block {index} differs: "
                        f"{was!r} != {now!r}"
                    )
            raise ValueError(
                f"{label}: chapter {chapter} has {len(actual[chapter])} blocks, "
                f"expected {len(expected[chapter])}"
            )
        expected_verses = chapter_verses(source)[chapter]
        if chapter_verses(published)[chapter] != expected_verses:
            raise ValueError(f"{label}: chapter {chapter} verse numbering differs")
        blocks += len(expected[chapter])
    return blocks

from __future__ import annotations

import argparse
import html
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .alignment import compiled_chapters
from .pipeline import ROOT
from .scope import ChapterScope, chapter_scope
from .usx import generate_aligned_usx


# Every page lives in one directory, beside an index, because these pages are
# read and restyled as a set. A chapter's other build products stay under
# `data/output/<CHAPTER>/`, which is where its inputs are.
SITE_DIRECTORY = "data/output/reverse-interlinear"


@dataclass
class LineBreak:
    """A poetic line boundary inside a verse.

    In the poetic books one verse spans several `q` paragraphs, and the
    parallelism between those lines is most of what the verse is saying.
    Flattening them into a paragraph would throw that away, so the boundaries
    are carried through to the rendered page.
    """
    style: str


@dataclass
class Verse:
    ref: str
    number: str
    parts: list[str | dict[str, str] | LineBreak] = field(default_factory=list)
    unrepresented_source: str | None = None
    unrepresented_transliteration: str | None = None
    unrepresented_ids: str | None = None
    # Where this verse's source is written, when the pinned edition does not
    # contain the verse and the chapter supplies it from a parallel.
    supplied_from: str | None = None
    line_style: str | None = None
    source_citation: str | None = None
    source_projection: str | None = None
    source_limitations: str | None = None


def read_aligned_usx(path: Path) -> tuple[str, dict[str, str], list[Verse]]:
    """Read only annotations embedded in a finished aligned USX document."""
    document = ET.parse(path).getroot()
    book = next((element for element in document.iter("book")), None)
    book_code = book.attrib.get("code", "UNK") if book is not None else "UNK"
    title = "Reverse interlinear"
    if book is not None and book.text:
        title = f"{book.text.strip()} — reverse interlinear"
    # The source language travels in the USX itself, so this converter still
    # needs nothing but the finished document.
    attributes = book.attrib if book is not None else {}
    language = {
        "code": attributes.get("x-source-language", "grc"),
        "name": attributes.get("x-source-name", "Greek"),
        "direction": attributes.get("x-source-dir", "ltr"),
    }

    chapter: str | None = None
    current: Verse | None = None
    verses: list[Verse] = []
    # A poetic line boundary only becomes a break once the verse has something
    # on the previous line, so it is held until the next content arrives.
    pending_line: str | None = None
    paragraph_style: str | None = None

    def take_line_break() -> None:
        nonlocal pending_line
        if pending_line is None or current is None:
            return
        style, pending_line = pending_line, None
        if any(not isinstance(part, str) or part.strip() for part in current.parts):
            current.parts.append(LineBreak(style))

    def append_text(value: str | None) -> None:
        if current is not None and value:
            if value.strip():
                take_line_break()
            current.parts.append(value)

    def walk(element: ET.Element, inherited_italic: bool = False) -> None:
        nonlocal chapter, current, pending_line, paragraph_style
        if element.tag == "chapter":
            chapter = element.attrib.get("number")
            current = None
            pending_line = None
            return
        if element.tag == "verse":
            number = element.attrib.get("number")
            if chapter and number:
                ref = f"{book_code}.{chapter}.{number.split('-', 1)[0]}"
                current = Verse(
                    ref=ref,
                    number=number,
                    unrepresented_source=element.attrib.get("x-unrepresented-source"),
                    unrepresented_transliteration=element.attrib.get(
                        "x-unrepresented-translit"
                    ),
                    unrepresented_ids=element.attrib.get("x-unrepresented-src"),
                    supplied_from=element.attrib.get("x-supplied-from"),
                    line_style=paragraph_style,
                )
                verses.append(current)
                # A verse that opens a line starts that line rather than
                # breaking one.
                pending_line = None
            return
        if element.tag == "note":
            note_id = element.get("x-footnote-id")
            if note_id:
                previous, previous_line = current, pending_line
                current = Verse(
                    ref=note_id,
                    number=f"{element.get('x-host-ref', '')} footnote",
                    unrepresented_source=element.get("x-unrepresented-source"),
                    unrepresented_transliteration=element.get("x-unrepresented-translit"),
                    unrepresented_ids=element.get("x-unrepresented-src"),
                    source_citation=element.get("x-source-citation"),
                    source_projection=element.get("x-source-projection"),
                    source_limitations=element.get("x-projection-limitations"),
                )
                verses.append(current)
                pending_line = None
                append_text(element.text)
                for child in element:
                    walk(child, inherited_italic)
                    append_text(child.tail)
                current, pending_line = previous, previous_line
            return
        if element.tag == "para":
            style = element.attrib.get("style", "")
            paragraph_style = style
            if style.startswith("q"):
                pending_line = style
            if style == "d" and chapter and current is None:
                # The descriptive title is verse 0: real translated text that
                # the ASV does not number.
                current = Verse(
                    ref=f"{book_code}.{chapter}.0",
                    number="title",
                    unrepresented_source=element.attrib.get("x-unrepresented-source"),
                    unrepresented_transliteration=element.attrib.get(
                        "x-unrepresented-translit"
                    ),
                    unrepresented_ids=element.attrib.get("x-unrepresented-src"),
                    line_style=style,
                )
                verses.append(current)
                pending_line = None
        original_italic = inherited_italic or (
            element.tag == "char" and element.attrib.get("style") in {"add", "it"}
        )
        if element.tag == "char" and element.attrib.get("style") == "w":
            if current is not None:
                take_line_break()
                token = dict(element.attrib)
                token["text"] = "".join(element.itertext())
                if original_italic:
                    token["original-italic"] = "true"
                current.parts.append(token)
            return

        append_text(element.text)
        for child in element:
            walk(child, original_italic)
            append_text(child.tail)

    walk(document)
    return title, language, [
        verse for verse in verses if any(isinstance(part, dict) for part in verse.parts)
    ]


def alignment_contexts(verse: Verse) -> dict[str, dict[str, Any]]:
    """Reconstruct alignment phrases from compact per-word USX attributes."""
    contexts: dict[str, dict[str, Any]] = {}
    last_part_by_alignment: dict[str, int] = {}
    for part_index, part in enumerate(verse.parts):
        if not isinstance(part, dict):
            continue
        alignment_id = part.get("x-align", "")
        context = contexts.setdefault(alignment_id, {
            "target_words": [],
            "target_ids": [],
            "target_head": None,
            "source": None,
            "transliteration": None,
            "source_ids": [],
            "source_head": None,
        })
        previous_part = last_part_by_alignment.get(alignment_id)
        separator = " "
        if previous_part is not None and part_index == previous_part + 2:
            candidate = verse.parts[previous_part + 1]
            if isinstance(candidate, str) and is_hyphen(candidate):
                separator = candidate
        context["target_phrase"] = (
            f'{context.get("target_phrase", "")}{separator}{part["text"]}'
            if context["target_words"]
            else part["text"]
        )
        context["target_words"].append(part["text"])
        context["target_ids"].append(part.get("x-id", ""))
        last_part_by_alignment[alignment_id] = part_index
        if part.get("x-head") == "true":
            context["target_head"] = part.get("x-id")
        if "x-source" in part:
            context["source"] = part["x-source"]
            context["transliteration"] = part.get("x-translit")
            context["source_ids"] = part.get("x-src", "").split()
            context["source_head"] = part.get("srcloc")
    return contexts


def is_hyphen(value: str) -> bool:
    """Whether text between lexical tokens joins them into one written word."""
    return value in {"-", "\u2010", "\u2011"}


def romanized_source(context: dict[str, Any]) -> str:
    """The source phrase as this page shows it: romanized, never in its script.

    The page is read by people who cannot read Hebrew or Greek letters, so the
    romanization is the source line rather than a gloss beneath it. A unit with
    no source at all still shows ∅.
    """
    return context.get("transliteration") or "∅"


def source_with_head_markup(context: dict[str, Any]) -> str:
    source = romanized_source(context)
    words = source.split()
    source_ids = context.get("source_ids", [])
    source_head = context.get("source_head")
    if len(words) < 2 or len(words) != len(source_ids) or source_head not in source_ids:
        return html.escape(source)
    head_index = source_ids.index(source_head)
    pieces = []
    for index, word in enumerate(words):
        head_class = ' class="source-head"' if index == head_index else ""
        pieces.append(f"<span{head_class}>{html.escape(word)}</span>")
    return " ".join(pieces)


def target_head_arrow(token: dict[str, str], context: dict[str, Any]) -> str | None:
    target_ids = context["target_ids"]
    target_head = context.get("target_head")
    target_id = token.get("x-id")
    if len(target_ids) < 2 or target_head not in target_ids or target_id == target_head:
        return None
    return "→" if target_ids.index(target_id) < target_ids.index(target_head) else "←"


def render_token(
    token: dict[str, str], context: dict[str, Any], language: dict[str, str]
) -> str:
    escaped_text = html.escape(token["text"])
    text = f"<em>{escaped_text}</em>" if token.get("original-italic") == "true" else escaped_text
    target_id = html.escape(token.get("x-id", ""), quote=True)
    alignment_id = html.escape(token.get("x-align", ""), quote=True)
    source_ids = html.escape(token.get("x-src", ""), quote=True)
    status = token.get("x-status", "aligned")
    classes = ["token"]
    if status != "aligned":
        classes.append(status)
    if token.get("original-italic") == "true":
        classes.append("original-italic")
    target_phrase = context["target_phrase"]
    source_phrase = romanized_source(context)
    tooltip = html.escape(
        f"English: {target_phrase}; {language['name']} (romanized): {source_phrase}; "
        f"alignment: {token.get('x-align', '')}",
        quote=True,
    )
    english = html.escape(target_phrase, quote=True)
    source_phrase_attribute = html.escape(source_phrase, quote=True)
    attributes = (
        f'class="{" ".join(classes)}" data-target-id="{target_id}" '
        f'data-alignment-id="{alignment_id}" data-source-ids="{source_ids}" '
        f'data-english="{english}" data-source="{source_phrase_attribute}" '
        f'title="{tooltip}"'
    )
    source = token.get("x-source")
    if source is not None:
        source_text = source_with_head_markup(context)
        # The romanization is the source line, so it carries the language tag
        # of the source written in Latin letters, and it always runs left to
        # right however the source itself is written.
        marks = f' lang="{html.escape(language["code"], quote=True)}-Latn" dir="ltr"'
        return (
            f"<ruby {attributes}>{text}"
            f'<rt{marks}><span class="translit">{source_text}</span></rt>'
            f"</ruby>"
        )
    arrow = target_head_arrow(token, context)
    if arrow is not None:
        return f'<ruby {attributes}>{text}<rt class="target-arrow">{arrow}</rt></ruby>'
    return f"<span {attributes}>{text}</span>"


def render_line(
    parts: list[str | dict[str, str]],
    contexts: dict[str, dict[str, Any]],
    language: dict[str, str],
) -> str:
    """Render one prose or poetic line without splitting hyphenated words."""
    rendered: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if not isinstance(part, dict):
            rendered.append(html.escape(part))
            index += 1
            continue

        compound = [render_token(part, contexts[part.get("x-align", "")], language)]
        end = index
        while (
            end + 2 < len(parts)
            and isinstance(parts[end + 1], str)
            and is_hyphen(parts[end + 1])
            and isinstance(parts[end + 2], dict)
        ):
            compound.append(html.escape(parts[end + 1]))
            next_token = parts[end + 2]
            compound.append(
                render_token(
                    next_token,
                    contexts[next_token.get("x-align", "")],
                    language,
                )
            )
            end += 2
        members = [parts[position] for position in range(index, end + 1, 2)]
        if end > index and all(
            member.get("x-align", "") == members[0].get("x-align", "")
            for member in members
        ):
            context = contexts[members[0].get("x-align", "")]
            head = context.get("target_head")
            anchor = next(
                (member for member in members if member.get("x-id") == head),
                members[0],
            ).copy()
            anchor["text"] = "".join(
                (parts[index + offset - 1] if offset else "") + member["text"]
                for offset, member in zip(range(0, end - index + 1, 2), members)
            )
            if not all(member.get("original-italic") == "true" for member in members):
                anchor.pop("original-italic", None)
            content = render_token(anchor, context, language)
        else:
            content = "".join(compound)
        rendered.append(
            f'<span class="hyphenated-word">{content}</span>'
            if end > index
            else content
        )
        index = end + 1
    return "".join(rendered)


def render_html(
    title: str,
    verses: list[Verse],
    language: dict[str, str],
    index_href: str | None = None,
) -> str:
    source_name = html.escape(language["name"])
    source_code = html.escape(language["code"], quote=True)
    verse_html = []
    for verse in verses:
        contexts = alignment_contexts(verse)
        # One entry per poetic line. Prose verses produce exactly one, so their
        # markup is unchanged.
        lines: list[tuple[str, list[str | dict[str, str]]]] = [
            (verse.line_style or "", [])
        ]
        for part in verse.parts:
            if isinstance(part, LineBreak):
                lines.append((part.style, []))
            elif isinstance(part, dict):
                lines[-1][1].append(part)
            else:
                lines[-1][1].append(part)
        rendered_lines = []
        for style, pieces in lines:
            content = render_line(pieces, contexts, language).strip()
            if content:
                rendered_lines.append((style, content))
        supplied = ""
        if verse.supplied_from:
            # The one place the page shows source that is not written in the
            # verse it stands under. A reader is owed that plainly.
            supplied = (
                f'<div class="supplied">'
                f'<span>{source_name} supplied from:</span> '
                f'{html.escape(verse.supplied_from)} — the pinned edition does '
                f'not contain this verse, which the ASV\'s underlying text has'
                f'</div>'
            )
        omissions = ""
        source_note = ""
        if verse.source_citation:
            source_note = '<div class="source-note">' + html.escape(" — ".join(
                text for text in (verse.source_citation, verse.source_projection,
                                  verse.source_limitations) if text
            )) + '</div>'
        if verse.unrepresented_source:
            source = html.escape(
                verse.unrepresented_transliteration or verse.unrepresented_source
            )
            ids = html.escape(verse.unrepresented_ids or "", quote=True)
            omissions = (
                f'<div class="unrepresented" data-source-ids="{ids}">'
                f'<span>Unrepresented {source_name}:</span> '
                f'<bdi lang="{source_code}-Latn" dir="ltr">{source}</bdi></div>'
            )
        body = "".join(
            f'<div class="interlinear{(" " + html.escape(style, quote=True)) if style.startswith("q") else ""}">'
            f'{content}</div>'
            for style, content in rendered_lines
        )
        verse_html.append(
            f'<section class="verse" id="{html.escape(verse.ref, quote=True)}">'
            f'<a class="verse-number" href="#{html.escape(verse.ref, quote=True)}">'
            f'{html.escape(verse.number)}</a>'
            f'{body}{supplied}{omissions}{source_note}</section>'
        )

    escaped_title = html.escape(title)
    # Only a page that sits beside an index links to one.
    navigation = (
        f'<p class="site-nav"><a href="{html.escape(index_href, quote=True)}">'
        "← All chapters</a></p>"
        if index_href else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    body {{ max-width: 78rem; margin: 2rem auto; padding: 0 1.25rem 4rem; line-height: 1.55; }}
    h1 {{ font-size: 1.55rem; margin-bottom: .35rem; }}
    .legend {{ color: #666; font: .9rem/1.4 system-ui, sans-serif; margin-bottom: 2rem; }}
    .site-nav {{ font: .85rem/1.4 system-ui, sans-serif; margin: 0 0 .6rem; }}
    .site-nav a {{ color: #245a9a; text-decoration: none; }}
    .verse {{ display: grid; grid-template-columns: 2.5rem 1fr; gap: .4rem .8rem; margin: 0 0 1.15rem; }}
    .verse-number {{ color: #777; font: 700 .85rem/1.7 system-ui, sans-serif; text-decoration: none; }}
    /* Each poetic line is its own row in the verse grid, so parallel lines
       stay parallel instead of reflowing into a paragraph. */
    .interlinear {{ grid-column: 2; font-size: 1.08rem; line-height: 2.85; }}
    .interlinear.q2 {{ padding-left: 1.75rem; }}
    .interlinear.q3 {{ padding-left: 3.5rem; }}
    .interlinear.q4 {{ padding-left: 5.25rem; }}
    /* Browsers hide an rt whose text exactly matches its ruby base when ruby
       annotations are separate. Greek transliterations can legitimately match
       their English targets (for example, the pronoun "me"), so keep them visible. */
    ruby {{ ruby-position: under; ruby-align: center; ruby-merge: merge; margin: 0 .04em; }}
    .hyphenated-word {{ display: inline-block; white-space: nowrap; }}
    rt {{ color: #245a9a; font: .76rem/1.15 "Noto Sans", system-ui, sans-serif; }}
    /* The source line is romanized, so it needs no script font and never
       reorders the English around it. */
    rt .translit {{ display: block; unicode-bidi: isolate; }}
    .target-arrow {{ color: #8b4cb8; font-size: .95rem; font-weight: 900; }}
    .source-head {{ font-weight: 800; text-decoration: underline; text-decoration-thickness: .1em; }}
    .token.alignment-hover {{ background: color-mix(in srgb, #4d9de0 22%, transparent); border-radius: .2rem; outline: 1px solid color-mix(in srgb, #2878b8 55%, transparent); }}
    .token.alignment-hover rt {{ background: color-mix(in srgb, #4d9de0 14%, transparent); border-radius: .2rem; }}
    .token.added:not(.original-italic) {{ background: color-mix(in srgb, #d88 18%, transparent); border-radius: .2rem; }}
    .token.added:not(.original-italic) rt {{ color: #a23; }}
    .unrepresented {{ grid-column: 2; color: #8a4d00; font: .8rem/1.4 system-ui, sans-serif; margin-top: -.35rem; }}
    .unrepresented span {{ font-weight: 650; }}
    .supplied {{ grid-column: 2; color: #4a4a8a; font: .8rem/1.4 system-ui, sans-serif; margin-top: -.35rem; }}
    .supplied span {{ font-weight: 650; }}
    .alignment-inspector {{ position: fixed; z-index: 10; max-width: min(32rem, calc(100vw - 2rem)); padding: .45rem .65rem; border: 1px solid #7778; border-radius: .35rem; background: Canvas; color: CanvasText; box-shadow: 0 .2rem .8rem #0003; font-size: .85rem; pointer-events: none; }}
    @media (prefers-color-scheme: dark) {{
      .legend, .verse-number {{ color: #aaa; }}
      rt, .site-nav a {{ color: #8fc1ff; }}
      .token.added:not(.original-italic) rt {{ color: #ffaaa0; }}
      .unrepresented {{ color: #f0bb75; }}
      .supplied {{ color: #a9b0f0; }}
    }}
  </style>
</head>
<body>
  {navigation}
  <h1>{escaped_title}</h1>
  <p class="legend">ASV is on top; the aligned {source_name} is underneath the English target head, romanized rather than in its own script. Arrows beneath the other English words point toward that head. In a multi-word {source_name} span, its source head is underlined. Non-italic English with ∅ is highlighted as potentially added; original ASV italics are preserved. The romanization is a reading aid only and never determines an alignment; the {source_name} itself is in the aligned USX. A bracketed form such as [ha] is a morpheme written only as pointing inside the word before it. Hover over any word to see its complete English–{source_name} alignment.</p>
  <div class="alignment-inspector" hidden></div>
  {''.join(verse_html)}
  <script>
    const tokens = [...document.querySelectorAll('.token')];
    const groups = new Map();
    const inspector = document.querySelector('.alignment-inspector');
    for (const token of tokens) {{
      const id = token.dataset.alignmentId;
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(token);
      token.addEventListener('pointerenter', event => {{
        for (const member of groups.get(id)) member.classList.add('alignment-hover');
        inspector.textContent = `English: ${{token.dataset.english}} · {source_name} (romanized): ${{token.dataset.source}}`;
        inspector.hidden = false;
        inspector.style.left = `${{Math.min(event.clientX + 14, window.innerWidth - inspector.offsetWidth - 12)}}px`;
        inspector.style.top = `${{Math.min(event.clientY + 18, window.innerHeight - inspector.offsetHeight - 12)}}px`;
      }});
      token.addEventListener('pointermove', event => {{
        inspector.style.left = `${{Math.min(event.clientX + 14, window.innerWidth - inspector.offsetWidth - 12)}}px`;
        inspector.style.top = `${{Math.min(event.clientY + 18, window.innerHeight - inspector.offsetHeight - 12)}}px`;
      }});
      token.addEventListener('pointerleave', () => {{
        for (const member of groups.get(id)) member.classList.remove('alignment-hover');
        inspector.hidden = true;
      }});
    }}
  </script>
</body>
</html>
"""


def render_index(pages: list[dict[str, Any]]) -> str:
    """A contents page for the directory of chapter pages."""
    rows = "".join(
        f'<li><a href="{html.escape(page["file"], quote=True)}">'
        f'{html.escape(page["title"])}</a>'
        f'<span class="counts">{page["verses"]} verses · {page["tokens"]} words · '
        f'{html.escape(page["source_name"])}</span></li>'
        for page in pages
    )
    chapters = len(pages)
    tokens = sum(page["tokens"] for page in pages)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ASV reverse interlinear</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    body {{ max-width: 48rem; margin: 2rem auto; padding: 0 1.25rem 4rem; line-height: 1.55; }}
    h1 {{ font-size: 1.55rem; margin-bottom: .35rem; }}
    .legend {{ color: #666; font: .9rem/1.4 system-ui, sans-serif; margin-bottom: 2rem; }}
    ul {{ list-style: none; margin: 0; padding: 0; }}
    li {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem 1rem;
          justify-content: space-between; padding: .5rem 0; border-bottom: 1px solid #7773; }}
    a {{ color: #245a9a; font-weight: 600; text-decoration: none; }}
    .counts {{ color: #777; font: .82rem/1.4 system-ui, sans-serif; }}
    @media (prefers-color-scheme: dark) {{
      .legend, .counts {{ color: #aaa; }}
      a {{ color: #8fc1ff; }}
    }}
  </style>
</head>
<body>
  <h1>ASV reverse interlinear</h1>
  <p class="legend">{chapters} aligned chapters, {tokens} aligned English words. Each page shows the ASV over its romanized original-language source. Built from the accepted alignment ledgers.</p>
  <ul>{rows}</ul>
</body>
</html>
"""


def aligned_usx_path(root: Path, scope: ChapterScope) -> Path:
    return root / f"data/output/{scope.slug}/asv-aligned/{scope.book.usx_file}"


def page_path(root: Path, scope: ChapterScope, directory: Path | None = None) -> Path:
    return (directory or root / SITE_DIRECTORY) / f"{scope.slug}.html"


def generate_reverse_interlinear(
    input_path: Path,
    output_path: Path,
    title: str | None = None,
    index_href: str | None = None,
) -> dict[str, Any]:
    document_title, language, verses = read_aligned_usx(input_path)
    if not verses:
        raise ValueError(f"no aligned word annotations found in {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_html(title or document_title, verses, language, index_href),
        encoding="utf-8",
    )
    return {
        "input": str(input_path),
        "output": str(output_path),
        "source_language": language["code"],
        "source_name": language["name"],
        "verses": len(verses),
        "tokens": sum(sum(isinstance(part, dict) for part in verse.parts) for verse in verses),
    }


def index_entry(root: Path, chapter: str) -> dict[str, Any]:
    """One index row, derived from a chapter's ledger rather than its page.

    Everything the index states about a chapter is already in the compiled
    ledger: how many verses it has, and how many English words the alignment
    accounts for. Reading it there means refreshing the index costs a file
    read per chapter instead of re-rendering every page, which is the
    difference between a step `finish` can always run and one that grows
    without bound as the project does.
    """
    scope = chapter_scope(chapter)
    rows = [
        json.loads(line)
        for line in (root / f"data/alignments/{scope.slug}.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    return {
        "chapter": scope.ref,
        "title": scope.chapter_name,
        "file": page_path(root, scope).name,
        "source_name": scope.source_language["name"],
        "verses": len(rows),
        # Every target token is partitioned into exactly one unit, so summing
        # the units' targets counts each aligned English word once.
        "tokens": sum(len(unit["target"]) for row in rows for unit in row["alignments"]),
    }


def build_index(
    root: Path = ROOT,
    chapters: list[str] | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    """Rewrite the index over every compiled chapter, rendering no pages."""
    chapters = chapters if chapters is not None else compiled_chapters(root)
    entries = [index_entry(root, chapter) for chapter in chapters]
    directory = directory or root / SITE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    index = directory / "index.html"
    index.write_text(render_index(entries), encoding="utf-8")
    return {
        "index": str(index),
        "chapters": [entry["chapter"] for entry in entries],
        "verses": sum(entry["verses"] for entry in entries),
        "tokens": sum(entry["tokens"] for entry in entries),
    }


def build_site(
    root: Path = ROOT,
    chapters: list[str] | None = None,
    rebuild_usx: bool = True,
    directory: Path | None = None,
) -> dict[str, Any]:
    """Rebuild the whole directory of pages, and the index over them.

    Every chapter with a compiled ledger is rebuilt from that ledger, so the
    set of pages is never a record of which chapters someone remembered to
    rerun. `rebuild_usx=False` renders from the aligned USX already on disk,
    which is what a change to this page's markup or styling needs. The links
    within the directory are relative, so it can be written anywhere.
    """
    chapters = chapters if chapters is not None else compiled_chapters(root)
    directory = directory or root / SITE_DIRECTORY
    pages = []
    for chapter in chapters:
        scope = chapter_scope(chapter)
        input_path = (
            generate_aligned_usx(root, chapter) if rebuild_usx
            else aligned_usx_path(root, scope)
        )
        output_path = page_path(root, scope, directory)
        summary = generate_reverse_interlinear(
            input_path,
            output_path,
            title=f"{scope.chapter_name} — ASV reverse interlinear",
            index_href="index.html",
        )
        pages.append({
            **summary,
            "chapter": scope.ref,
            "title": scope.chapter_name,
            "file": output_path.name,
        })
    index = build_index(root, chapters, directory)
    return {
        "directory": str(directory),
        "index": index["index"],
        "chapters": [page["chapter"] for page in pages],
        "verses": sum(page["verses"] for page in pages),
        "tokens": sum(page["tokens"] for page in pages),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render aligned USX as reverse-interlinear HTML")
    # No default: `--all` and `--index-only` need no chapter, and everything
    # else needs the one the caller meant rather than John 1.
    parser.add_argument("--chapter")
    parser.add_argument(
        "--all", action="store_true",
        help="rebuild every chapter with a compiled ledger, and the index",
    )
    parser.add_argument(
        "--pages-only", action="store_true",
        help="render from the aligned USX already on disk instead of rebuilding it",
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="rewrite the index from the compiled ledgers, rendering no pages",
    )
    parser.add_argument(
        "input", nargs="?", type=Path,
    )
    parser.add_argument(
        "output", nargs="?", type=Path,
    )
    args = parser.parse_args(argv)
    if args.index_only:
        print(json.dumps(build_index(ROOT), indent=2))
        return 0
    if args.all:
        print(json.dumps(build_site(ROOT, rebuild_usx=not args.pages_only), indent=2))
        return 0
    if not args.chapter:
        parser.error("a chapter is required unless --all or --index-only")
    scope = chapter_scope(args.chapter)
    input_path = args.input or aligned_usx_path(ROOT, scope)
    output_path = args.output or page_path(ROOT, scope)
    result = generate_reverse_interlinear(
        input_path,
        output_path,
        # An explicit input document names itself; only a chapter built from
        # this repository's layout is titled from the chapter, and only a page
        # written into the site directory has an index to link back to.
        title=(
            None if args.input
            else f"{scope.chapter_name} — ASV reverse interlinear"
        ),
        index_href=None if args.output else "index.html",
    )
    print(json.dumps(result, indent=2))
    return 0

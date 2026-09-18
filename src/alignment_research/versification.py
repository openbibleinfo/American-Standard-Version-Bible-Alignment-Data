"""Map Hebrew verse numbering onto the numbering the ASV uses.

The Hebrew Bible and the Western versification the ASV inherits from the KJV do
not agree about where verses begin. In the Psalms a superscription is verse 1
of the Hebrew and an unnumbered title in English, so everything after it is off
by one. Elsewhere whole runs of verses move across a chapter boundary: the
Hebrew Exodus 7:26 is English Exodus 8:1, and the Hebrew Joel 3 is English Joel
2:28-32.

None of that is worth rediscovering per chapter, and deriving it from verse
counts would be guesswork that happens to work in the Psalms. OSHB publishes a
catalogue of the differences as `wlc/VerseMap.xml`, in the same repository this
project already pins for the Hebrew text, so that table is the authority here.

The unit this module works in is the **segment**: a whole Hebrew verse, or one
of the two parts a Hebrew verse divides into. An ASV verse is filled by an
ordered list of segments, and every shape the table records is a statement
about segments:

* `Ps.13.6!a -> Ps.13.5` — a Hebrew verse divides, and each part becomes a
  whole ASV verse.
* `1Kgs.18.34!a -> 1Kgs.18.33!b` — a Hebrew verse divides, and its first part
  is the *tail* of the previous ASV verse. The part marker on the Hebrew side
  says the verse divides; the part marker on the ASV side only says where in
  its target the material sits, which is why it needs no decision.
* `1Kgs.22.44 -> 1Kgs.22.43!b` — no Hebrew part marker at all: two whole
  Hebrew verses merge into one ASV verse.
* `Isa.63.19!b -> Isa.64.1` — a Hebrew verse's material belongs to a *different*
  ASV chapter. Read from Isaiah 63 that segment is **dropped**: not this
  chapter's work, and named as such in the build summary. Read from Isaiah 64
  the same row is **incoming**: ASV 64:1 is filled by Hebrew that Isaiah 64 does
  not contain, so Isaiah 63's text is fetched alongside its own and the segment
  is aligned here. Every crossing row is one of those two things seen from one
  side or the other, so a chapter's source is not always a single Hebrew
  chapter, and `source_chapters` says which ones it is.

The table lists only the verses that differ, which leaves two traps. A Psalm
superscription has no English verse at all, so it is simply absent from the
table -- and treating an absent row as "unchanged" would map it onto the verse
that the following Hebrew verse legitimately claims. That collision is detected
rather than assumed, and it is only resolved as a title when the ASV chapter
actually has a descriptive title to hold it. The same collision in the *middle*
of a chapter is not a superscription but a merge stated only half of: the ASV's
1 Chronicles 12:4 is Hebrew 12:4 and 12:5 together, and the table says just
`1Chr.12.5 -> 1Chr.12.4`, leaving the other half unchanged. Where a chapter
opens is what separates the two. And where a verse divides, only
the part that *moves* is listed: the other part is unchanged and so implicit,
which is why the two parts of a division are derived here rather than read.

Not every superscription is numbered, though. The MT writes some of them into
the verse it numbers 1 rather than giving them a verse, so those chapters shift
by nothing and their ASV title has no Hebrew verse of its own. That is reported
as ``title_in_first_verse``, and the title's words are aligned in the packet
for the Hebrew verse that contains them.

One thing the table can only imply is a verse the pinned edition does not
contain at all. The ASV's Nehemiah 7:68 is in the KJV's Hebrew and not in the
Leningrad Codex, so the table maps WLC's verse 68 onto ASV 69 and leaves ASV 68
with no source. Such a verse is reported as ``unsourced``, and what fills it is
a decision recorded per chapter in `config/<CHAPTER>-unsourced-verses.json`:
either the parallel verse of the same edition that supplies it — Nehemiah 7:68
is supplied from Ezra 2:66 — or a statement that nothing does, in which case its
English is aligned as `added`. Every other unfilled ASV verse is a misread
mapping and fails.

Where a verse number can be derived, a division point cannot — it falls between
two words, and only reading both texts says which two. So the table is not
enough there, and neither is a guess: the division is a judgment, recorded per
chapter in `config/<CHAPTER>-verse-divisions.json` and reviewed like any other
textual decision. Until one is recorded and resolved, the chapter fails the
build. A division is recorded once, in the chapter whose Hebrew verse divides,
and a chapter receiving the other part reads that same file rather than
recording a second judgment that could disagree with it.

Everything this module concludes is checked against both texts before it is
used: the segments must cover exactly the verses the ASV chapter contains, each
target's segments must sit in the order the table's part markers claim, and a
recorded division must agree with the table about which two verses it produces.
A chapter it cannot map that way fails the build instead of producing a quietly
misaligned result.
"""
from __future__ import annotations

import json
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


VERSE_MAP_NS = "{http://www.APTBibleTools.com/namespace}"
# The ASV inherits the KJV's versification, which is the column this table
# targets. Where the two ever disagree, the per-chapter check below fails
# rather than silently trusting the label.
TARGET_COLUMN = "kjv"
SOURCE_COLUMN = "wlc"
# A descriptive title carries the Hebrew superscription, which the ASV does not
# number. It is aligned as verse 0 so that it is neither lost nor confused with
# a numbered verse.
TITLE_VERSE = 0
# The only verse parts the table uses, and the only ones modelled here.
PARTS = ("a", "b")


def consonants(value: str) -> str:
    """Strip pointing and accents so a recorded anchor can be checked.

    A division is recorded against the word it begins at, and that word is
    written differently in every edition and quoted differently by every
    reader. Comparing consonants only makes the check strict about the word
    and indifferent to how it was pointed.
    """
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(
        character for character in decomposed
        if not unicodedata.combining(character) and character.isalpha()
    )


def chapter_of(reference: str) -> str:
    """`PSA.13` from `PSA.13.6`."""
    return reference.rsplit(".", 1)[0]


def verse_of(reference: str) -> int:
    return int(reference.rsplit(".", 1)[1])


@dataclass(frozen=True)
class Segment:
    """One piece of Hebrew text, and the ASV verse it helps fill.

    ``part`` is ``None`` for a whole Hebrew verse, or ``"a"``/``"b"`` for the
    two pieces a divided verse produces. ``target_part`` is what the table says
    about *where in its target* the segment sits, and is purely ordering
    information: it needs no decision, unlike the division point itself.

    ``source_chapter`` is usually the chapter being built, and is carried
    explicitly because it is not always: an ASV chapter can be filled in part by
    Hebrew written in the chapter before or after it.
    """

    source_chapter: int
    source_verse: int
    part: str | None
    target_ref: str
    target_part: str | None = None

    @property
    def book(self) -> str:
        """The book code, which the target reference already carries."""
        return self.target_ref.split(".", 1)[0]

    @property
    def label(self) -> str:
        """`34a`, or `33` for a whole verse -- how a source_ref names it."""
        return f"{self.source_verse}{self.part or ''}"

    @property
    def reference(self) -> str:
        """`ISA.63.19b` -- the label qualified by the chapter it is written in."""
        return f"{self.book}.{self.source_chapter}.{self.label}"

    @property
    def key(self) -> tuple[int, int]:
        """The Hebrew verse this segment comes from, chapter and all."""
        return (self.source_chapter, self.source_verse)

    @property
    def order(self) -> tuple[int, int, int]:
        """Text order: a verse's second part follows its first."""
        return (self.source_chapter, self.source_verse, 1 if self.part == "b" else 0)

    def summary(self) -> dict[str, object]:
        return {
            "source": self.label,
            "target": self.target_ref,
            "target_part": self.target_part,
        }


@dataclass(frozen=True)
class VerseDivision:
    """Where one Hebrew verse's words stop filling one ASV verse and start another.

    `second_begins_at_word` is a 1-based OSHB word index within the Hebrew
    verse, which is the one ordinal every source stream already carries.
    `second_begins_with` is that word's consonantal form, kept so the build can
    prove the index still points at the word a reviewer approved rather than
    silently shifting when an edition is re-fetched.

    The two targets are full ASV references rather than bare verse numbers,
    because a part may land in the previous verse, the next one, or -- as at
    Isaiah 63:19 -- in the next chapter, which is not this chapter's work.
    """

    source_verse: int
    first_target: str
    second_target: str
    second_begins_at_word: int
    second_begins_with: str

    def target_for_word(self, word: int | None) -> str:
        if word is None or word < self.second_begins_at_word:
            return self.first_target
        return self.second_target

    def summary(self) -> dict[str, object]:
        return {
            "source_verse": self.source_verse,
            "targets": [self.first_target, self.second_target],
            "second_begins_at_word": self.second_begins_at_word,
            "second_begins_with": self.second_begins_with,
        }


@dataclass(frozen=True)
class ChapterVersification:
    """How one chapter's Hebrew text fills its ASV verses, segment by segment."""

    ref: str
    segments: tuple[Segment, ...]
    title_source_verses: tuple[int, ...]
    title_in_first_verse: bool = False
    divisions: dict[tuple[int, int], VerseDivision] = field(default_factory=dict)
    # ASV verses this chapter has no source text for at all. See
    # `chapter_versification`: the pinned edition does not contain them.
    unsourced: tuple[int, ...] = ()

    @cached_property
    def prefix(self) -> str:
        return f"{self.ref}."

    @cached_property
    def chapter(self) -> int:
        return verse_of(self.ref)

    @cached_property
    def kept(self) -> tuple[Segment, ...]:
        """Segments this chapter aligns, in text order."""
        return tuple(
            segment for segment in self.segments
            if segment.target_ref.startswith(self.prefix)
        )

    @cached_property
    def dropped(self) -> tuple[Segment, ...]:
        """Segments the table sends to another chapter, which this one does not align."""
        return tuple(
            segment for segment in self.segments
            if not segment.target_ref.startswith(self.prefix)
        )

    @cached_property
    def incoming(self) -> tuple[Segment, ...]:
        """Segments this chapter aligns that are written in another Hebrew chapter."""
        return tuple(
            segment for segment in self.kept if segment.source_chapter != self.chapter
        )

    @cached_property
    def source_chapters(self) -> tuple[int, ...]:
        """Every Hebrew chapter this ASV chapter's text is drawn from.

        Almost always just its own. Where it is not, the build has to fetch and
        parse the others too, so this is what the fetch script asks for.
        """
        return tuple(sorted({segment.source_chapter for segment in self.segments}))

    @cached_property
    def by_target(self) -> dict[int, tuple[Segment, ...]]:
        grouped: dict[int, list[Segment]] = defaultdict(list)
        for segment in self.kept:
            grouped[verse_of(segment.target_ref)].append(segment)
        return {
            target: tuple(sorted(group, key=lambda segment: segment.order))
            for target, group in sorted(grouped.items())
        }

    @cached_property
    def _whole_verse_targets(self) -> dict[tuple[int, int], str]:
        return {
            segment.key: segment.target_ref
            for segment in self.segments if segment.part is None
        }

    @property
    def source_to_target(self) -> dict[int, int]:
        """The undivided verses' mapping, for readers that want just that.

        A divided verse has no single answer here and is deliberately absent;
        ``locate`` is the complete question. So is a verse from another Hebrew
        chapter, which the summary reports as ``incoming`` instead: a bare verse
        number would be read as this chapter's, and it is not.
        """
        return {
            source_verse: verse_of(target)
            for (source_chapter, source_verse), target in self._whole_verse_targets.items()
            if source_chapter == self.chapter and target.startswith(self.prefix)
        }

    @property
    def is_identity(self) -> bool:
        return (
            not self.divisions
            and not self.dropped
            and not self.incoming
            and not self.unsourced
            and all(len(group) == 1 for group in self.by_target.values())
            and all(
                segment.source_verse == verse_of(segment.target_ref)
                for segment in self.kept
            )
        )

    def locate(
        self, source_verse: int, word: int | None = None,
        source_chapter: int | None = None,
    ) -> int | None:
        """The ASV verse this material fills, or ``None`` when it is not ours.

        `word` is only consulted for a divided verse, so every chapter without
        a division behaves exactly as before whether or not a caller supplies
        it. `source_chapter` defaults to the chapter being built, which is where
        all but a handful of verses come from. ``None`` means the table sends
        this segment to another chapter, which the per-chapter build drops.
        """
        if source_chapter is None:
            source_chapter = self.chapter
        key = (source_chapter, source_verse)
        division = self.divisions.get(key)
        if division is not None:
            target_ref = division.target_for_word(word)
        else:
            target_ref = self._whole_verse_targets.get(key)
            if target_ref is None:
                if source_chapter != self.chapter:
                    # Hebrew from a chapter this one borrows from, but not the
                    # part it borrows. The caller is filtering a whole chapter.
                    return None
                raise ValueError(
                    f"{self.ref} has no versification mapping for Hebrew verse {source_verse}"
                )
        if not target_ref.startswith(self.prefix):
            return None
        return verse_of(target_ref)

    def target(
        self, source_verse: int, word: int | None = None,
        source_chapter: int | None = None,
    ) -> int:
        """``locate``, refusing rather than returning material from elsewhere."""
        target = self.locate(source_verse, word, source_chapter)
        if target is None:
            raise ValueError(
                f"{self.ref} sends Hebrew verse {source_verse} "
                f"(word {word}) to another chapter"
            )
        return target

    def source_ref(self, target_verse: int) -> str | None:
        """What an ASV verse's packet actually holds, named honestly.

        `1KI.18.33+34a` is one whole Hebrew verse followed by the first part of
        the next; `ISA.63.19a` is a part whose remainder Isaiah 64 aligns. Where
        the material comes from another Hebrew chapter, each segment is named in
        full — `ISA.63.19b` — because the chapter is the whole point.

        ``None`` where the verse is unsourced: naming a source verse there
        would assert a Hebrew verse the pinned edition does not have.
        """
        segments = self.by_target.get(target_verse, ())
        if not segments:
            return None
        if any(segment.source_chapter != self.chapter for segment in segments):
            return "+".join(segment.reference for segment in segments)
        return f"{self.ref}." + "+".join(segment.label for segment in segments)

    def summary(self) -> dict[str, object]:
        return {
            "chapter": self.ref,
            "identical": self.is_identity,
            "title_source_verses": list(self.title_source_verses),
            "title_in_first_verse": self.title_in_first_verse,
            "shifted": {
                str(source): target
                for source, target in sorted(self.source_to_target.items())
                if source != target
            },
            "divisions": [
                # A division of another chapter's verse says so; one of this
                # chapter's own reads exactly as it always has.
                division.summary() if source_chapter == self.chapter
                else {**division.summary(), "source_chapter": source_chapter}
                for (source_chapter, _), division in sorted(self.divisions.items())
            ],
            "merged": [
                {"target": f"{self.ref}.{target}", "source_ref": self.source_ref(target)}
                for target, group in self.by_target.items() if len(group) > 1
            ],
            "dropped": [segment.summary() for segment in self.dropped],
            "unsourced": [f"{self.ref}.{verse}" for verse in self.unsourced],
            "incoming": [
                {
                    "source": segment.reference,
                    "target": segment.target_ref,
                    "target_part": segment.target_part,
                }
                for segment in self.incoming
            ],
        }


def parse_verse_map(path: Path, osis_book: str) -> list[dict[str, str]]:
    """Read one book's difference rows exactly as the table states them."""
    root = ET.parse(path).getroot()
    for book in root.iter(f"{VERSE_MAP_NS}book"):
        if book.attrib.get("osisID") != osis_book:
            continue
        return [dict(verse.attrib) for verse in book.iter(f"{VERSE_MAP_NS}verse")]
    return []


def _reference_parts(reference: str) -> tuple[str, int, int, str | None]:
    """Split `Ps.3.2` or `Ps.13.6!a` into book, chapter, verse, and part."""
    body, _, part = reference.partition("!")
    book, chapter, verse = body.rsplit(".", 2)
    return book, int(chapter), int(verse), part or None


def _target_reference(value: object, ref: str, field_name: str) -> str:
    """Read a recorded target as a full ASV reference.

    A bare number means a verse of this chapter, which is how the first
    recorded division (Psalm 13) was written and still reads. A dotted
    reference names any verse, including one in the next chapter.
    """
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{ref} verse division {field_name} is missing")
    if isinstance(value, int):
        return f"{ref}.{value}"
    text = str(value).strip()
    if text.isdigit():
        return f"{ref}.{int(text)}"
    pieces = text.rsplit(".", 2)
    if len(pieces) != 3 or not pieces[1].isdigit() or not pieces[2].isdigit():
        raise ValueError(
            f"{ref} verse division {field_name} {value!r} is neither a verse "
            "number in this chapter nor an ASV verse reference"
        )
    return f"{pieces[0]}.{int(pieces[1])}.{int(pieces[2])}"


def load_verse_divisions(path: Path, ref: str) -> dict[int, VerseDivision]:
    """Read a chapter's recorded divisions, refusing any still under review.

    An unresolved division is not a usable default. Dividing a verse in the
    wrong place produces a chapter that validates perfectly and is wrong, so
    the scaffold is deliberately unusable until a reviewer has agreed with it.
    """
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("chapter") != ref:
        raise ValueError(f"{path.name} records divisions for {document.get('chapter')}, not {ref}")
    divisions: dict[int, VerseDivision] = {}
    for entry in document.get("divisions", []):
        if entry.get("status") != "resolved":
            raise ValueError(
                f"{ref} verse division for Hebrew verse {entry.get('source_verse')} is "
                f"{entry.get('status')!r}; review {path.name} and mark it resolved"
            )
        missing = [
            key for key in
            ("source_verse", "first_target", "second_target",
             "second_begins_at_word", "second_begins_with")
            if entry.get(key) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"{ref} verse division is missing {missing}; complete {path.name}"
            )
        division = VerseDivision(
            source_verse=int(entry["source_verse"]),
            first_target=_target_reference(entry["first_target"], ref, "first_target"),
            second_target=_target_reference(entry["second_target"], ref, "second_target"),
            second_begins_at_word=int(entry["second_begins_at_word"]),
            second_begins_with=consonants(str(entry["second_begins_with"])),
        )
        if division.second_begins_at_word < 2:
            raise ValueError(
                f"{ref} verse division for Hebrew verse {division.source_verse} would "
                "leave the first verse empty; the second part cannot begin at word 1"
            )
        divisions[division.source_verse] = division
    return divisions


@dataclass(frozen=True)
class SuppliedVerse:
    """What fills an ASV verse the pinned source edition does not contain.

    ``source_ref`` names the verse the words are taken from — a full reference,
    because it is in another book: the ASV's Nehemiah 7:68 is supplied from the
    parallel muster list at Ezra 2:66, which the pinned WLC does contain and
    which the ASV renders in exactly the same English. ``None`` records the
    other resolution, that nothing in the pinned edition supplies the verse and
    its English is aligned as `added`.

    ``words`` is the supplying verse's consonantal text, word by word, kept for
    the same reason a division records the word it begins at: an edition is
    re-fetched, and a supply that quietly began pointing at different words
    would produce a chapter that validates perfectly and is wrong.
    """

    target_ref: str
    source_ref: str | None
    words: tuple[str, ...] = ()
    evidence: str = ""

    @property
    def target_verse(self) -> int:
        return verse_of(self.target_ref)

    def summary(self) -> dict[str, object]:
        return {"target": self.target_ref, "source": self.source_ref}


def load_supplied_verses(path: Path, ref: str) -> dict[int, SuppliedVerse]:
    """Read a chapter's resolutions for the verses its edition does not have.

    Refusing an unresolved entry for the same reason `load_verse_divisions`
    does: supplying the wrong verse, or supplying none where a parallel exists,
    is a judgment, and a scaffold is not one. A chapter with no such verse has
    no file, and `chapter_versification` is what says whether it needs one.
    """
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("chapter") != ref:
        raise ValueError(
            f"{path.name} records verses for {document.get('chapter')}, not {ref}"
        )
    supplied: dict[int, SuppliedVerse] = {}
    for entry in document.get("verses", []):
        target = _target_reference(entry.get("target"), ref, "target")
        if not target.startswith(f"{ref}."):
            raise ValueError(f"{path.name} records {target}, which is not in {ref}")
        if entry.get("status") != "resolved":
            raise ValueError(
                f"{ref} unsourced verse {target} is {entry.get('status')!r}; "
                f"review {path.name} and mark it resolved"
            )
        evidence = str(entry.get("evidence") or "").strip()
        if not evidence:
            raise ValueError(f"{ref} unsourced verse {target} records no evidence")
        resolution = entry.get("resolution")
        if resolution == "unsourced":
            supplied[verse_of(target)] = SuppliedVerse(target, None, evidence=evidence)
            continue
        if resolution != "supplied":
            raise ValueError(
                f"{ref} unsourced verse {target} has unknown resolution "
                f"{resolution!r}; it is 'supplied' or 'unsourced'"
            )
        source = str(entry.get("source") or "").strip()
        pieces = source.rsplit(".", 2)
        if len(pieces) != 3 or not pieces[1].isdigit() or not pieces[2].isdigit():
            raise ValueError(
                f"{ref} supply for {target} names source {source!r}, which is not "
                "a verse reference such as EZR.2.66"
            )
        words = tuple(
            consonants(str(word.get("text") or ""))
            for word in entry.get("source_words") or ()
        )
        if not words:
            raise ValueError(
                f"{ref} supply for {target} records no source_words; the words "
                "supplied are what a re-fetch is checked against"
            )
        supplied[verse_of(target)] = SuppliedVerse(target, source, words, evidence)
    return supplied


def _table_routes(
    path: Path, osis_book: str, chapter: int, ref: str
) -> dict[tuple[int, int, str | None], tuple[str, str | None]]:
    """Where the table sends each piece of the Hebrew text this chapter needs.

    Keyed by the Hebrew chapter, the verse, and the part of it the row names;
    valued by the full ASV reference and the part of *that* verse the material
    fills. Rows the table does not state are absent, and mean "unchanged"; the
    caller fills them in, because only it knows which Hebrew verses exist.

    Usually every key is this chapter's own. Where the table sends material
    *in* from a neighbouring Hebrew chapter, that whole chapter's rows are read
    too — not only the row that crosses — because a verse's other part is
    stated relative to its own chapter and would otherwise be derived wrongly.
    """
    book_code = chapter_of(ref)
    rows = []
    for row in parse_verse_map(path, osis_book):
        source = row.get(SOURCE_COLUMN, "")
        target = row.get(TARGET_COLUMN, "")
        if not source or not target:
            continue
        rows.append((source, target, _reference_parts(source), _reference_parts(target)))

    # Which Hebrew chapters fill this ASV chapter: its own, plus any the table
    # sends material in from. Hebrew Isaiah 63:19b is the ASV's Isaiah 64:1, so
    # building Isaiah 64 reads Isaiah 63 as well; Hebrew Joel 4 is the ASV's
    # Joel 3 in its entirety, and reads as one chapter of source for another.
    source_chapters = {chapter}
    for source, target, (source_book, source_chapter, _, _), (target_book, target_chapter, _, _) in rows:
        if target_book != osis_book or target_chapter != chapter:
            continue
        if source_book != osis_book:
            raise ValueError(
                f"{ref} is affected by a mapping from another book ({source} -> "
                f"{target}), which this build does not model"
            )
        source_chapters.add(source_chapter)

    routes: dict[tuple[int, int, str | None], tuple[str, str | None]] = {}
    for source, target, source_parts, target_parts in rows:
        source_book, source_chapter, source_verse, source_part = source_parts
        target_book, target_chapter, target_verse, target_part = target_parts
        if source_book != osis_book or source_chapter not in source_chapters:
            continue
        if target_book != source_book:
            raise ValueError(
                f"{ref} is affected by a mapping into another book ({source} -> "
                f"{target}), which this build does not model"
            )
        for reference, part in ((source, source_part), (target, target_part)):
            if part is not None and part not in PARTS:
                raise ValueError(
                    f"{ref} is affected by {reference}, which names a verse part "
                    f"other than {' or '.join(PARTS)}; that shape is not modelled"
                )
        key = (source_chapter, source_verse, source_part)
        if key in routes:
            raise ValueError(
                f"{ref} versification mapping is not one-to-one: {source} is mapped twice"
            )
        routes[key] = (f"{book_code}.{target_chapter}.{target_verse}", target_part)
    return routes


def _divided_from_routes(
    routes: dict[tuple[int, int, str | None], tuple[str, str | None]], ref: str,
) -> dict[tuple[int, int], dict[str, tuple[str, str | None]]]:
    """Which Hebrew verses divide, and where each of their two parts goes.

    The table lists only the part that moves; the other part is unchanged and
    therefore absent, so it is filled in as the verse's own ASV number. That is
    a derivation, not a judgment -- the judgment is *where* the verse divides,
    which lives in the recorded decision.
    """
    book_code = chapter_of(ref)
    divided: dict[tuple[int, int], dict[str, tuple[str, str | None]]] = defaultdict(dict)
    for (source_chapter, source_verse, part), route in routes.items():
        if part is not None:
            divided[(source_chapter, source_verse)][part] = route
    for (source_chapter, source_verse), parts in divided.items():
        if (source_chapter, source_verse, None) in routes:
            raise ValueError(
                f"{ref} maps Hebrew {source_chapter}:{source_verse} both whole and "
                "in parts; the versification table contradicts itself"
            )
        for part in PARTS:
            parts.setdefault(
                part, (f"{book_code}.{source_chapter}.{source_verse}", None)
            )
    return dict(sorted(divided.items()))


def divided_verses(
    path: Path, osis_book: str, chapter: int, ref: str
) -> dict[int, dict[str, tuple[str, str | None]]]:
    """The divisions this chapter needs a decision for, straight from the table.

    The scaffold and the build must agree about which verses divide and into
    what, so they read it here rather than each interpreting the table. Only
    this chapter's own verses are listed: a division is decided once, by the
    chapter whose Hebrew verse divides, and read from there by the chapter that
    receives the other part.
    """
    divided = _divided_from_routes(_table_routes(path, osis_book, chapter, ref), ref)
    return {
        source_verse: parts
        for (source_chapter, source_verse), parts in divided.items()
        if source_chapter == chapter
    }


def source_chapters(
    path: Path, osis_book: str, chapter: int, ref: str, *, has_own_chapter: bool
) -> tuple[int, ...]:
    """Every Hebrew chapter an ASV chapter's text is drawn from.

    Read from the table alone, because the fetch script needs it before there
    is any source text to build a full mapping from — except for the one thing
    the table cannot say. A chapter is normally drawn from its own number even
    when the table moves every verse of it elsewhere: the Hebrew Joel 3 is the
    ASV's Joel 2:28-32, and the ASV's Joel 3 still reads its own Hebrew chapter
    in order to report all five verses of it as dropped. But the ASV's Malachi
    4 has no Hebrew chapter of its own to read: Hebrew Malachi ends at chapter
    3, whose verses 19-24 are this chapter entire. Rows the table does not
    state mean "unchanged", so absence proves nothing there; the caller settles
    it from the Hebrew text, and passes the answer in.
    """
    routes = _table_routes(path, osis_book, chapter, ref)
    own = {chapter} if has_own_chapter else set()
    return tuple(sorted({source_chapter for source_chapter, _, _ in routes} | own))


def _unsourced_targets(
    routes: dict[tuple[int, int, str | None], tuple[str, str | None]],
    chapter: int,
    prefix: str,
    source_verses: set[int],
    unfilled: set[int],
) -> tuple[int, ...]:
    """Which unfilled ASV verses the pinned source edition simply does not have.

    An ASV verse no segment fills is nearly always a mapping this build has
    read wrongly, and stays an error. The one thing it can honestly be is a
    verse the ASV's underlying text contains and the pinned edition omits:
    Nehemiah 7:68, the horses and mules of the returning company, is in the
    KJV's Hebrew and not in the Leningrad Codex, so WLC's chapter has 72 verses
    where the ASV has 73.

    That is not guessed from the counts. The table records the omission in the
    only way it can, by mapping the Hebrew verse *numbered* 68 onto ASV 69 and
    every verse after it one further on, so the gap is exactly where a Hebrew
    verse of the same number exists and is sent to a later ASV verse. An
    unfilled verse the table does not account for that way — a chapter whose
    Hebrew failed to parse, a row read backwards — has nothing to say for
    itself and fails the build.
    """
    accounted = []
    for verse in sorted(unfilled):
        route = routes.get((chapter, verse, None))
        if verse not in source_verses or route is None:
            continue
        target_ref, _ = route
        if target_ref.startswith(prefix) and verse_of(target_ref) > verse:
            accounted.append(verse)
    return tuple(accounted)


def chapter_versification(
    path: Path,
    osis_book: str,
    chapter: int,
    ref: str,
    source_verses: set[int],
    target_verses: set[int],
    has_title: bool,
    divisions: dict[int, VerseDivision] | None = None,
    incoming_divisions: dict[int, dict[int, VerseDivision]] | None = None,
) -> ChapterVersification:
    """Resolve and verify one chapter's segments, or fail loudly.

    ``source_verses`` are this chapter's own Hebrew verse numbers, and
    ``target_verses`` the ASV verse numbers actually present, excluding the
    title. Both texts are consulted because the table alone cannot prove that
    it describes the chapter in front of us. Material arriving from another
    Hebrew chapter is named by the table row that sends it, so it needs no
    verse list of its own; ``incoming_divisions`` carries the division
    decisions those chapters recorded, keyed by chapter.
    """
    routes = _table_routes(path, osis_book, chapter, ref)
    divided = _divided_from_routes(routes, ref)
    recorded = {(chapter, verse): division for verse, division in (divisions or {}).items()}
    for source_chapter, chapter_divisions in (incoming_divisions or {}).items():
        for verse, division in chapter_divisions.items():
            recorded[(source_chapter, verse)] = division
    resolved_divisions = _resolve_divisions(ref, chapter, divided, recorded)

    book_code = chapter_of(ref)
    prefix = f"{ref}."
    # A Hebrew verse the table leaves unstated keeps its own number -- except
    # where another verse's row has already taken that number *whole*. Two
    # different things look like that collision, and the chapter's opening is
    # what tells them apart.
    #
    # At the head of a psalm it is the superscription the table deliberately
    # omits: Hebrew 3:1 is the ASV's unnumbered title and Hebrew 3:2 is the
    # ASV's 3:1, so the collision runs from the first Hebrew verse and the ASV
    # has a title to hold it. Some psalms number two such verses, so it is a
    # leading *run* rather than one verse.
    #
    # In the middle of a chapter it is a merge, which is the same shape the
    # table states outright at `1Kgs.22.44 -> 1Kgs.22.43!b`. The ASV's 1
    # Chronicles 12:4 is Hebrew 12:4 and 12:5 together, and the table says only
    # the half that moved (`1Chr.12.5 -> 1Chr.12.4`), leaving Hebrew 12:4 to be
    # read as unchanged. Both verses fill ASV 12:4, in the order they are
    # written, and the collision is the evidence for that rather than a problem
    # with it.
    #
    # A number claimed only in part -- the tail of ASV 18:33, the head of ASV
    # 22:22 -- is neither: the rest of that ASV verse is exactly what this
    # Hebrew verse still fills. Only this chapter's own rows shift its verses.
    whole_claims = {
        verse_of(target_ref)
        for (source_chapter, _, _), (target_ref, target_part) in routes.items()
        if source_chapter == chapter
        and target_part is None and target_ref.startswith(prefix)
    }

    segments: list[Segment] = []
    unmapped: list[int] = []
    # A superscription is what a chapter *opens* with, so only the leading run
    # of collisions can be one. That is also where the missing title stays an
    # error worth failing on: a chapter whose first Hebrew verse is displaced
    # and which prints no title for it is a chapter this build cannot explain,
    # and reading it as a merge would fold a superscription into verse 1 on no
    # evidence at all.
    opening = True
    for verse in sorted(source_verses):
        if (chapter, verse) in divided:
            for part in PARTS:
                target_ref, target_part = divided[(chapter, verse)][part]
                segments.append(Segment(chapter, verse, part, target_ref, target_part))
            opening = False
            continue
        route = routes.get((chapter, verse, None))
        if route is not None:
            segments.append(Segment(chapter, verse, None, route[0], route[1]))
            opening = False
            continue
        if verse in whole_claims and opening:
            if not has_title:
                raise ValueError(
                    f"{ref} has Hebrew verse {verse} with no ASV verse to map onto, "
                    "and the ASV chapter has no descriptive title to hold it"
                )
            unmapped.append(verse)
            continue
        if verse in whole_claims and verse not in target_verses:
            # It merges into the ASV verse of its own number, and the ASV has
            # no such verse. Nothing left to read it as.
            raise ValueError(
                f"{ref} has Hebrew verse {verse} whose number another verse has "
                f"taken, and no ASV verse {verse} for it to merge into"
            )
        segments.append(
            Segment(chapter, verse, None, f"{book_code}.{chapter}.{verse}", None)
        )
        opening = False

    # Whatever another Hebrew chapter sends here. Only the pieces that land in
    # this chapter are segments of it; the rest of that chapter is its own
    # business, and is not this chapter's to drop.
    for (source_chapter, source_verse, part), (target_ref, target_part) in sorted(routes.items()):
        if source_chapter == chapter or not target_ref.startswith(prefix):
            continue
        segments.append(
            Segment(source_chapter, source_verse, part, target_ref, target_part)
        )

    # The refusal that used to stand here now happens above, at the verse it is
    # about, so that it names that verse rather than a list gathered later.
    #
    # A title with no unmapped Hebrew verse is not a contradiction: the MT
    # numbers some superscriptions as verse 1 and writes others into the verse
    # it numbers 1, and Psalm 11 is the first of the second kind. The rule the
    # numbered superscriptions established still applies -- the title is the
    # ASV's rendering of the superscription, aligned against it -- so the
    # title's words are aligned in the packet for the Hebrew verse that
    # holds them, which is the one the ASV numbers 1.
    title_in_first_verse = has_title and not unmapped
    for verse in unmapped:
        segments.append(
            Segment(chapter, verse, None, f"{book_code}.{chapter}.{TITLE_VERSE}", None)
        )
    segments.sort(key=lambda segment: segment.order)

    # Every ASV verse this chapter's segments fill, which is what the chapter
    # can be checked against before it is built.
    filled = {
        verse_of(segment.target_ref) for segment in segments
        if segment.target_ref.startswith(prefix)
    }
    expected = set(target_verses) | ({TITLE_VERSE} if unmapped else set())
    unsourced = _unsourced_targets(routes, chapter, prefix, source_verses, expected - filled)
    versification = ChapterVersification(
        ref=ref,
        segments=tuple(segments),
        title_source_verses=tuple(unmapped),
        title_in_first_verse=title_in_first_verse,
        divisions=resolved_divisions,
        unsourced=unsourced,
    )
    _verify_segment_order(versification)
    if filled | set(unsourced) != expected:
        raise ValueError(
            f"{ref} versification mapping produces {sorted(filled)}, "
            f"but the ASV chapter has {sorted(expected)}"
        )
    return versification


def _verify_segment_order(versification: ChapterVersification) -> None:
    """Check text order against what the table says about each target's parts.

    Segments are ordered by the Hebrew, which is the order the text is written
    in. Where the table also marks the ASV side -- ``!a`` for a head, ``!b``
    for a tail -- the two must agree, and two segments may not claim the same
    position in the same ASV verse.
    """
    for target, group in versification.by_target.items():
        claimed: dict[str, Segment] = {}
        for index, segment in enumerate(group):
            part = segment.target_part
            if part is None:
                continue
            if part in claimed:
                raise ValueError(
                    f"{versification.ref} versification mapping is not one-to-one: "
                    f"{claimed[part].label} and {segment.label} both claim part {part} "
                    f"of ASV verse {target}"
                )
            claimed[part] = segment
            wanted = 0 if part == "a" else len(group) - 1
            if index != wanted:
                raise ValueError(
                    f"{versification.ref} places Hebrew {segment.label} at position "
                    f"{index + 1} of {len(group)} in ASV verse {target}, but the table "
                    f"calls it part {part} of that verse"
                )


def _resolve_divisions(
    ref: str,
    chapter: int,
    divided: dict[tuple[int, int], dict[str, tuple[str, str | None]]],
    recorded: dict[tuple[int, int], VerseDivision],
) -> dict[tuple[int, int], VerseDivision]:
    """Match each divided verse in the table against its recorded division.

    The table says *that* a verse divides and which two verses its parts fill;
    the recorded decision says *where*. Both halves are required, and they have
    to agree — a decision naming different targets than the table is a decision
    about a different verse.

    A neighbouring chapter's divided verse matters here only when one of its
    parts lands in this chapter, and the decision is read from the file of the
    chapter that owns the verse rather than recorded again.
    """
    prefix = f"{ref}."
    resolved: dict[tuple[int, int], VerseDivision] = {}
    for (source_chapter, source_verse), parts in sorted(divided.items()):
        first, second = parts["a"][0], parts["b"][0]
        ours = source_chapter == chapter
        if not ours and not (first.startswith(prefix) or second.startswith(prefix)):
            continue
        owner = f"{chapter_of(ref)}.{source_chapter}"
        division = recorded.get((source_chapter, source_verse))
        if division is None and ours:
            raise ValueError(
                f"{ref} divides Hebrew verse {source_verse} into ASV verses "
                f"{first} and {second}, and where it divides is a judgment "
                f"the table does not record. Run `make init-divisions CHAPTER={ref}`, "
                "resolve the entry, and build again"
            )
        if division is None:
            raise ValueError(
                f"{ref} is filled in part by Hebrew {owner}.{source_verse}, which "
                f"divides into ASV verses {first} and {second}. Where it divides is "
                f"{owner}'s judgment to record. Run "
                f"`make init-divisions CHAPTER={owner}`, resolve the entry, and "
                "build again"
            )
        if (division.first_target, division.second_target) != (first, second):
            raise ValueError(
                f"{ref} records a division of Hebrew verse {source_verse} into ASV "
                f"verses {division.first_target} and {division.second_target}, but the "
                f"table divides it into {first} and {second}"
            )
        resolved[(source_chapter, source_verse)] = division
    # Only this chapter's own decision file is held to naming nothing extra; a
    # neighbour's file legitimately records divisions this chapter never sees.
    unused = sorted(
        source_verse for (source_chapter, source_verse) in set(recorded) - set(resolved)
        if source_chapter == chapter
    )
    if unused:
        raise ValueError(
            f"{ref} records divisions for Hebrew verse(s) {unused}, which the "
            "versification table does not divide"
        )
    return resolved

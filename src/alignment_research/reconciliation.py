"""Whole-chapter and whole-corpus views for the session allowed to have one.

Per-verse isolation is what keeps a chapter affordable, and it is also what
lets two verses analyse one construction two different ways with neither
wrong on its own. Reconciliation is where that is caught. Finding the
candidates is mechanical — group every unit by what it aligns and print the
groups that disagree — while deciding which disagreement is a real split is
the session's whole job, so everything here is read-only. Nothing in this
module edits a verse, records a convention, or gates a build.

The corpus side answers the other question reconciliation is asked: whether a
rule it wants to promote is what the finished chapters already do. That check
was being written by hand every time, which is the reason it was sometimes
skipped, and a rule that contradicts a finished chapter is worse than no rule.

Both views join a chapter's prepared packets to its accepted verse files,
because the compiled ledger carries token *identifiers* and no token text.
Packets live in the generated tree, so a chapter whose packets are absent is
named as skipped rather than quietly dropped from a count.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .pipeline import ROOT, display_surface
from .scope import chapter_scope


def mark_supplied(words: tuple[str, ...], supplied: tuple[bool, ...]) -> str:
    """Join `words`, wrapping runs the ASV set in italics: `*the gospel*`.

    Consecutive supplied words are one run because the ASV italicises a
    supplied *phrase*; `*the* *gospel*` reads as two separate decisions.
    """
    parts: list[str] = []
    for word, is_supplied in zip(words, supplied):
        if is_supplied and parts and parts[-1].endswith("*"):
            parts[-1] = f"{parts[-1][:-1]} {word}*"
        else:
            parts.append(f"*{word}*" if is_supplied else word)
    return " ".join(parts)


@dataclass(frozen=True)
class Unit:
    """One accepted alignment unit, with its tokens resolved to text."""

    chapter: str
    ref: str
    status: str
    confidence: str
    note: str
    surfaces: tuple[str, ...]
    lemmas: tuple[str, ...]
    morphs: tuple[str, ...]
    source_head: str
    target: tuple[str, ...]
    target_head: str
    # Where this unit's tokens sit in the verse's source, parallel to
    # `surfaces`/`lemmas`/`morphs`. Empty for an `added` unit, which has no
    # source token at all. Defaulted because the field is only needed by the
    # neighbour filters, and every construction site passes by keyword.
    positions: tuple[int, ...] = ()
    # Which of this unit's English words the ASV set in italics, parallel to
    # `target`. Rule 11 makes that markup evidence rather than a verdict, and
    # evidence a session has to open the packet to see is evidence it will
    # skip: an article emptied by a supplied possessive and one emptied by
    # nothing look identical in a view that drops it.
    supplied: tuple[bool, ...] = ()

    @property
    def key(self) -> tuple[str, ...]:
        """What two occurrences must share to be the same thing analysed twice."""
        return tuple(sorted(set(self.lemmas)))

    @property
    def treatment(self) -> tuple[str, str, str]:
        """How this occurrence was analysed, insensitive to case only.

        Confidence was never part of it, and is no longer recorded at all:
        `render_corpus_query` counts precedent by grouping on this tuple, and
        letting the same analysis split into two rows over how sure two sessions
        were would make a unanimous corpus read as divided.
        """
        return (self.status, " ".join(self.target).lower(), self.target_head.lower())

    @property
    def english(self) -> str:
        return " ".join(self.target) or "(none)"

    @property
    def english_marked(self) -> str:
        """`english`, with ASV-supplied runs in asterisks: *the gospel*.

        A separate property, never the one the filters and `treatment` use.
        Marking the string those read would make `--target` patterns miss a
        supplied word and split one analysis into two rows over typography.
        """
        if not any(self.supplied):
            return self.english
        return mark_supplied(self.target, self.supplied)

    @property
    def greek(self) -> str:
        return " ".join(self.surfaces) or "(added)"

    @property
    def leading_morph(self) -> str:
        """The morph of this unit's leftmost token *in the source*.

        Not `morphs[0]`: a unit's tokens are listed in the order the verse file
        wrote them, which is source order only most of the time.
        """
        if not self.morphs:
            return ""
        if not self.positions:
            return self.morphs[0]
        return self.morphs[self.positions.index(min(self.positions))]

    @property
    def morph_class(self) -> str:
        """The coarse part of the morph code: `T-DSF` → `T`, `CONJ` → `CONJ`.

        Mechanical on purpose. A taxonomy of what an article does would have to
        be right about a language to avoid hiding things; this only has to be
        consistent, and the same view serves Hebrew chapters. Hebrew codes
        carry no `-`, so their class is the whole code and the signature is
        finer there — which is why nothing here concludes from a bucket.
        """
        return self.leading_morph.split("-")[0] or "?"


def chapter_units(root: Path, chapter: str) -> list[Unit]:
    """Every accepted unit of one chapter, in verse and document order."""
    scope = chapter_scope(chapter)
    packets = root / f"data/output/{scope.slug}/verses"
    accepted = root / f"data/alignments/{scope.slug}/verses"
    if not packets.is_dir():
        raise FileNotFoundError(
            f"no prepared packets for {scope.ref}; run "
            f"`make prepare-alignment CHAPTER={scope.ref}` first"
        )
    units: list[Unit] = []
    for path in sorted(packets.glob("*.json")):
        verse = accepted / path.name
        if not verse.exists():
            continue
        packet = json.loads(path.read_text(encoding="utf-8"))
        response = json.loads(verse.read_text(encoding="utf-8"))
        source = {token["id"]: token for token in packet["source_tokens"]}
        target = {token["id"]: token for token in packet["target_tokens"]}
        # Source order comes from the packet, never from the order the verse
        # file happens to list its units or their tokens in. Those agree most
        # of the time and disagree in about one verse in seven — and where they
        # disagree is exactly the reordered rendering whose neighbours matter.
        position = {token["id"]: i for i, token in enumerate(packet["source_tokens"])}
        for unit in response["alignments"]:
            head = unit.get("source_head")
            units.append(Unit(
                chapter=scope.ref,
                ref=packet["ref"],
                status=unit["status"],
                confidence=unit.get("confidence", ""),
                note=unit.get("note", ""),
                # A Hebrew morpheme may have no letters of its own, as when the
                # article assimilates into a pointed preposition. Printing an
                # empty string would hide a real token from the one view whose
                # job is to notice it.
                surfaces=tuple(display_surface(source[i]) for i in unit["source"]),
                lemmas=tuple(str(source[i]["lemma"]) for i in unit["source"]),
                morphs=tuple(str(source[i]["morph"]) for i in unit["source"]),
                source_head=display_surface(source[head]) if head else "-",
                target=tuple(target[i]["text"] for i in unit["target"]),
                supplied=tuple(
                    bool(target[i].get("added")) for i in unit["target"]
                ),
                target_head=(
                    target[unit["target_head"]]["text"] if unit.get("target_head") else "-"
                ),
                positions=tuple(position[i] for i in unit["source"]),
            ))
    return units


def _chain_key(unit: Unit) -> tuple[str, str, tuple[int, ...]]:
    """Identifies a unit by value: no two units of a verse share source tokens."""
    return (unit.chapter, unit.ref, unit.positions)


def neighbours(units: list[Unit]) -> dict[
    tuple[str, str, tuple[int, ...]], tuple[Unit | None, Unit | None]
]:
    """Each unit's source-order neighbours, within its own verse.

    Most of what a reconciliation actually asks is about a unit *and the source
    word next to it* — the contract's article rules are all of the form "an
    article, and what follows it" — and a unit filtered in isolation cannot
    answer any of them.

    A unit spanning several tokens takes its left edge from `min(positions)`
    and its right from `max`. An `added` unit has no source token, so it is in
    no chain: it neither has neighbours nor is anyone's.

    The chain stops at the verse edge. Reaching into the next verse would be
    cheap and wrong — rule 18 makes the verse the unit of work — so a unit at
    either end has no neighbour on that side, and therefore matches no filter
    on it.
    """
    by_verse: dict[tuple[str, str], list[Unit]] = defaultdict(list)
    for unit in units:
        if unit.positions:
            by_verse[(unit.chapter, unit.ref)].append(unit)
    index: dict[tuple[str, str, tuple[int, ...]], tuple[Unit | None, Unit | None]] = {}
    for verse in by_verse.values():
        ordered = sorted(verse, key=lambda u: (min(u.positions), max(u.positions)))
        for i, unit in enumerate(ordered):
            index[_chain_key(unit)] = (
                ordered[i - 1] if i else None,
                ordered[i + 1] if i + 1 < len(ordered) else None,
            )
    return index


def divergences(units: list[Unit]) -> list[tuple[tuple[str, ...], list[Unit]]]:
    """Every lemma this chapter analysed more than one way, commonest first.

    A lemma occurring once cannot disagree with itself, and a lemma always
    analysed the same way is exactly what should not be reported: the point of
    the table is that it is short enough to read in full.

    What counts as disagreeing is a split in `treatment` — different status,
    English or head. It used to include a split in confidence over an identical
    treatment, on the reasoning that sessions which agreed on every token and
    disagreed about whether the call was settled had disagreed about the
    construction. Measured, that produced 12 of 1,775 candidates across 40
    chapters, none of them moving a token, and the field itself is no longer
    recorded past `nt-greek-verse-v58` / `ot-hebrew-verse-v14`. A chapter old
    enough to carry confidences is not made inconsistent by them.
    """
    grouped: dict[tuple[str, ...], list[Unit]] = defaultdict(list)
    for unit in units:
        if unit.status == "added":
            continue  # No source lemma to key on; nothing to be consistent with.
        grouped[unit.key].append(unit)
    found = [
        (key, group) for key, group in grouped.items()
        if len(group) > 1 and len({unit.treatment for unit in group}) > 1
    ]
    return sorted(found, key=lambda pair: (-len(pair[1]), pair[0]))


VERSE_END = "(end)"


def grade(unit: "Unit") -> str:
    """A legacy confidence marker, where the unit has one.

    Nothing records confidence past `nt-greek-verse-v58` / `ot-hebrew-verse-v14`
    — it took two of its three values, 96% of them `high`, nothing in the
    pipeline gated on it, and no two models measured here ever agreed on it. The
    76,646 units that carry one still print it, because it is real data in a
    committed ledger and a view that dropped it silently would misreport what
    those chapters say.
    """
    return f"  [{unit.confidence}]" if unit.confidence not in ("", "high") else ""


def context_buckets(
    group: list[Unit],
    chains: dict[tuple[str, str, tuple[int, ...]], tuple[Unit | None, Unit | None]],
) -> list[tuple[str, list[Unit]]]:
    """Split one lemma's occurrences by a coarse context signature, commonest first.

    The signature is the unit's own morph class and that of the **next** unit
    in source order. Next-only, not both sides: conditioning on the previous
    unit as well fragments a chapter into singleton buckets, which compare
    nothing. For the article — the highest-frequency and highest-risk token in
    an epistle — the governing direction is forwards anyway.

    The known consequence is that a second attributive article and a
    substantival one both land in `T→V`, and what licenses their difference is
    what *precedes*. That bucket is still the right one to look at: it is
    exactly the set rules 31 and 32 say to compare, and the session decides it
    as it decides every other row.
    """
    grouped: dict[str, list[Unit]] = defaultdict(list)
    for unit in group:
        _, following = chains.get(_chain_key(unit), (None, None))
        signature = f"{unit.morph_class}→{following.morph_class if following else VERSE_END}"
        grouped[signature].append(unit)
    return sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0]))


def render_divergences(chapter: str, units: list[Unit]) -> str:
    """The candidate table: what to look at, never what to conclude."""
    found = divergences(units)
    chains = neighbours(units)
    lines = [
        f"=== {chapter} cross-verse candidates — "
        f"{len(found)} lemma(s) analysed more than one way",
        "  Candidates, not findings. Most of these are the ASV rendering one",
        "  word differently in different contexts, which is not a split; a split",
        "  is one construction analysed two ways with nothing in the source to",
        "  license the difference. Read each group and decide. Where you find a",
        "  real one, fix every verse it touches, re-validate, re-run",
        "  mark-reviewed, and record it with record-convention.",
        "  Each lemma is split by context: the unit's own morph class and the",
        "  class of the next source unit, written CLASS→CLASS. A bucket that",
        "  agreed with itself is one summary line, so what is printed in full is",
        "  where a context was treated two ways. The signature is mechanical and",
        "  coarse, so it can put two different constructions in one bucket, and",
        "  it never says which of them is right.",
        "  The table is keyed by source lemma, so it is blind to `added` units:",
        "  they have no source token and appear in no group. A chapter that marks",
        "  a supplied English word `added` in one verse and folds it into the word",
        "  that licenses it in another shows a clean table and still holds a real",
        "  split. Only the dump, read end to end, shows that one.",
        "",
    ]
    for key, group in found:
        treatments = len({unit.treatment for unit in group})
        lines.append(
            f"### {' '.join(key)}  ({len(group)} occurrences, {treatments} treatments)"
        )
        buckets = context_buckets(group, chains)
        singletons = 0
        for signature, bucket in buckets:
            kinds = {unit.treatment for unit in bucket}
            head = f"  {signature:<12} {len(bucket)} occurrence(s), {len(kinds)} treatment(s)"
            if len(kinds) == 1:
                status, english, target_head = next(iter(kinds))
                lines.append(
                    f"{head} — all {status}, "
                    f"{english or '(none)'}, head={target_head}"
                )
                singletons += len(bucket) == 1
                continue
            lines.append(head)
            for unit in bucket:
                flag = grade(unit)
                lines.append(
                    f"   {unit.ref:<11} {unit.status:<14} {'+'.join(unit.morphs):<16}"
                    f" {unit.greek:<22} -> {unit.english:<34} head={unit.target_head}{flag}"
                )
        if all(len({u.treatment for u in b}) == 1 for _, b in buckets):
            lines.append(
                f"  Every bucket here is internally unanimous ({len(buckets)}"
                f" bucket(s), {singletons} of them a single occurrence, which"
                " explains nothing)."
            )
            lines.append(
                "  That is evidence that context accounts for the difference,"
                " not a verdict that it does."
            )
        lines.append("")
    if not found:
        lines.append("  Nothing. Every repeated lemma was analysed one way.")
        lines.append("")
    return "\n".join(lines)


def render_dump(chapter: str, units: list[Unit]) -> str:
    """Every unit of the chapter, one line each, for grepping.

    Written to a file rather than into a briefing on purpose: it is linear in
    chapter length, and Psalm 119 would swamp the part a session must read.
    """
    lines = [
        f"=== {chapter} — every accepted unit ({len(units)})",
        "English wrapped in asterisks is the ASV's italics: supplied by the",
        "translators, and rule 11 evidence rather than a verdict. Notes quote",
        "English with the same asterisks, and sit past the `#`.",
        "",
    ]
    current = ""
    for unit in units:
        if unit.ref != current:
            current = unit.ref
            lines.append(f"=== {unit.ref}")
        flag = grade(unit)
        note = f"  # {unit.note}" if unit.note else ""
        lines.append(
            f"  {unit.status:<13} {unit.greek:<34} ({'+'.join(unit.morphs)})"
            f" -> {unit.english_marked:<38} head={unit.target_head}{flag}{note}"
        )
    return "\n".join(lines) + "\n"


def compiled_chapters(root: Path) -> list[str]:
    """Chapters with accepted verses on disk, in directory order.

    A verse, not a directory: a pass that halts before it lands anything still
    leaves `verses/` behind it, and counting that empty directory made every
    corpus query report a chapter it had nothing to read — MAT.26 and MRK.14
    each halted on an oversized pack and put the whole corpus check into
    disagreement with itself.

    Supplementary footnote and KJV Greek alignments also have a `verses/`
    directory but are not main-text chapters. Exclude only those known stores,
    so malformed chapter evidence is still surfaced rather than hidden by a
    chapter-name filter. Keep landed, uncompiled chapters in this inventory;
    the ledger-based inventory would omit the work reconciliation must inspect.
    """
    directory = root / "data/alignments"
    return sorted(
        path.name for path in directory.iterdir()
        if path.name not in {"footnotes", "kjv-greek"}
        and path.is_dir() and any((path / "verses").glob("*.json"))
    )


def corpus_units(
    root: Path, *, testament: str | None = None, exclude: str | None = None,
) -> tuple[list[Unit], list[str]]:
    """Every accepted unit in the corpus, and the chapters that could not be read.

    The skipped list is returned rather than logged away. A corpus check that
    silently covered eighty chapters instead of eighty-three reads exactly like
    one that covered all of them, and the whole value of the check is that its
    coverage is known.
    """
    units: list[Unit] = []
    skipped: list[str] = []
    for chapter in compiled_chapters(root):
        if exclude and chapter == chapter_scope(exclude).ref:
            continue
        if testament and chapter_scope(chapter).testament != testament:
            continue
        try:
            units.extend(chapter_units(root, chapter))
        except (FileNotFoundError, KeyError):
            skipped.append(chapter)
    return units, skipped


def _neighbour_matches(
    neighbour: Unit | None, morph: str | None, pattern: re.Pattern[str] | None,
) -> bool:
    """A missing neighbour matches no filter; an absent filter matches anything."""
    if not morph and pattern is None:
        return True
    if neighbour is None:
        return False
    if morph and not any(m.startswith(morph) for m in neighbour.morphs):
        return False
    if pattern and not pattern.search(neighbour.english):
        return False
    return True


def canonical(text: str) -> str:
    """A lemma reduced to the form two spellings of it agree on.

    Hebrew points are combining marks, and the same lemma can be written with
    them in either order: MACULA stores `בְּ` as bet, dagesh, sheva, while the
    same three characters typed or copied out of a briefing usually arrive as
    bet, sheva, dagesh. The two are indistinguishable on screen and unequal as
    strings, so an exact comparison silently missed every unit — a query for
    `בְּ` reported no precedent for a lemma the corpus holds 182 times, and
    reported it in the words the caller reads as a finding rather than as a
    miss. Normalizing both sides is what makes the comparison mean what the
    caller meant; canonical ordering sorts by combining class, so it reaches
    any mark order rather than the one pair that prompted this.

    Greek needs it too, for the same reason in the other direction: a lemma
    with its accent composed and one with it decomposed are equally invisible
    a difference.
    """
    return unicodedata.normalize("NFC", text)


def matches(
    units: list[Unit], *, lemma: str | None = None, morph: str | None = None,
    target: str | None = None, status: str | None = None,
    prev_morph: str | None = None, next_morph: str | None = None,
    prev_target: str | None = None, next_target: str | None = None,
) -> Iterator[Unit]:
    """Filter units by source lemma, morph prefix, English text, or status.

    The four neighbour filters ask the same two questions of the source word
    before or after the unit, with the same semantics: a morph prefix, and a
    regex over English. The chain is built here rather than passed in so the
    signature stays the only new surface — these are one-shot CLI queries, and
    an O(n) rebuild per call costs nothing worth saving.
    """
    wanted = canonical(lemma) if lemma else None
    pattern = re.compile(target, re.IGNORECASE) if target else None
    before = re.compile(prev_target, re.IGNORECASE) if prev_target else None
    after = re.compile(next_target, re.IGNORECASE) if next_target else None
    chains = (
        neighbours(units)
        if any((prev_morph, next_morph, prev_target, next_target)) else {}
    )
    for unit in units:
        if wanted and wanted not in (canonical(l) for l in unit.lemmas):
            continue
        if morph and not any(m.startswith(morph) for m in unit.morphs):
            continue
        if status and unit.status != status:
            continue
        if pattern and not pattern.search(unit.english):
            continue
        if chains:
            previous, following = chains.get(_chain_key(unit), (None, None))
            if not _neighbour_matches(previous, prev_morph, before):
                continue
            if not _neighbour_matches(following, next_morph, after):
                continue
        yield unit


def render_corpus_query(
    found: list[Unit], skipped: list[str], *, examples: int = 6, described: str = "",
    notes: bool = False,
) -> str:
    """Group the hits by treatment, so the shape of the precedent is the answer.

    A promotion check wants to know whether the corpus is unanimous, lopsided,
    or split. Counts by treatment say that at a glance; a list of matches does
    not.

    The head is part of the key, because two units that pick different heads
    for the same English are two analyses. Grouping on status and English alone
    printed them as one row and listed both heads on it, so nine occurrences of
    `ἃ` → *the things which* that split seven to two read as unanimous. That is
    the one way this output can be worse than a list of matches: a reader who
    counts rows sees agreement that is not there.

    `notes` prints each example's own note underneath it. Off by default
    because it triples the height of a large result, and worth asking for
    whenever the count is close: a precedent whose note concedes the other
    reading is weaker evidence than one that does not, and the counts alone
    cannot say which kind a row is. That distinction decides promotions, and
    it is otherwise reachable only by opening the other chapter's verse file.
    """
    grouped: dict[tuple[str, str, str], list[Unit]] = defaultdict(list)
    for unit in found:
        status, english, head = unit.treatment
        grouped[(status, english or "(none)", head or "-")].append(unit)
    lines = [
        f"=== corpus query{f' — {described}' if described else ''}: "
        f"{len(found)} unit(s) in {len({unit.chapter for unit in found})} chapter(s)",
        "",
    ]
    for (status, english, head), group in sorted(
        grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
    ):
        lines.append(
            f"{len(group):>5}  {status:<13} {english:<32} head: {head}"
        )
        for unit in group[:examples]:
            marker = grade(unit)
            lines.append(
                f"       {unit.ref:<11} {unit.greek:<26} ({'+'.join(unit.morphs)}){marker}"
            )
            if notes and unit.note:
                lines.append(f"         # {unit.note}")
        if len(group) > examples:
            lines.append(f"       … and {len(group) - examples} more")
    if not found:
        lines.append("  No unit in the corpus matches. The corpus has no precedent"
                     " either way, which is not the same as agreeing with you.")
    if skipped:
        lines.extend([
            "",
            f"  Not read ({len(skipped)}): {', '.join(skipped)}",
            "  Their packets are missing from the generated tree; "
            "`make prepare-alignment` restores them.",
        ])
    return "\n".join(lines) + "\n"


def write_view(root: Path, chapter: str) -> dict[str, Any]:
    """The whole reconcile-view: candidate table to stdout, dump to disk."""
    scope = chapter_scope(chapter)
    units = chapter_units(root, scope.ref)
    dump = root / f"data/output/{scope.slug}/reconcile-dump.txt"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(render_dump(scope.ref, units), encoding="utf-8")
    return {
        "chapter": scope.ref,
        "units": len(units),
        "candidates": len(divergences(units)),
        "table": render_divergences(scope.ref, units),
        "dump": str(dump.relative_to(root)),
    }


def view_text(root: Path = ROOT, chapter: str = "JHN.1") -> str:
    view = write_view(root, chapter)
    return (
        f"{view['table']}"
        f"  Every unit of the chapter, one line each, is in {view['dump']}\n"
        f"  ({view['units']} units). Grep it rather than opening verse JSON.\n"
    )


def verse_reference(ref: str) -> tuple[str, str]:
    """Split `GAL.2.17` into the chapter to load and the verse to keep."""
    pieces = ref.upper().split(".")
    if len(pieces) != 3:
        raise ValueError(f"expected a verse such as GAL.2.17, not {ref!r}")
    scope = chapter_scope(".".join(pieces[:2]))
    try:
        verse = int(pieces[2])
    except ValueError as error:
        raise ValueError(f"invalid verse reference {ref!r}") from error
    if verse < 1:
        raise ValueError(f"invalid verse reference {ref!r}")
    return scope.ref, f"{scope.ref}.{verse}"


def _packet_for(root: Path, chapter: str, verse_ref: str) -> dict[str, Any] | None:
    """One verse's prepared packet, found by its own `ref`.

    Scanned rather than addressed by filename: the packet for verse 7 is
    `07.json` only while a chapter stays under a hundred verses, and Psalm 119
    does not.
    """
    directory = root / f"data/output/{chapter_scope(chapter).slug}/verses"
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json")):
        packet = json.loads(path.read_text(encoding="utf-8"))
        if packet.get("ref") == verse_ref:
            return packet
    return None


def render_verse(root: Path, ref: str) -> str:
    """One verse's accepted units, with its Greek and its ASV above them.

    The chapter dump answers this for the chapter in hand, and reconciliation
    spends most of its time in *other* chapters: a corpus query says how often
    a treatment occurs and in which verses, and the next question is always
    what those verses actually look like. Without this that meant opening two
    JSON files and joining them by token id — the packet holds the text and
    the accepted file holds only identifiers — which is enough friction to
    turn a precedent worth reading into one taken on trust.
    """
    chapter, verse_ref = verse_reference(ref)
    scope = chapter_scope(chapter)
    packet = _packet_for(root, chapter, verse_ref)
    if packet is None:
        return (
            f"=== {verse_ref}: no prepared packet.\n"
            f"  Either the verse is past the end of {chapter}, or the chapter "
            f"has not been prepared;\n"
            f"  `make prepare-alignment CHAPTER={chapter}` builds it.\n"
        )
    units = [unit for unit in chapter_units(root, chapter) if unit.ref == verse_ref]
    greek = " ".join(display_surface(token) for token in packet["source_tokens"]) or (
        "(none: the pinned source edition does not contain this verse)"
    )
    asv = mark_supplied(
        tuple(token["text"] for token in packet["target_tokens"]),
        tuple(bool(token.get("added")) for token in packet["target_tokens"]),
    )
    lines = [
        f"=== {verse_ref} — {len(units)} accepted unit(s)",
        "English wrapped in asterisks is the ASV's italics: supplied by the",
        "translators, and rule 11 evidence rather than a verdict. Notes quote",
        "English with the same asterisks, and sit past the `#`.",
        "",
        f"  {'Hebrew' if scope.is_hebrew else 'Greek':<6} {greek}",
        f"  ASV    {asv}",
        "",
    ]
    # Where the source is not written in this verse. A reviewer reading a
    # supplied verse's Hebrew has to know it is the parallel's, or the one
    # thing worth checking about the verse is invisible.
    for source_ref in sorted({
        token["supplied_from"] for token in packet["source_tokens"]
        if token.get("supplied_from")
    }):
        lines[-1:-1] = [
            f"  supplied from {source_ref}: the pinned edition does not contain "
            "this verse, and this is the parallel it is read from",
        ]
    if not units:
        lines.append(
            "  No accepted alignment on disk for this verse yet, so only the "
            "packet is shown."
        )
        return "\n".join(lines) + "\n"
    for unit in units:
        flag = grade(unit)
        note = f"\n      # {unit.note}" if unit.note else ""
        lines.append(
            f"  {unit.status:<13} {unit.greek:<30} ({'+'.join(unit.morphs)})"
            f" -> {unit.english_marked:<36} head={unit.target_head}{flag}{note}"
        )
    lines.append("")
    lines.append("  Read-only. Nothing here edits a verse or gates a build.")
    return "\n".join(lines) + "\n"

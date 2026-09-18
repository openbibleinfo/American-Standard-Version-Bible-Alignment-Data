"""Complete compact briefings for verse alignment and review.

Aligning and reviewing a verse both need the same small facts: which
source tokens exist, which target tokens exist, and how the accepted units
join them. Reading the contract, schema, and raw JSON separately costs model
round trips, so the renderer supplies the contract, actions, and bounded verse
view together.
"""
from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path
from typing import Any

from .alignment import (
    anchor_chapter_variants,
    conventions_path,
    instructions_path,
    load_jsonl,
    prepare_alignment,
    recorded_conventions,
)
from .pipeline import ROOT, build_chapter, display_surface
from .diagnostics import (
    LARGE_SOURCE_REVIEW_CHECKLIST,
    LARGE_SOURCE_UNIT_THRESHOLD,
)
from .reconciliation import write_view
from .scope import ChapterScope, chapter_scope


def parse_verses(value: str | None, available: list[int]) -> list[int]:
    """Accept ``7``, ``1-8``, ``1,3,5``, or nothing for the whole chapter."""
    if not value:
        return available
    wanted: list[int] = []
    for piece in value.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            start, end = (int(part) for part in piece.split("-", 1))
            wanted.extend(range(start, end + 1))
        else:
            wanted.append(int(piece))
    unknown = sorted(set(wanted) - set(available))
    if unknown:
        raise ValueError(f"no prepared packet for verse(s) {unknown}")
    return [verse for verse in available if verse in set(wanted)]


def verse_number(ref: str) -> int:
    return int(ref.rsplit(".", 1)[1])


def source_column(token: dict[str, Any]) -> str:
    """Print a source token so a terminal can be read left to right.

    A right-to-left surface reorders whatever shares its line and cannot be
    padded to a column width, since its combining points are zero width. Where
    the source supplies a transliteration, that is what gets the aligned
    column, and the pointed form follows on its own.
    """
    surface = display_surface(token)
    lemma = token.get("lemma") or "-"
    transliteration = token.get("transliteration")
    if transliteration:
        return f"{transliteration:<20} {surface} / {lemma}"
    return f"{surface:<20} / {lemma}"


def render_packet(packet: dict[str, Any]) -> list[str]:
    lines = [
        f"=== {packet['ref']}  ({len(packet['source_tokens'])} source"
        f" / {len(packet['target_tokens'])} target)"
    ]
    lines.append("-- source")
    # Where the words are written, when that is not this verse. A verse the
    # pinned edition does not contain is filled from its parallel, and the
    # pass has to be told: the Hebrew is real and aligns as usual, but it is
    # another book's verse and its note should say so.
    supplied = sorted({
        token["supplied_from"] for token in packet["source_tokens"]
        if token.get("supplied_from")
    })
    for source_ref in supplied:
        lines.append(
            f"  (supplied from {source_ref}: the pinned source edition does not"
            " contain this verse, which the ASV's underlying text has, so the"
            " parallel verse it renders identically supplies the source here)"
        )
    if not packet["source_tokens"]:
        # A verse the pinned edition does not contain. Saying so is the whole
        # briefing for it: a pass that read an empty source list without being
        # told why would look for the missing text rather than align what is
        # in front of it.
        lines.append(
            "  (none: the pinned source edition does not contain this verse,"
            " which the ASV's underlying text has)"
        )
    for token in packet["source_tokens"]:
        lines.append(
            f"  {token['id']} {str(token['morph']):<12} {str(token['gloss']):<22}"
            f" {source_column(token)}"
        )
    lines.append("-- target")
    lines.append(
        "  "
        + "  ".join(
            f"{token['id']}:{token['text']}" + ("*" if token["added"] else "")
            for token in packet["target_tokens"]
        )
    )
    lines.append("  " + " ".join(token["text"] for token in packet["target_tokens"]))
    for note in packet["target_notes"]:
        lines.append(f"-- note ({note['class']}) {note['text']}")
    by_project_id = {
        token.get("project_id"): token["id"] for token in packet["source_tokens"]
    }
    for decision in packet["variant_decisions"]:
        lines.append(
            f"-- variant {decision['unit_id']} {decision['status']}"
            f" -> {decision['operational_reading']}"
        )
        lines.extend(f"   {line}" for line in variant_detail(decision, by_project_id))
    return lines


def variant_detail(
    decision: dict[str, Any], by_project_id: dict[str, str]
) -> list[str]:
    """What a note about this unit has to be able to cite.

    The unit id and the operational reading are not enough to write a note
    with. A pass that finds English answering to no source token needs to know
    whether a textual unit is why, which readings the decision left standing,
    and which of its own s### the unit covers. Rule 12 still forbids deciding
    the unit again; this is the citation, not a second vote.

    The line that matters most is the last one. An underdetermined unit falls
    back to the base reading, and the base is usually one of the readings the
    English still permits. Where it is not — where the evidence positively
    excluded the very reading the projection then carried — the projection is
    known to differ from the ASV's Greek, and the English with no token to
    answer to is that difference showing through. Saying so is what turns a
    guess into a citation.
    """
    covered = [
        by_project_id[project_id]
        for project_id in decision.get("projected_source_ids", [])
        if project_id in by_project_id
    ]
    compatible = decision.get("compatible_readings", [])
    detail = []
    if compatible:
        detail.append("compatible " + ", ".join(compatible))
    detail.append("covers " + (", ".join(covered) if covered else "no source token"))
    lines = ["; ".join(detail)]
    if decision.get("operational_reading") not in compatible and compatible:
        lines.append(
            "FALLBACK EXCLUDED BY EVIDENCE: the projection carries"
            f" {decision['operational_reading']}, which this unit's evidence ruled"
            " out, so English here may answer to a source word the projection"
            " does not have."
        )
    return lines


def render_response(packet: dict[str, Any], response: dict[str, Any]) -> list[str]:
    source = {token["id"]: token for token in packet["source_tokens"]}
    target = {token["id"]: token for token in packet["target_tokens"]}
    large_units = {
        ordinal: len(unit["source"])
        for ordinal, unit in enumerate(response["alignments"], 1)
        if len(unit["source"]) >= LARGE_SOURCE_UNIT_THRESHOLD
    }
    lines = [f"-- accepted alignment ({len(response['alignments'])} units)"]
    if large_units:
        units = ", ".join(
            f"{ordinal} ({count} source morphemes)"
            for ordinal, count in large_units.items()
        )
        lines.append(
            "-- large-source-unit review candidates: units " + units + ". "
            "Check their atomicity; the threshold is a heuristic, not evidence "
            "that a unit is wrong."
        )
    for ordinal, unit in enumerate(response["alignments"], 1):
        original = " ".join(
            display_surface(source[value]) for value in unit["source"]
        ) or "∅"
        english = []
        for value in unit["target"]:
            word = target[value]["text"]
            if target[value]["added"]:
                word = f"*{word}*"
            if value == unit["target_head"]:
                word = f"[{word}]"
            english.append(word)
        head = source[unit["source_head"]] if unit["source_head"] else None
        flags = "" if unit["status"] == "aligned" else f"  !{unit['status']}"
        if ordinal in large_units:
            flags += f"  ?large-source-unit:{large_units[ordinal]}"
        # Shown only where a pre-v59/v15 verse recorded one. The field is no
        # longer asked for, and printing an empty grade beside every unit of a
        # new chapter would be a column that says nothing.
        if unit.get("confidence", "high") != "high":
            flags += f"  ({unit['confidence']})"
        source_ids = " ".join(
            f"[{value}]" if value == unit["source_head"] else value
            for value in unit["source"]
        ) or "-"
        target_ids = " ".join(
            f"[{value}]" if value == unit["target_head"] else value
            for value in unit["target"]
        ) or "-"
        lines.append(
            f"  S:{source_ids:<22} T:{target_ids:<22}"
            f" | {' '.join(english) or '∅'}{flags}"
            f"   [{original} / {head['lemma'] if head else '-'}]"
        )
        if unit["note"]:
            lines.append(f"      {unit['note']}")
    for note in response["review_notes"]:
        lines.append(f"  review: {note}")
    return lines


PASS_NAMES = {
    "packet": "alignment",
    "review": "review",
    "both": "alignment and review",
}


def render_session_notice(scope_ref: str, mode: str, wanted: list[int]) -> list[str]:
    """State the session rule where it is actually decided.

    A render begins every pass, so this is the one moment the rule is read at
    the point it applies. It is stated in `AGENTS.md` too, but a file read once
    at startup cannot reach the turn where a finished session is about to take
    another chapter.
    """
    span = f"{wanted[0]}-{wanted[-1]}" if len(wanted) > 1 else str(wanted[0])
    lines = [
        f"=== {scope_ref} {PASS_NAMES[mode]} pass — "
        f"{len(wanted)} verse(s), {span}",
    ]
    if len(wanted) == 1:
        lines.extend([
            "  One session performs one pass for one verse. If this session has",
            "  already finished a verse pass, stop and start a fresh one. Continue",
            "  only to resume this same pass, reconstructed with make status.",
        ])
    else:
        lines.extend([
            "  This range render is for inspection only. Before linguistic work,",
            "  render one verse and give it a fresh one-pass model session.",
        ])
    lines.append("")
    return lines


def render_contract(root: Path, scope: ChapterScope) -> list[str]:
    """Include the binding testament contract in the one required read."""
    contract = instructions_path(root, scope).read_text(encoding="utf-8").rstrip()
    return ["=== Binding testament contract", *contract.splitlines(), ""]


def render_response_shape(ref: str) -> list[str]:
    """Show the exact compact response shape so the schema need not be opened."""
    return [
        "=== Accepted response shape",
        "  Write one JSON object with exactly these fields:",
        f'  {{"ref":"{ref}","alignments":[{{"source":["s###"],',
        '  "source_head":"s###","target":["t###"],"target_head":"t###",',
        '  "status":"aligned","note":""}],',
        '  "review_notes":[]}',
        "  Status is aligned, unrepresented, added, or noncompositional;",
        "  note is always a string. There is no confidence field: say what was",
        "  contested in the note, which is what a later reader can act on.",
        "",
    ]


def render_pass_actions(
    scope: ChapterScope, mode: str, wanted: list[int]
) -> list[str]:
    """Keep deterministic pass mechanics in the briefing, not extra reads."""
    if len(wanted) != 1:
        return []
    verse = wanted[0]
    ref = f"{scope.ref}.{verse}"
    accepted = f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
    validate = (
        "PYTHONPATH=src python3 scripts/generate_alignments.py "
        f"validate-verse {ref}"
    )
    lines = [
        "=== Pass actions",
        "  This briefing is complete, rendered or handed to you.",
        "  Do not open packet JSON, the response schema, README, AGENTS.md, or",
        "  list/check/create directories.",
    ]
    if mode == "packet":
        lines.extend([
            f"  Write {accepted}, then validate with:",
            f"    {validate}",
            "  Codex: after reasoning, apply the patch and run validation in one",
            "  functions.exec interaction; do not pause between those operations.",
        ])
    elif mode == "review":
        lines.extend([
            "  The token table and accepted units below are sufficient for review.",
            f"  {LARGE_SOURCE_REVIEW_CHECKLIST}",
            "  Do not open accepted JSON unless you first identify a correction and",
            "  need its exact edit context.",
            "  A verse that is already right takes one reply and no tool call.",
            "  If correcting, apply the patch and validate it in one functions.exec",
            "  interaction:",
            "    PYTHONPATH=src python3 scripts/generate_alignments.py "
            f"validate-verse {ref}",
            "  Either way, end your reply with one line — REVIEWED: <one sentence a",
            "  later reader can act on>. Whoever launched you records the receipt",
            "  with mark-reviewed and names what reviewed the verse; do not run it",
            "  yourself.",
        ])
    lines.append("")
    return lines


def render_conventions(root: Path, scope: ChapterScope) -> list[str]:
    recorded = recorded_conventions(conventions_path(root, scope))
    if not recorded:
        return []
    return [
        f"=== {scope.ref} conventions already settled",
        *(f"  - {entry}" for entry in recorded),
        "  Apply these; record any new chapter-wide judgment with "
        "generate_alignments.py record-convention.",
        "",
    ]


def render_decisions(root: Path, scope: ChapterScope) -> str:
    """The whole briefing for a chapter's unresolved textual decisions.

    This is the one briefing that cannot go through `prepare_alignment`, since
    the unresolved decisions are precisely what preparation refuses to run
    without. It reads the normalized apparatus instead, which the build has
    already produced: the closed candidate set for each unit, and the ASV verse
    and notes that are the only evidence allowed to decide it.

    Witnesses are shown but are not evidence here. The question is never which
    reading is original; it is which reading the ASV translators rendered, and
    that is answered from their English and their notes or not at all.
    """
    build_chapter(scope.ref, root)
    normalized = root / f"data/normalized/{scope.slug}"
    # Anchored, because a lemma may be quoted across a verse boundary and a
    # unit can only be judged against all the English it governs. Jude 22-23
    # is one apparatus entry, and briefed with verse 22 alone it was decided
    # on half its evidence.
    anchored = anchor_chapter_variants(
        scope,
        load_jsonl(normalized / "source-tokens.jsonl"),
        load_jsonl(normalized / "source-variants.jsonl"),
        root,
    )
    units = {unit["id"]: unit for unit in anchored}
    verses = {verse["ref"]: verse for verse in load_jsonl(normalized / "asv-target-tokens.jsonl")}
    notes: dict[str, list[dict[str, Any]]] = {}
    for note in load_jsonl(normalized / "asv-notes.jsonl"):
        notes.setdefault(note["ref"], []).append(note)

    path = root / "config" / scope.variant_decisions_name
    document = json.loads(path.read_text(encoding="utf-8"))
    owed = [entry for entry in document["decisions"] if entry.get("status") == "needs_review"]

    lines = [
        f"=== {scope.ref} textual decisions — {len(owed)} unit(s) awaiting judgment",
        "",
        "=== Policy",
        *(f"  {line}" for line in textwrap.wrap(document["policy"], 76)),
        "",
        "=== How to decide a unit",
        "  Select a reading only when the ASV distinguishes it: its wording, an",
        "  italicized supply, or an ASV note explicitly mapped to this unit. A",
        "  note classed alternative_rendering is about English wording and never",
        "  decides a reading; a note classed textual_variant may.",
        "  Otherwise the unit is underdetermined: keep every reading the English",
        "  still permits and the documented base fallback carries the projection.",
        "  Both outcomes are decisions and both need evidence. Witness lists say",
        "  which editions print a reading, never which one the ASV rendered.",
        "  Judge each unit only from the ASV shown beneath it, which is every",
        "  verse its lemma touches; do not harmonise across the",
        "  chapter, and do not open the apparatus, other chapters, or the vendor",
        "  data. This render is the complete briefing.",
        "",
        "=== Recording the decisions",
        "  Record every unit below in one command, as a JSON array on stdin.",
        "  A session pays for its whole context on every turn, so one command",
        "  for the chapter is most of what this pass costs. The heredoc marker",
        "  closes at the start of a line, so run this unindented:",
        "",
        "PYTHONPATH=src python3 scripts/generate_alignments.py \\",
        f"  decide-many {scope.ref} <<'JSON'",
        "[",
        "  {\"unit_id\": \"...\", \"status\": \"selected\",",
        "   \"reading\": \"r##\", \"evidence\": \"...\"},",
        "  {\"unit_id\": \"...\", \"status\": \"underdetermined\",",
        "   \"compatible\": [\"r##\", \"r##\"], \"evidence\": \"...\"}",
        "]",
        "JSON",
        "",
        "  compatible defaults to every candidate; narrow it when the English",
        "  excludes one but does not settle the rest. Every judgment is checked",
        "  before any is written, so a batch that names a reading outside a",
        "  candidate set is refused whole and the file is untouched — fix the",
        "  named units and send the array again. Do not edit the decisions file",
        "  directly and do not commit.",
        "",
    ]

    for entry in owed:
        unit = units.get(entry["unit_id"])
        if unit is None:
            # Coverage is proved at preparation; a scaffold this stale is a
            # build problem and saying so beats rendering a unit with no text.
            raise ValueError(
                f"{entry['unit_id']} is in {path.name} but not in the built apparatus; "
                f"the scaffold is stale for {scope.ref}"
            )
        refs = unit.get("base_refs") or [unit["ref"]]
        span = unit["ref"] if len(refs) == 1 else (
            f"{refs[0]}-{refs[-1].rsplit('.', 1)[1]}, quoted across the verse boundary"
        )
        lines.append(f"=== {entry['unit_id']}  ({span}, at {unit['location']})")
        lines.append(f"-- base lemma  {unit['lemma'] or '∅'}")
        for candidate in unit["candidates"]:
            mark = "base" if candidate.get("base") else "    "
            witnesses = ", ".join(candidate.get("witnesses") or []) or "-"
            lines.append(
                f"  {candidate['id']:<9} {mark}  {candidate['text'] or '∅ (omits)'}"
            )
            lines.append(f"  {'':<9}       witnesses: {witnesses}")
        for ref in refs:
            lines.append(f"-- ASV {ref}")
            text = verses.get(ref, {}).get("text", "(verse text unavailable)")
            for line in textwrap.wrap(text, 74):
                lines.append(f"  {line}")
            for note in notes.get(ref, []):
                lines.append(f"-- note ({note['class']}) {note['text']}")
        lines.append("")
    return "\n".join(lines)


def render_reconciliation(root: Path, scope: ChapterScope) -> str:
    """The whole briefing for the one session allowed to see a whole chapter.

    Reconciliation was the last pass without a render, and it paid for that:
    pointed at `AGENTS.md` by `CLAUDE.md`, it read a thousand lines of driver
    entry points, batch routes, provider metering and Hebrew versification to
    reconcile a Greek chapter. That file says of itself that every line is
    briefing a coordinator pays for and an aligner does not need; this session
    is neither. What it actually needs is the testament contract it may be
    about to amend, the chapter's own conventions, the report it is asked to
    judge, and the commands it will run — all of which are here.

    "Here" has to include the write commands, not just the two read-only
    views. The first version of this render named mark-reviewed and
    record-convention as steps while forbidding the two documents that give
    their invocations, and a session spent four calls guessing subcommand
    names off a make target and grepping the tree for the rest. A briefing
    that closes the door on its own reference must carry what was behind it.

    The report is included rather than cited. Citing it is what the driver's
    prompt used to do, and the file is written by `evaluate-alignment`, which
    runs in `make finish` — afterwards. An instruction that is false at the
    moment it is read costs a session four calls to discover that.

    The candidate table is included for the same reason, and it was learned the
    same way twice: a session told to *run* reconcile-view ran it, piped it
    through `tail` to keep it small, lost the head of the table, and ran the
    whole recomputation again into a file to get it back. Naming a command is
    an invitation to page it. This render runs the view itself, so the table
    arrives whole and the dump is on disk with its path spelled out before the
    session can wonder where it went.
    """
    report = root / f"reports/{scope.slug}-validation.md"
    try:
        view = write_view(root, scope.ref)
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as error:
        view = {"error": str(error)}
    dump = view.get("dump", f"data/output/{scope.slug}/reconcile-dump.txt")
    lines = render_stable_prefix(root, scope)
    lines.extend([
        f"=== {scope.ref} reconciliation pass — the whole chapter, one session",
        "  You are the coordinating reconciliation session. A `make drive` run",
        "  is waiting on you and will run the finishing checks and the commit",
        "  itself, so do",
        "  neither. Nothing else is writing to this chapter while you work.",
        "  This render is the complete briefing. Do not read AGENTS.md or",
        "  README.md: they are process and machinery for a coordinator, and",
        "  nothing in them governs the",
        "  judgments below.",
        "  Every other pass in this project sees one verse. This one sees the",
        "  chapter, because a split between two verses is invisible from inside",
        "  either of them.",
        "  This briefing is long enough that a harness may hand it to you as a",
        "  path to a saved file rather than as text. If so, read that file",
        "  directly and page it: never `cat` it, which only saves a second copy",
        "  and costs a call for nothing, and do not re-run the render. Ask for",
        "  its line count once, then read it in explicit offset/limit chunks",
        "  sized to your reader's cap. Six small reads and a stray `cat` is the",
        "  observed failure; two sized reads is the job.",
        "",
        "=== Context discipline: delegate bounded investigations",
        "  You are the only context given the whole chapter. When two or more",
        "  candidate groups or corpus checks can be",
        "  investigated independently, delegate each as a fresh, bounded,",
        "  read-only investigation. For Codex use fork_turns: none; for Claude",
        "  start a fresh subagent. Never reuse a completed agent for another",
        "  investigation, and launch independent investigations in parallel where",
        "  the runtime permits.",
        "  Give each agent only its exact question, the relevant references or",
        "  excerpts, and the read-only commands it needs. Do not hand it this whole",
        "  briefing, the chapter dump, or your transcript. Ask it to return a short",
        "  finding, its supporting references or evidence, and a recommendation —",
        "  not its reading or transcript.",
        "  Investigation agents must not edit alignments, validate or mark reviews,",
        "  record conventions, alter a contract, run finishing checks, or commit.",
        "  You synthesize their findings, make every final judgment, and remain the",
        "  sole writer. Apply and validate changes serially. If a question cannot be",
        "  bounded, delegation is unavailable, or the answer is already immediate",
        "  from this briefing, handle it directly rather than spawning work for its",
        "  own sake.",
        "  A prompt that lands in one round trip carries: the competing readings",
        "  written out as rows — status, English, head, and each side's",
        "  existing note verbatim — the contract rules the question turns on,",
        "  pasted as TEXT and never as bare numbers;",
        "  the corpus-query filters, pasted, with the first few queries written",
        "  out; and NOTES=1 with the reason it matters, that a precedent whose",
        "  note concedes the other reading is weaker evidence than one that does",
        "  not. Tell it to verify the rows you quote rather than trust them: they",
        "  are your hypothesis, and the corpus has overturned them before. Ask it",
        "  to flag any neighbouring inconsistency it noticed but was not asked",
        "  about, and settle that with a fresh agent rather than from the",
        "  side-remark.",
        "  Paste the rule text because you already hold the whole contract and the",
        "  agent does not. A number makes it open a file of a hundred long rules to",
        "  recover six, and every agent you send pays that read again; quoting the",
        "  six costs you a few hundred tokens once. This is the largest avoidable",
        "  cost in a reconciliation, so cite a number only alongside the text, as a",
        "  label the agent can quote back to you.",
        "  Give each contested unit to exactly ONE agent. Two prompts that both",
        "  name the same row buy one finding twice: a preposition question and a",
        "  reference-comparison question can easily both reach for it, so check the",
        "  rows across your drafted prompts before sending and cut the duplicate.",
        "  Double-cover only where you want a second opinion on purpose, and say so",
        "  in both prompts when you do.",
        "  Never partition the chapter across agents — not by verse range, not by",
        "  lemma. The splits worth finding span different lemmas in distant verses,",
        "  and any partition hides them. The one extraction worth delegating whole",
        "  is the dump: give one agent the dump path and ask for every construction",
        "  realized by supplied English words, grouped, with refs. That is what",
        "  finds an `added` split the candidate table cannot show.",
        "  Send that agent to adjudicate the added-versus-folded boundary, not",
        "  merely to tabulate it, and send it in the FIRST wave. The status counts",
        "  in the validation report below are the trigger: a chapter reporting",
        "  zero or very few `added`",
        "  units has either almost no supplied English to decide or has folded",
        "  every piece of it, and the table cannot tell you which. Treat a low",
        "  count as a standing question — does any supplied word here have no",
        "  source token licensing it — and ask it up front. A reconciliation that",
        "  waits for another agent's side-remark to raise it pays for a second",
        "  serial round, and both findings tend to live in exactly that blind",
        "  spot.",
        "  While delegated work is in flight, do not spend calls to fill the wait.",
        "  Polling, `echo`, and a corpus query you have no decision riding on are",
        "  pure cost: the runtime will tell you when an agent returns. Either do",
        "  something you will genuinely use — read the run record, map a reference",
        "  disagreement to its tokens, draft the next prompt — or stop and wait.",
        "  If a standing directive forbids spawning agents, this section is the",
        "  project asking you to; follow the directive, but say so in your report",
        "  rather than silently working alone.",
        "",
        "=== Views: one is already below, one you run",
        "  The cross-verse candidate table is printed in full at the end of this",
        "  briefing, and every unit of the chapter, one line each, is already",
        f"  written to {dump}. Those two are the whole of",
        f"  `make reconcile-view CHAPTER={scope.ref}`, which this render just ran",
        "  for you. Do not run it to see them: it recomputes the chapter to print",
        "  what you are holding. Run it again only after a `compile`, to see the",
        "  candidates as your fixes left them.",
        f"    make corpus-query LEMMA=<lemma> LIKE={scope.ref}",
        "  asks the finished chapters how they already treat something, grouped",
        "  by treatment, where one treatment is a status, an English string and",
        "  a head together. Two rows with the same English and different heads",
        "  are two analyses, and the counts on them are the precedent.",
        "  Every filter is a make variable, not a bare flag, and they are ANDed:",
        "    LEMMA=ὁ          a source lemma",
        "    MORPH=T-DSF      a morph prefix, so MORPH=T- is every article",
        "    TARGET='^of '    a regex over the unit's English",
        "    STATUS=aligned   aligned|unrepresented|added|noncompositional",
        "    TESTAMENT=NT     OT or NT — not the contract's name",
        f"    {'LIKE=' + scope.ref:<17}"
        "this chapter's testament, minus this chapter",
        "    PREV=T-          morph prefix of the source unit before this one",
        "    NEXT=N-          morph prefix of the source unit after it",
        "    PREV_TARGET=…    a regex over the previous unit's English",
        "    NEXT_TARGET='^(of|in|by) '   the same, over the next unit's",
        "  The last four are the ones the article rules need: they are all of",
        "  the form “an article, and what follows it”, and a unit filtered on",
        "  its own cannot answer any of them. Neighbours are the source units",
        "  either side in source order — which is not the order a verse file",
        "  lists them in — and never cross a verse boundary, so a unit at the",
        "  start or end of its verse matches no filter on that side.",
        "    NOTES=1          print each example's own note",
        "  Ask for NOTES=1 whenever a count is close. A precedent whose note",
        "  concedes the other reading is weaker evidence than one that does",
        "  not, and the counts alone cannot tell you which kind a row is.",
        "    make verse-view REF=PHP.2.11",
        "  shows one verse — its source, its ASV, and every accepted unit with",
        "  its note — in any chapter, which is what a corpus query's list of",
        "  refs is for. Reading precedent this way does not count as reopening",
        "  a finished chapter: it writes nothing.",
        "  All three views are read-only.",
        "",
        "=== Where this chapter's files are",
        "  Same reason as the commands below: this session cannot read",
        f"  AGENTS.md, and `{scope.ref}` is nowhere in the paths it would have",
        "  learned there. Relative to the repo root:",
        f"    data/alignments/{scope.slug}/verses/NN.json      edit these",
        f"    data/output/{scope.slug}/verses/NN.json          the packet",
        f"    data/alignments/{scope.slug}/reviews/NN.json     receipts",
        f"    data/alignments/{scope.slug}/conventions.md      conventions",
        f"    data/alignments/{scope.slug}.jsonl               compiled — never edit",
        "  NN is the verse, zero-padded to two digits and wider past 99. An",
        "  alignment file carries token identifiers and no text, and the packet",
        "  carries the text, so neither is readable alone — which is what the",
        "  dump and verse-view are for, and the reason to reach for a JSON file",
        "  only to change one; its top-level key is `alignments`, not `units`. The",
        "  compiled `.jsonl` is output: editing it is",
        "  overwritten by the next compile and changes nothing meanwhile.",
        "  Every other chapter sits at the same paths under its own name, so a",
        "  subagent sent to read precedent needs these lines as much as you do.",
        "",
        "=== Commands that write, in full",
        "  AGENTS.md and the align-chapter skill carry these, and this session",
        "  is told not to read either, so they are here. From the repo root:",
        "    PYTHONPATH=src python3 scripts/generate_alignments.py \\",
        "        validate-verse REF",
        "    PYTHONPATH=src python3 scripts/generate_alignments.py \\",
        "        mark-reviewed REF --note \"...\"",
        "    PYTHONPATH=src python3 scripts/generate_alignments.py \\",
        "        record-convention REF \"...\"",
        "    PYTHONPATH=src python3 scripts/generate_alignments.py \\",
        f"        compile {scope.ref}",
        "  A verse you changed takes the first two in that order; mark-reviewed",
        "  refuses an invalid alignment itself, but validate-verse says why in a",
        "  readable form. Editing is serial and one verse at a time, and so is",
        "  the validate-verse that follows each edit — that pairing is what keeps",
        "  a bad write from travelling. The bookkeeping is not: once every edit",
        "  is in, the mark-reviewed calls for all touched verses, and likewise",
        "  the record-convention calls, can go in one shell invocation each",
        "  rather than one call apiece. compile rebuilds the ledger both views read, so run it",
        "  before trusting reconcile-view or a corpus query again — otherwise",
        "  they answer from the chapter as it was before the fix.",
        "  mark-reviewed stamps the contract version that is live when it runs.",
        "  Re-review every verse you touched BEFORE bumping the version in task",
        "  2, or one receipt will claim a contract the chapter was not aligned",
        "  under while its twenty-odd siblings name the real one.",
        "  That version can move without you: another chapter's reconciliation",
        "  may be editing config/ while you work. So finish every mark-reviewed",
        "  before touching the contract at all, and build the task 2 edit from",
        "  the contract file as it reads then — parse its version and its highest",
        "  rule number rather than trusting the copy printed above, and assert on",
        "  what you parsed, so a collision fails loudly instead of appending a",
        "  duplicate rule number.",
        f"  Close by checking that every receipt in data/alignments/{scope.ref}/",
        "  reviews/ carries the one version named by aligned_under_contract_version",
        f"  in data/alignments/{scope.ref}-run.json. Repair a straggler in the",
        "  receipt itself; re-running mark-reviewed only stamps the version that",
        "  is live now.",
        "",
        "=== 1. Cross-verse consistency",
        "  Find any construction this chapter analysed two different ways.",
        "  Genesis 5 is the case to have in mind: eight verses had split on one",
        "  repeated noun, and no single verse was wrong on its own. Most",
        "  candidates are not splits — the ASV renders one word differently in",
        "  different contexts, and a difference the source licenses is not a",
        "  split. Where you find a real one, decide it, apply the fix to every",
        "  verse it touches, re-validate each, re-run mark-reviewed for those",
        "  verses, and record the decision with record-convention — the four",
        "  command lines are above, under “Commands that write”.",
        "  The report below lists every note text that recurs across verses as a",
        "  chapter-wide judgment that reached later verses unrecorded. That count",
        "  is mechanical: most repeats are verse passes each restating a contract",
        "  rule they applied independently, and the contract already binds every",
        "  verse, so those need no record-convention. For each one, ask which rule",
        "  it restates; if none does, record it. A conventions log padded with",
        "  restatements hides the judgment that actually needs carrying.",
        "",
        "=== 2. Contract promotion",
        "  If this chapter taught a rule that generalises, check it against the",
        "  corpus with corpus-query before writing it, then add it to this",
        "  testament's contract under config/ and bump the contract version.",
        "  Where it qualifies a rule already there, amend that rule in place and",
        "  keep its number instead of adding a new one: that is how most of this",
        "  contract was learned, and it is what keeps a correction from becoming",
        "  a cross-reference with the over-broad sentence still standing. A",
        "  rule that contradicts finished chapters is worse than no rule, and a",
        "  corpus that contradicts itself is a reason not to promote at all.",
        "  Editing the contract needs no recompilation: compilation never reads",
        "  it, and each chapter's run record keeps the version it was aligned",
        "  under. The version is declared once, in the contract's own opening",
        "  line, and everything else reads it from there — bump that line and",
        "  nothing else needs changing.",
        "  Before writing a rule, ask whether it exists only because the data",
        "  arrived in the wrong shape. The contract is for linguistic judgments; a",
        "  rule that describes a packet or versification limitation makes that",
        "  limitation permanent and teaches every later pass to reproduce it. The",
        "  pipeline already models such cases — GREEK_ASV_VERSE_MOVES and",
        "  GREEK_ASV_CLAUSE_MOVES, and source_ref/incoming on the Hebrew side — so",
        "  route the tokens there and leave the contract alone.",
        "",
        "=== 3. Report what you changed, in a few lines",
        "  Do not run the finishing checks and do not commit; the driver does",
        "  both. Do not reopen a finished chapter other than this one.",
        "  Never run `git status`, and never run `git diff` over the tree. This",
        "  repository sits on a WSL mount of a Windows filesystem, where either",
        "  takes minutes, and nothing you are asked to do needs one: you know",
        "  what you edited, and the driver stages and commits everything.",
        "",
    ])
    if "error" in view:
        lines.extend([
            f"=== {scope.ref} cross-verse candidates — not built",
            f"  {view['error']}",
            f"  Build the chapter, then run `make reconcile-view"
            f" CHAPTER={scope.ref}` once.",
            "",
        ])
    else:
        lines.extend([
            f"=== {scope.ref} cross-verse candidates (task 1) — the whole table",
            f"  {view['units']} units, {view['candidates']} candidate(s). Every unit,",
            f"  one line each, is in {dump}: grep it rather",
            "  than opening verse JSON. A line may carry a [medium] marker if",
            "  the verse was aligned before the confidence field was dropped;",
            "  it is historical, and nothing is owed on account of it.",
            "",
            *view["table"].rstrip().splitlines(),
            "",
        ])
    if report.exists():
        lines.extend([
            f"=== {scope.ref} validation report ({report.relative_to(root)})",
            *report.read_text(encoding="utf-8").rstrip().splitlines(),
            "",
        ])
    else:
        lines.extend([
            f"=== {scope.ref} validation report — not written yet",
            "  It is produced by evaluate-alignment, which reads the compiled",
            "  ledger. Build both before reconciliation:",
            f"    make compile-alignment CHAPTER={scope.ref}",
            f"    make evaluate-alignment CHAPTER={scope.ref}",
            "",
        ])
    return "\n".join(lines)


def render_stable_prefix(
    root: Path, scope: ChapterScope, conventions: bool = True
) -> list[str]:
    """Everything byte-identical for every verse of this chapter, first.

    A briefing is mostly its testament contract, and the contract is the same
    for every verse of every chapter in that testament. Ordering the invariant
    part ahead of the verse-specific part is what lets a provider's prompt
    cache hold it: a cache is a prefix match, so a verse number in the opening
    line would make every verse a fresh prefix and the contract would be paid
    for once per verse. The chapter's conventions come next because they vary
    per chapter but not per verse, which is the granularity a chapter-sized
    batch reuses.
    """
    lines = render_contract(root, scope)
    if conventions:
        lines.extend(render_conventions(root, scope))
    return lines


def render_api_task(scope: ChapterScope, verse: int, mode: str) -> list[str]:
    """State the task for a worker that has no filesystem and no tools.

    A programmatic caller writes the accepted file and runs the validator
    itself, so the shell mechanics in `render_pass_actions` are not merely
    unnecessary here — they are wrong, and a model that tried to follow them
    would report work it had no way to do.
    """
    ref = f"{scope.ref}.{verse}"
    lines = [f"=== Task — {ref} {PASS_NAMES[mode]}"]
    if mode == "review":
        lines.extend([
            "  Review the accepted alignment below against the contract and the",
            "  token table. Return one review object. If it is already right,",
            f'  return {{"ref":"{ref}","verdict":"unchanged",',
            '  "alignments":[],"review_notes":[]}. Do not reproduce its units.',
            "  If it is wrong, use verdict corrected and return the COMPLETE",
            "  corrected alignments array; the driver materializes and validates",
            "  the canonical accepted object. Put anything the next reader needs",
            "  into review_notes.",
            f"  {LARGE_SOURCE_REVIEW_CHECKLIST}",
        ])
    else:
        lines.extend([
            "  Align this verse under the contract above and return the",
            "  alignment as one JSON object.",
        ])
    lines.extend([
        "  This message is the complete briefing; there are no files to open and",
        "  no commands to run. Return only the JSON object.",
        "",
    ])
    return lines


def briefing_parts(
    root: Path = ROOT, chapter: str = "JHN.1", verse: int = 1,
    mode: str = "packet", conventions: bool = True,
) -> tuple[str, str]:
    """Split one verse's briefing into its cacheable and per-verse halves.

    The two are concatenated for a human or an agentic session and sent as a
    cached system block plus a user message for an API call. Splitting here
    rather than at the call site keeps one definition of what a pass is told.
    """
    return briefing_parts_many(root, chapter, [verse], mode, conventions)[verse]


def briefing_parts_many(
    root: Path, chapter: str, verses: list[int], mode: str = "packet",
    conventions: bool = True,
) -> dict[int, tuple[str, str]]:
    """Split several verse briefings after preparing their chapter once.

    Preparation verifies a content hash over every source input before using
    its packet cache. That freshness check belongs to the chapter operation,
    not to each verse in it: repeating it here would reread the same large
    comparison files once per job without making any result fresher.
    """
    scope = chapter_scope(chapter)
    packets = {
        verse_number(packet["ref"]): packet
        for packet in prepare_alignment(root, scope.ref)
    }
    unknown = sorted(set(verses) - set(packets))
    if unknown:
        if len(unknown) == 1:
            raise ValueError(f"no prepared packet for verse {unknown[0]}")
        raise ValueError(f"no prepared packet for verses {unknown}")
    stable = "\n".join(render_stable_prefix(root, scope, conventions))
    briefings: dict[int, tuple[str, str]] = {}
    for verse in verses:
        packet = packets[verse]
        variable = list(render_api_task(scope, verse, mode))
        variable.extend(render_packet(packet))
        if mode == "review":
            path = root / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
            if not path.exists():
                raise FileNotFoundError(f"no accepted alignment for {packet['ref']}")
            variable.extend(
                render_response(packet, json.loads(path.read_text(encoding="utf-8")))
            )
        briefings[verse] = stable, "\n".join(variable)
    return briefings


def render_verse_body(
    root: Path, scope: ChapterScope, packets: dict[int, dict[str, Any]],
    wanted: list[int], mode: str,
) -> list[str]:
    """Everything after the stable prefix: the pass, its actions, its verses.

    Named rather than inlined because a driven session is handed the two halves
    separately, and the halves have to stay the same text a reader of `make
    render` sees. Where the cut falls is the whole point: everything here either
    names a verse or follows something that does.
    """
    lines = list(render_session_notice(scope.ref, mode, wanted))
    lines.extend(render_pass_actions(scope, mode, wanted))
    if mode == "packet" and len(wanted) == 1:
        lines.extend(render_response_shape(f"{scope.ref}.{wanted[0]}"))
    for verse in wanted:
        packet = packets[verse]
        if mode in ("packet", "review", "both"):
            lines.extend(render_packet(packet))
        if mode in ("review", "both"):
            path = root / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
            if not path.exists():
                lines.append(f"=== {packet['ref']}  (no accepted alignment yet)")
            else:
                lines.extend(
                    render_response(packet, json.loads(path.read_text(encoding="utf-8")))
                )
        lines.append("")
    return lines


def verse_session_parts_many(
    root: Path, chapter: str, verses: list[int], mode: str = "packet",
    conventions: bool = True,
) -> tuple[str, dict[int, str]]:
    """Split a pass's session briefings into one cacheable half and a verse each.

    Concatenated, a verse's two halves are exactly what `make render` prints for
    it: this is a routing decision about where each byte travels, not a second
    briefing that could drift from the first. The contract and the chapter's
    conventions are about four fifths of a verse briefing and are identical for
    every verse of the chapter, so the driver sends them once as a system prompt
    — consecutive sessions share that prefix cache and only the first pays to
    write it, which is what the lone cache-priming verse is already for.
    The rest names its verse and travels in the prompt, where it costs the
    session nothing to have been handed rather than to have rendered.

    One stable half is returned for the whole pass rather than one per verse:
    there is only one, and a shape that let a caller pair verse 7's contract
    with verse 8's packet would invite exactly that.
    """
    scope = chapter_scope(chapter)
    packets = {
        verse_number(packet["ref"]): packet
        for packet in prepare_alignment(root, scope.ref)
    }
    unknown = sorted(set(verses) - set(packets))
    if unknown:
        if len(unknown) == 1:
            raise ValueError(f"no prepared packet for verse {unknown[0]}")
        raise ValueError(f"no prepared packet for verses {unknown}")
    stable = "\n".join(render_stable_prefix(root, scope, conventions))
    bodies = {
        verse: "\n".join(render_verse_body(root, scope, packets, [verse], mode))
        for verse in verses
    }
    return stable, bodies


def verse_task_parts_many(
    root: Path, chapter: str, verses: list[int], mode: str = "packet",
    conventions: bool = True,
) -> tuple[str, dict[int, str]]:
    """A tool-free pass's briefings, in the shape a driven pass is given them.

    The same two halves `briefing_parts_many` builds for a provider request,
    regrouped: one stable half for the whole pass and one body per verse, which
    is what `verse_session_parts_many` returns and therefore what the driver
    already knows how to send. Nothing is rendered differently — a verse told
    it has no tools is told that once, in `render_api_task`, whether the request
    goes to a batch endpoint or to `claude -p --tools ""`.

    Regrouped rather than rebuilt because the stable half is the same string for
    every verse by construction, and a shape that returned one per verse would
    invite pairing verse 7's contract with verse 8's packet — the objection
    `verse_session_parts_many` already answers.
    """
    briefings = briefing_parts_many(root, chapter, verses, mode, conventions)
    if not briefings:
        return "", {}
    stable = next(iter(briefings.values()))[0]
    return stable, {verse: variable for verse, (_, variable) in briefings.items()}


def packed_evidence_parts_many(
    root: Path, chapter: str, verses: list[int], mode: str = "packet",
    conventions: bool = True,
) -> tuple[str, dict[int, str]]:
    """The shared contract and conflict-free evidence for a packed request.

    `verse_task_parts_many` correctly tells each ordinary request to return one
    object. Concatenating those task instructions would make a packed request
    ask for several single outer objects and one wrapper at the same time. This
    shares the exact packet and accepted-alignment rendering while leaving the
    one packed task instruction to its caller.
    """
    if mode not in ("packet", "review"):
        raise ValueError(f"unknown mode {mode}; expected packet or review")
    scope = chapter_scope(chapter)
    packets = {
        verse_number(packet["ref"]): packet
        for packet in prepare_alignment(root, scope.ref)
    }
    unknown = sorted(set(verses) - set(packets))
    if unknown:
        if len(unknown) == 1:
            raise ValueError(f"no prepared packet for verse {unknown[0]}")
        raise ValueError(f"no prepared packet for verses {unknown}")
    stable = "\n".join(render_stable_prefix(root, scope, conventions))
    evidence: dict[int, str] = {}
    for verse in verses:
        packet = packets[verse]
        lines = [f"=== Verse evidence — {packet['ref']}"]
        lines.extend(render_packet(packet))
        if mode == "review":
            path = root / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"
            if not path.exists():
                raise FileNotFoundError(f"no accepted alignment for {packet['ref']}")
            lines.extend(
                render_response(packet, json.loads(path.read_text(encoding="utf-8")))
            )
        evidence[verse] = "\n".join(lines)
    return stable, evidence


def verse_session_parts(
    root: Path = ROOT, chapter: str = "JHN.1", verse: int = 1,
    mode: str = "packet", conventions: bool = True,
) -> tuple[str, str]:
    """One verse's session briefing, in its two halves."""
    stable, bodies = verse_session_parts_many(
        root, chapter, [verse], mode, conventions,
    )
    return stable, bodies[verse]


def render_chapter(
    root: Path = ROOT, chapter: str = "JHN.1", verses: str | None = None,
    mode: str = "packet", conventions: bool = True,
) -> str:
    """Render a range as a self-contained briefing.

    A worker should need this output and nothing else: the chapter-wide
    judgments taken so far, then only the verses in its own range. Bounding
    what a verse pass has to carry is the point — an ever-growing transcript
    is what makes a long chapter expensive, whether or not it is delegated.
    """
    scope = chapter_scope(chapter)
    packets = {
        verse_number(packet["ref"]): packet
        for packet in prepare_alignment(root, scope.ref)
    }
    wanted = parse_verses(verses, sorted(packets))
    lines: list[str] = render_stable_prefix(root, scope, conventions)
    lines.extend(render_verse_body(root, scope, packets, wanted, mode))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render prepared packets or accepted alignments compactly"
    )
    parser.add_argument("chapter")
    parser.add_argument(
        "--verses", help="a verse, a range such as 1-8, or a comma-separated list"
    )
    parser.add_argument(
        "--mode", choices=["packet", "review", "both", "decisions", "reconcile"],
        default="packet",
        help="packet for alignment, review for accepted units, decisions for "
             "the chapter's unresolved textual units, reconcile for the "
             "whole-chapter pass that follows them",
    )
    parser.add_argument(
        "--no-conventions", action="store_true",
        help="omit the chapter conventions a worker would otherwise need",
    )
    args = parser.parse_args(argv)
    if args.mode in ("decisions", "reconcile"):
        # Chapter-level briefings, so --verses has nothing to select and a
        # caller who passed one has misunderstood the mode rather than narrowed
        # it. The decisions file, and the chapter itself, are the work list.
        if args.verses:
            parser.error(
                f"--mode {args.mode} renders the whole chapter; --verses does not apply"
            )
        render = render_decisions if args.mode == "decisions" else render_reconciliation
        try:
            print(render(ROOT, chapter_scope(args.chapter)))
        except (ValueError, FileNotFoundError) as error:
            parser.error(str(error))
        return 0
    try:
        print(render_chapter(
            ROOT, args.chapter, args.verses, args.mode,
            conventions=not args.no_conventions,
        ))
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    return 0

"""Reconcile a chapter as one bounded request instead of an agentic session.

Reconciliation is the largest line in a chapter's bill by a wide margin: 67% of
measured spend, about forty turns and 4.2M raw tokens, because a session that
holds a whole chapter re-reads that chapter at every turn it takes. None of
those re-reads is the judgment. What the judgment needs is a chapter's worth of
evidence *once*, and what the chapter needs back is a small set of validated
patches.

So this module is the other half of the boundary the verse passes already
keep. Machinery prepares one packet — the contract, the conventions, the
cross-verse candidate table, the validation report, and every verse it can
afford to show with its token identifiers — and a single high-effort request
returns a no-change verdict or complete corrected payloads. The parent
validates each patch against the same ledger validator a verse pass answers to,
writes it, and marks the receipt. Nothing here trusts the model with a file.

A promotion is applied here, and that is a reversal. It used to be returned as
a recommendation and never written, on the reasoning that a contract edit binds
every chapter aligned afterwards and so belongs to a person. The person was
reached by a queue entry the driver rewrote on every finished chapter; when two
drivers were consolidated the surviving one cleared that entry instead, and the
recommendation quietly had nowhere to go. Sixty-three chapters then reconciled
without a rule reaching the contract. A recommendation with no reader is not a
safeguard, so the write is now real, bounded by what `promote_contract` will
accept and recorded beside the run that made it.

One thing is still deliberately not automatic: an evidence request is answered
*once*, from machinery — a verse's token table, or a corpus query the same
filters a session would have typed — and the chapter is then re-asked. One
round, because a second is how a bounded request grows back into a session.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Callable, Iterable

from .alignment import (
    mark_reviewed,
    promote_contract,
    record_convention,
    validate_response,
)
from .pipeline import ROOT
from .reconciliation import (
    Unit,
    chapter_units,
    corpus_units,
    divergences,
    matches,
    render_corpus_query,
    render_divergences,
    render_dump,
    render_verse,
)
from .render import (
    render_packet,
    render_response,
    render_stable_prefix,
    verse_number,
)
from .scope import ChapterScope, chapter_scope

RESPONSE_SCHEMA = "config/reconciliation-response.schema.json"

#: What the whole chapter half of one request may take up, in characters.
#: About 120,000 tokens, which leaves the contract in the system prompt, the
#: reasoning, and the response inside a 200k window with room to spare. It is a
#: character budget rather than a count of anything because what it bounds is
#: one request's context, and a Hebrew verse and a Greek one are not the same
#: size.
#:
#: Everything below is spent out of it in a fixed order, most load-bearing
#: first: the candidate table is the evidence the pass exists for, the report
#: is what it is asked to judge, precedent decides promotions, and the verse
#: bodies are what make a patch writable at all. A chapter whose bodies do not
#: all fit gets the compact dump instead, so that a split in a verse it cannot
#: patch is still a split it can see and ask about.
PACKET_BUDGET = 480_000
DIVERGENCE_BLOCK = 100_000
REPORT_BLOCK = 24_000
PRECEDENT_TOTAL = 48_000

#: Per-verse detail, when a chapter is small enough to show whole. Kept as its
#: own name because `render_verse_bodies` is also called with whatever the
#: packet has left, and a caller reading only that would not know there was a
#: default.
BODY_BUDGET = 350_000

#: Corpus precedent is included for the candidate lemmas, commonest first, and
#: stops here. A chapter with sixty candidate lemmas is not a chapter where the
#: sixtieth decides anything, and a packet that grows with the corpus stops
#: being one request.
PRECEDENT_LEMMAS = 12
PRECEDENT_EXAMPLES = 4

#: What one lemma's precedent may take up in the packet. A Hebrew preposition
#: has thousands of accepted units across hundreds of treatments, and printed
#: whole it is a third of a request for one candidate. So the packet carries
#: the commonest treatments, counted, without their notes, and says what it
#: cut: a reconciler that needs the tail — or the notes, which decide a close
#: count — asks for it with a narrower `corpus` request, and gets it in full.
PRECEDENT_BLOCK = 6_000
PACKET_EXAMPLES = 2

#: How many evidence requests one escalation round will answer. A reconciler
#: that wants more than this has not been given a packet; it has been given a
#: research project, and the right answer is to look at the chapter by hand.
ANSWERED_REQUESTS = 8

#: The filters a corpus request may use, spelled exactly as the make variables
#: a session would have typed, so the packet can describe them in the words the
#: existing views already use.
CORPUS_FILTERS = {
    "LEMMA": "lemma", "MORPH": "morph", "TARGET": "target", "STATUS": "status",
    "PREV": "prev_morph", "NEXT": "next_morph",
    "PREV_TARGET": "prev_target", "NEXT_TARGET": "next_target",
}


def accepted_path(root: Path, scope: ChapterScope, verse: int) -> Path:
    return root / f"data/alignments/{scope.slug}/verses/{verse:02d}.json"


def packet_path(root: Path, scope: ChapterScope, verse: int) -> Path:
    return root / f"data/output/{scope.slug}/verses/{verse:02d}.json"


def candidate_verses(units: list[Unit]) -> list[int]:
    """The verses a cross-verse candidate touches, commonest lemma first.

    This is the order the per-verse detail is spent in, so it is the order the
    candidate table itself is in: a lemma analysed six ways across six verses
    is where a split actually lives, and a chapter too long to show whole
    should spend what it has there.
    """
    ordered: list[int] = []
    for _, group in divergences(units):
        for unit in group:
            verse = verse_number(unit.ref)
            if verse not in ordered:
                ordered.append(verse)
    return ordered


def render_verse_bodies(
    root: Path, scope: ChapterScope, wanted: Iterable[int],
    budget: int = BODY_BUDGET,
) -> tuple[list[str], list[int]]:
    """Every verse this packet can afford, with its token identifiers.

    A patch is a complete verse payload, so a verse the reconciler cannot see
    by identifier is a verse it cannot correct — it can only describe the
    problem and ask. That is why the budget is spent on candidate verses first
    and why what did not fit is named rather than dropped silently: `requests`
    exists to buy back exactly this, and a reconciler that does not know what
    it is missing cannot ask for it.
    """
    lines: list[str] = []
    shown: list[int] = []
    spent = 0
    for verse in wanted:
        packet_file = packet_path(root, scope, verse)
        accepted_file = accepted_path(root, scope, verse)
        if not packet_file.exists() or not accepted_file.exists():
            continue
        packet = json.loads(packet_file.read_text(encoding="utf-8"))
        accepted = json.loads(accepted_file.read_text(encoding="utf-8"))
        body = [*render_packet(packet), *render_response(packet, accepted), ""]
        size = sum(len(line) + 1 for line in body)
        if spent and spent + size > budget:
            continue
        lines.extend(body)
        shown.append(verse)
        spent += size
    return lines, sorted(shown)


def render_precedents(
    root: Path, scope: ChapterScope, units: list[Unit],
    lemmas: int = PRECEDENT_LEMMAS, total: int = PRECEDENT_TOTAL,
    corpus: Callable[..., tuple[list[Unit], list[str]]] = corpus_units,
) -> list[str]:
    """What the finished chapters already do with this chapter's candidates.

    The corpus check is the one piece of reconciliation that was reliably
    skipped when it had to be typed, and a rule that contradicts a finished
    chapter is worse than no rule. Run here, once, for the candidate lemmas
    rather than on request, because a request costs a whole extra round and
    this costs a table.
    """
    wanted = [key for key, _ in divergences(units)][:lemmas]
    if not wanted:
        return []
    spent = 0
    found, skipped = corpus(root, testament=scope.testament, exclude=scope.ref)
    lines = [
        f"=== Corpus precedent for {scope.ref}'s candidate lemmas",
        "  How the finished chapters of this testament already treat each",
        "  candidate, grouped by treatment: a status, an English string and a",
        "  head together. Two rows with the same English and different heads",
        "  are two analyses, and the counts on them are the precedent. This",
        f"  chapter is excluded from every count. {len(found)} unit(s) searched"
        + (f", {len(skipped)} chapter(s) unreadable and skipped." if skipped
           else "."),
        "",
    ]
    for key in wanted:
        for lemma in key:
            if spent >= total:
                lines.append(
                    "  Precedent for the remaining candidates is not shown; ask "
                    "for it with a corpus request."
                )
                return lines
            hits = list(matches(found, lemma=lemma))
            block = trim(render_corpus_query(
                hits, skipped, examples=PACKET_EXAMPLES,
                described=f"LEMMA={lemma}", notes=False,
            ), min(PRECEDENT_BLOCK, total - spent), f"LEMMA={lemma}")
            lines.extend(block)
            lines.append("")
            spent += sum(len(line) + 1 for line in block)
    return lines


def trim(block: str, budget: int, described: str) -> list[str]:
    """Cut a precedent table at a line boundary, and say that it was cut.

    Silence here would be the one failure this table cannot survive: the rows
    are commonest first, so a quietly truncated query reads as a unanimous
    corpus. What is dropped is named, with the request that buys it back.
    """
    lines = block.rstrip().splitlines()
    kept: list[str] = []
    spent = 0
    for line in lines:
        if spent + len(line) + 1 > budget:
            kept.extend([
                f"  … {len(lines) - len(kept)} further line(s) of this table are",
                "  not shown. The rows above are the commonest treatments, so",
                "  what is missing is the tail, not the shape. Ask for it with a",
                f"  narrower corpus request — {described} plus MORPH, STATUS,",
                "  TARGET or a neighbour filter — and it comes back in full,",
                "  with each example's own note.",
            ])
            break
        kept.append(line)
        spent += len(line) + 1
    return kept


def render_task(scope: ChapterScope, shown: list[int], missing: list[int]) -> list[str]:
    """What the request is for, and the exact shape of what may come back."""
    lines = [
        f"=== {scope.ref} reconciliation — one request, no tools",
        "  Every other pass in this project sees one verse. This one sees the",
        "  chapter, because a split between two verses is invisible from inside",
        "  either of them. Below is the whole briefing: the binding contract,",
        "  this chapter's recorded conventions, the cross-verse candidate table,",
        "  the validation report, corpus precedent for the candidates, and the",
        "  verses themselves with their token identifiers. There are no files to",
        "  open and no commands to run. Return one JSON object.",
        "",
        "  What to decide, in order:",
        "  1. Cross-verse consistency. Find any construction this chapter",
        "     analysed two different ways. Most candidates are not splits — the",
        "     ASV renders one word differently in different contexts, and a",
        "     difference the source licenses is not a split. Where you find a",
        "     real one, return a patch for every verse it touches.",
        "  2. Conventions. Record a chapter-wide judgment that a later reader",
        "     would otherwise have to rediscover. A judgment that merely",
        "     restates a contract rule needs no convention: the contract already",
        "     binds every verse, and a log padded with restatements hides the",
        "     one that matters.",
        "  3. Contract promotion. If this chapter taught a rule that",
        "     generalises and the precedent below agrees, write it — either as",
        "     a new rule, or as an amendment to one already there. Prefer the",
        "     amendment. Most of what this contract knows was learned that way:",
        "     a rule met the case it had not considered and was widened or",
        "     narrowed where it stands. A qualification returned as a new rule",
        "     pointing back at an old one leaves the over-broad sentence in",
        "     place for whoever reads it first, and the contract grows a",
        "     cross-reference where it needed a correction. An amendment",
        "     replaces that rule's text whole, so return the entire rule as it",
        "     should now read, not the clause you are adding. What you",
        "     return is appended verbatim to the binding contract you were given",
        "     above, and binds every chapter aligned after this one, so write it",
        "     as a rule and not as a description of one: the wording you send",
        "     is the wording a verse pass will read, with no one in between to",
        "     ask what it meant. Give it the numbered rules' own voice, cite by",
        "     number the rules it qualifies, and do not number it yourself.",
        "     Three things are not promotions, and each has cost this project a",
        "     rule it had to live with. A judgment that restates a rule already",
        "     in the contract is not one. A rule the corpus below contradicts",
        "     is worse than no rule at all, and a corpus that contradicts",
        "     itself is a reason not to promote. And a rule that exists only",
        "     because the data arrived in the wrong shape makes that shape",
        "     permanent and teaches every later pass to reproduce it — the",
        "     pipeline models those cases elsewhere, so leave the contract",
        "     alone. Most chapters teach nothing general: an empty array is the",
        "     ordinary answer and costs nothing, while a weak rule is paid for",
        "     by every chapter that follows.",
        "",
        "  The response object, exactly:",
        '    ref            "' + scope.ref + '"',
        "    verdict        no-change | changes | needs-evidence",
        "    report         a few lines a later reader can act on",
        "    patches        [] or one entry per verse you are correcting:",
        "                     ref, reason, review_notes, and the COMPLETE",
        "                     alignments array for that verse — every source and",
        "                     target id partitioned exactly once, as the verse",
        "                     pass returned it. A patch is written and validated",
        "                     verbatim; a partial one is rejected.",
        "    conventions    [] or {ref, text} entries to append to the log",
        "    promotions     [] or {summary, basis, rule} entries, each a NEW",
        "                   rule. rule is the contract text itself, unnumbered;",
        "                   summary and basis say what it settles and what",
        "                   evidence carried it, and are recorded beside the",
        "                   edit rather than written into the contract",
        "    amendments     [] or {number, rule, why} entries, each rewriting a",
        "                   rule the contract already has. number is that",
        "                   rule's own number and never changes — rules cite",
        "                   each other by number, so a number is an address.",
        "                   rule is the complete replacement text, unnumbered.",
        "                   Amending a rule you did not read in full is how a",
        "                   hard-won qualification gets dropped, so quote the",
        "                   whole of it back",
        "    requests       [] or {kind, value, why} entries, and verdict",
        "                   needs-evidence. kind is `verse` with a reference",
        "                   such as " + f"{scope.ref}.4" + ", or `corpus` with",
        "                   filters written as make variables, ANDed:",
        "                     LEMMA=ὁ MORPH=T- STATUS=aligned TARGET='^of '",
        "                     PREV=T- NEXT=N- PREV_TARGET=… NEXT_TARGET=…",
        "                   They are answered mechanically and the chapter is",
        "                   asked once more. Ask only for what would change a",
        "                   judgment; there is exactly one such round.",
        "",
        "  A chapter that is already consistent returns verdict no-change, an",
        "  empty patches array, and a report saying what you checked. That is a",
        "  complete and expected answer, and it is the common one.",
        "",
    ]
    if missing:
        lines.extend([
            "  Not every verse fits in one packet. Shown with identifiers below:",
            f"    {', '.join(str(verse) for verse in shown)}",
            "  Present in the candidate table and the dump but not shown with",
            "  identifiers:",
            f"    {', '.join(str(verse) for verse in missing)}",
            "  Ask for one of those with a `verse` request rather than writing a",
            "  patch you cannot address by identifier.",
            "",
        ])
    return lines


def measure(lines: Iterable[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def adjudication_parts(
    root: Path, chapter: str, report: str | None = None,
    budget: int = PACKET_BUDGET,
    corpus: Callable[..., tuple[list[Unit], list[str]]] = corpus_units,
) -> tuple[str, str]:
    """One chapter's reconciliation packet, in its two halves.

    Split for the reason every other briefing here is: the contract and the
    chapter's conventions are the same bytes for every chapter of a testament,
    so they travel as the system prompt where a prefix cache holds them, and
    everything that names this chapter travels in the prompt.

    The chapter half is assembled against a budget rather than taken as it
    comes. A 176-verse psalm would otherwise build a packet no window holds,
    and the failure would arrive as a refused request after the chapter had
    been aligned and reviewed. What varies with the chapter's size is only how
    much per-verse detail it can carry; what never varies is that the packet
    says which verses it left out.
    """
    scope = chapter_scope(chapter)
    units = chapter_units(root, scope.ref)
    ordered = candidate_verses(units)
    # Candidate verses first, then the rest in order: the budget is spent
    # top-down, and a chapter too long to show whole should spend it where a
    # split actually lives.
    for verse in sorted(
        int(path.stem)
        for path in (root / f"data/alignments/{scope.slug}/verses").glob("*.json")
    ):
        if verse not in ordered:
            ordered.append(verse)

    table = trim(
        render_divergences(scope.ref, units), DIVERGENCE_BLOCK, "the candidate table",
    )
    validation = (
        [f"=== {scope.ref} validation report",
         *trim(report, REPORT_BLOCK, "the validation report"), ""]
        if report else []
    )
    precedent = render_precedents(
        root, scope, units, total=PRECEDENT_TOTAL, corpus=corpus,
    )
    left = budget - measure(table) - measure(validation) - measure(precedent)

    whole, shown_whole = render_verse_bodies(root, scope, ordered, budget=left)
    if len(shown_whole) == len(ordered):
        dump: list[str] = []
        bodies, shown = whole, shown_whole
    else:
        # Every unit of the chapter, one line each and no identifiers. It costs
        # about a fifth of what the bodies cost and it is what lets a
        # reconciler notice a split in a verse it cannot patch — which is the
        # difference between asking for that verse and never knowing to.
        dump = render_dump(scope.ref, units).splitlines()
        bodies, shown = render_verse_bodies(
            root, scope, ordered, budget=max(left - measure(dump), 0),
        )
    missing = [verse for verse in sorted(ordered) if verse not in shown]

    variable = render_task(scope, shown, missing)
    variable.extend(table)
    variable.append("")
    variable.extend(validation)
    if dump:
        variable.extend(dump)
        variable.append("")
    variable.extend(precedent)
    variable.extend(bodies)
    stable = "\n".join(render_stable_prefix(root, scope))
    return stable, "\n".join(variable)


def parse_corpus_request(value: str) -> dict[str, str]:
    """Read `LEMMA=ὁ NEXT=N-` into the filters `matches` takes.

    Unknown keys are dropped rather than refused. The request came from a model
    that was shown the list, and a query answered with the filters it did get
    is more useful than a round spent saying no — while a filter nobody
    implements must never silently narrow a result, which is why it is dropped
    and named in the answer rather than passed through.
    """
    filters: dict[str, str] = {}
    try:
        pieces = shlex.split(value)
    except ValueError:
        # An unbalanced quote. Splitting on whitespace loses a trailing space
        # inside a regex, which is a filter that means something else — so the
        # fallback is the same reading, minus the quoting it could not parse.
        pieces = value.split()
    for piece in pieces:
        key, _, argument = piece.partition("=")
        field = CORPUS_FILTERS.get(key.strip().upper())
        if field and argument:
            filters[field] = argument
    return filters


def answer_requests(
    root: Path, chapter: str, requests: list[dict[str, str]],
    limit: int = ANSWERED_REQUESTS,
    corpus: Callable[..., tuple[list[Unit], list[str]]] = corpus_units,
) -> str:
    """Answer evidence requests from machinery, once.

    A `verse` request is a verse's token table and accepted units — the same
    view the packet shows, for a verse the budget could not fit. A `corpus`
    request is the query a session would have typed. Neither needs a model, so
    neither gets one: this is the escalation the design allows, and it is a
    function call.
    """
    scope = chapter_scope(chapter)
    lines = [
        f"=== Answers to your {min(len(requests), limit)} request(s) for {scope.ref}",
        "  This is the one evidence round. Decide from what is here and the",
        "  packet you already have, and return your final object: patches,",
        "  conventions, promotions, amendments, and a verdict.",
        "  Any request left unanswered below is one this run cannot answer;",
        "  say so in your report rather than asking again.",
        "",
    ]
    for request in requests[:limit]:
        kind = (request.get("kind") or "").strip()
        value = (request.get("value") or "").strip()
        lines.append(f"--- {kind} {value}")
        if kind == "verse":
            try:
                lines.extend(render_verse(root, value).splitlines())
            except (FileNotFoundError, KeyError, ValueError) as error:
                lines.append(f"  could not be read: {error}")
        elif kind == "corpus":
            filters = parse_corpus_request(value)
            if not filters:
                lines.append("  no filter in that request was recognised.")
            else:
                found, skipped = corpus(
                    root, testament=scope.testament, exclude=scope.ref,
                )
                hits = list(matches(found, **filters))
                lines.append(render_corpus_query(
                    hits, skipped, examples=PRECEDENT_EXAMPLES,
                    described=value, notes=True,
                ))
        else:
            lines.append(f"  unknown request kind {kind!r}.")
        lines.append("")
    return "\n".join(lines)


def validate_adjudication(chapter: str, response: dict[str, Any]) -> list[str]:
    """Check what came back before any of it reaches a file.

    Only the things a schema cannot say: that the object is about this chapter,
    that a verdict and its payload agree, and that every patch names a verse of
    it. Whether a patch is a legal alignment is the ledger validator's question
    and is asked per verse when it is applied.
    """
    scope = chapter_scope(chapter)
    errors: list[str] = []
    if response.get("ref") != scope.ref:
        errors.append(f"ref must be {scope.ref}; got {response.get('ref')!r}")
    verdict = response.get("verdict")
    patches = response.get("patches") or []
    requests = response.get("requests") or []
    if verdict == "no-change" and patches:
        errors.append(
            f"verdict no-change came with {len(patches)} patch(es); "
            "a chapter that changes returns verdict changes"
        )
    if verdict == "changes" and not patches:
        errors.append("verdict changes came with no patches")
    if verdict == "needs-evidence" and not requests:
        errors.append("verdict needs-evidence came with no requests")
    for patch in patches:
        ref = patch.get("ref", "")
        if not ref.startswith(f"{scope.ref}."):
            errors.append(f"patch names {ref!r}, which is not a verse of {scope.ref}")
    return errors


def apply_adjudication(
    root: Path, chapter: str, response: dict[str, Any], model: str = "",
) -> dict[str, Any]:
    """Write what the reconciliation decided, and say what would not land.

    A patch is validated against its own packet before it overwrites anything,
    exactly as a returned review is: an accepted verse already passed the
    validator once, so a correction that does not pass is refused rather than
    allowed over work that did. A refused patch leaves the chapter's
    fingerprint unchanged, which is what makes the next run reconcile it again
    rather than record the chapter as settled.
    """
    scope = chapter_scope(chapter)
    applied: list[str] = []
    rejected: dict[str, list[str]] = {}
    for patch in response.get("patches") or []:
        ref = patch.get("ref", "")
        try:
            verse = verse_number(ref)
        except (ValueError, IndexError):
            rejected[ref or "(unnamed)"] = ["the patch names no verse"]
            continue
        source = packet_path(root, scope, verse)
        if not source.exists():
            rejected[ref] = [f"no prepared packet for {ref}"]
            continue
        packet = json.loads(source.read_text(encoding="utf-8"))
        payload = {
            "ref": ref,
            "alignments": patch.get("alignments") or [],
            "review_notes": patch.get("review_notes") or [],
        }
        errors = validate_response(packet, payload)
        if errors:
            rejected[ref] = errors
            continue
        accepted_file = accepted_path(root, scope, verse)
        accepted_file.parent.mkdir(parents=True, exist_ok=True)
        accepted_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Re-marked here rather than left to the next run: the edit stales the
        # receipt that bound the verse to its old bytes, and a chapter carrying
        # a stale receipt is one `make finish` refuses.
        mark_reviewed(root, ref, [
            note for note in (
                f"Reconciliation by {model or 'the driver'} — corrected",
                (patch.get("reason") or "").strip(),
            ) if note
        ], model=model or None)
        applied.append(ref)
    recorded: list[str] = []
    for convention in response.get("conventions") or []:
        ref = (convention.get("ref") or scope.ref).strip()
        text = (convention.get("text") or "").strip()
        if not text:
            continue
        try:
            record_convention(root, ref, text)
        except ValueError:
            record_convention(root, scope.ref, text)
        recorded.append(text)
    promotions = response.get("promotions") or []
    amendments = response.get("amendments") or []
    try:
        promoted = promote_contract(
            root, scope,
            [entry.get("rule") or "" for entry in promotions],
            amendments,
        )
    except ValueError as error:
        # The contract is the one document every later chapter reads, so a
        # malformed rule is refused outright rather than written in a repaired
        # form nobody chose. Refusing costs this chapter's promotion and
        # nothing else: the patches above are already written, and the reason
        # is carried out with the rest of the outcome.
        promoted = {"rules": [], "amended": [], "refused": str(error)}
    # What the rule settles and what carried it, kept beside the edit. The
    # contract itself holds only the rule, because it is read by a pass that is
    # deciding a verse and not auditing a decision.
    for entry in promotions:
        for rule in promoted.get("rules", []):
            if rule["text"] == " ".join((entry.get("rule") or "").split()):
                rule["summary"] = entry.get("summary") or ""
                rule["basis"] = entry.get("basis") or ""
    return {
        "chapter": scope.ref,
        "verdict": response.get("verdict"),
        "amendments": amendments,
        "applied": applied,
        "rejected": rejected,
        "conventions": recorded,
        "promotions": promotions,
        "promoted": promoted,
        "report": response.get("report") or [],
    }


def response_schema(root: Path = ROOT) -> str:
    """The reconciliation schema, without the meta-schema line the CLI refuses."""
    schema = json.loads((root / RESPONSE_SCHEMA).read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    return json.dumps(schema, ensure_ascii=False)

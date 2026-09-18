"""Several verse tasks in one request, and the shapes that keeps honest.

A verse is a closed problem: its packet, its contract, its one object back.
Nothing about that changes when several of them travel together, and this
module exists so that nothing about it *can*. It holds only the parts a packed
request needs that a single one does not — how verses are grouped, how the
request is worded, how the outer wrapper is built from the ordinary per-verse
schema, and how the wrapper is taken apart again into the same per-verse
objects the driver already knows how to validate and land.

It is deliberately pure and imports nothing from the driver, so that both the
read-only benchmark and the production pass can share it without either one
importing the other.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from .batching import verse_of
from .diagnostics import LARGE_SOURCE_REVIEW_CHECKLIST


def packs(refs: Sequence[str], size: int) -> list[list[str]]:
    """The pass's work list as groups of at most `size` verses, in order."""
    if size < 1:
        raise ValueError("a pack must hold at least one verse")
    items = list(refs)
    return [items[index:index + size] for index in range(0, len(items), size)]


def even_packs(refs: Sequence[str], cap: int) -> list[list[str]]:
    """The work list in as few groups as a cap allows, all the same size.

    `packs` cuts a fixed size off the front until nothing is left, which leaves
    a remainder: 75 verses in packs of 50 is a request of 50 and a request of
    25, and the large one is the one that overflows. This divides instead —
    75 becomes 38 and 37, 66 becomes 33 and 33, 176 becomes four of 44 — so
    every request of a pass is about as hard as every other, and the size that
    was measured to fit is the size that is actually sent.
    """
    if cap < 1:
        raise ValueError("a pack must hold at least one verse")
    items = list(refs)
    if not items:
        return []
    count = -(-len(items) // cap)
    base, extra = divmod(len(items), count)
    groups: list[list[str]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        groups.append(items[start:start + size])
        start += size
    return groups


def pack_label(group: Sequence[str]) -> str:
    """What one request is called in the log and in the ledger.

    A single verse keeps its own reference, because that is what every other
    part of this system calls it. A group is named by its span, so a ledger row
    says which verses one price bought without listing all of them.
    """
    if not group:
        raise ValueError("a pack cannot be empty")
    if len(group) == 1:
        return group[0]
    return f"{group[0]}-{verse_of(group[-1])}"


def wrap_schema(item_schema: str) -> str:
    """The ordinary per-verse response schema, inside one ordered array.

    The item is used exactly as it stands. A packed request asks for the same
    objects a single request asks for, so the wrapper may add a container and
    nothing else: a schema that relaxed one keyword here would be a second
    contract, and the point of packing is that there is only ever one.

    Deliberately not bounded by `minItems`/`maxItems`. A pack of eight that
    comes back holding one is exactly the failure those would catch, and they
    are not reliably supported across the endpoints here — an unsupported
    keyword is a 400 on some and silently dropped on others, which would make
    the guarantee look stronger than it is. The count is stated in the prompt
    and enforced by the parent: a verse the response omitted is asked again in
    the next pack, told that it was omitted.
    """
    return json.dumps({
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": json.loads(item_schema),
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }, ensure_ascii=False)


def packed_prompt(
    refs: Sequence[str], bodies: dict[int, str], mode: str,
) -> str:
    """Several complete verse tasks with one unambiguous outer response.

    The per-verse briefings are rendered by `packed_evidence_parts_many`, which
    leaves out the ordinary "return one object" instruction precisely so that
    this can state it once, for the wrapper, without contradicting itself.
    """
    if mode not in ("packet", "review"):
        raise ValueError(f"unknown mode {mode}; expected packet or review")
    noun = "review" if mode == "review" else "alignment"
    lines = [
        f"Complete these {len(refs)} independent verse {noun} tasks.",
        "Treat every verse as a closed problem under the shared contract in",
        "your system prompt. Do not let an analysis chosen for one verse decide",
        "another. Produce the ordinary object required for each verse and put",
        "that object into `results`, in the same order as the evidence sections.",
        "Return only one outer JSON object of this form:",
        '  {"results":[<one complete object per verse>]}',
        "Do not omit a verse and do not add commentary outside the object.",
    ]
    if mode == "review":
        lines.extend([
            "For each verse, review the accepted alignment against the contract",
            "and token table. If it is right, use verdict `unchanged`, an empty",
            "alignments array, and only review notes worth preserving. If it is",
            "wrong, use verdict `corrected` and return the COMPLETE corrected",
            "alignments array; never return only a patch.",
            LARGE_SOURCE_REVIEW_CHECKLIST,
        ])
    else:
        lines.extend([
            "For each verse, align every supplied source and target ID exactly",
            "once under the contract and return its complete canonical alignment",
            "object.",
        ])
    lines.extend([
        "This is the complete briefing. There are no files to open and no",
        "commands to run.",
        "",
    ])
    for ref in refs:
        lines.append(bodies[verse_of(ref)])
        lines.append("")
    return "\n".join(lines)


def pack_payloads(
    payload: dict[str, Any] | None, refs: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The requested per-verse objects from one wrapper, and its shape errors.

    A verse is kept only if it came back exactly once and says which verse it
    is. A duplicate is dropped rather than resolved: two objects claiming one
    reference are two answers, and choosing between them here would be the
    parent guessing at a judgment it did not make.
    """
    if not isinstance(payload, dict) or set(payload) != {"results"}:
        return {}, ["packed response must contain exactly a results field"]
    results = payload.get("results")
    if not isinstance(results, list):
        return {}, ["packed results must be an array"]
    wanted = set(refs)
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    duplicates: set[str] = set()
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            errors.append(f"result {index} is not an object")
            continue
        ref = item.get("ref")
        if ref not in wanted:
            errors.append(f"result {index} has unexpected ref {ref!r}")
            continue
        if ref in found:
            duplicates.add(ref)
            errors.append(f"result ref {ref} occurs more than once")
            continue
        found[ref] = item
    for ref in duplicates:
        found.pop(ref, None)
    return found, errors


#: What a reply looks like when the pack was too large to answer rather than
#: too hard. It decides whether the driver bisects the group or gives up on it,
#: and it is a text match because that is what the CLI hands back — the
#: alternative is treating every failure as an overflow and paying to halve a
#: pack that failed for a reason halving cannot fix.
#:
#: The Claude Code fragments are spelled out rather than shortened to `output`
#: because that is the sentence the CLI actually returns, and MAT.26 and MRK.14
#: each paid $8 to halt on it: `API Error: Claude's response exceeded the 64000
#: output token maximum. To configure this behavior, set the
#: CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.` Not one fragment below
#: matched it, so the pass read an overflow as a refusal and stopped instead of
#: halving a pack that would have fit.
OVERSIZED = (
    "max_tokens", "maximum output", "output limit", "too long",
    "structured output", "truncated",
    "output token maximum", "max_output_tokens", "max output tokens",
)


def looks_oversized(text: str) -> bool:
    lowered = (text or "").lower()
    return any(fragment in lowered for fragment in OVERSIZED)


#: What a reply looks like when the transport failed rather than the model.
#: A dropped connection, an overloaded upstream, a gateway that gave up: the
#: request never reached an answer, so there is nothing about it to repair and
#: nothing to learn from asking a smaller or simpler version of it. The one
#: useful response is to send the same request again.
#:
#: The fragments are deliberately narrow. `api error: 5` and `api error: 429`
#: rather than a bare `api error`, because a 400 — a schema the CLI refused, a
#: prompt over the window — will fail identically every time it is sent, and a
#: retry there is money spent to read the same sentence twice.
TRANSIENT = (
    "connection lost", "connection error", "connection reset",
    "socket hang up", "econnreset", "epipe",
    "overloaded", "internal server error", "service unavailable",
    "bad gateway", "gateway timeout",
    "session timed out", "api error: 5", "api error: 429", "rate limit",
)


def looks_transient(text: str) -> bool:
    lowered = (text or "").lower()
    return any(fragment in lowered for fragment in TRANSIENT)

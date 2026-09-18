"""Run a chapter's verse passes as batch jobs instead of model sessions.

Every verse pass in this repository was already a closed unit of work: one
render in, one JSON object out, one validator verdict. Nothing about that
required a model session with a filesystem — the session was how it happened,
not what it was. This module drives the same unit through a provider's batch
endpoint, where the work is half price and the discipline that a session had
to be told to keep is instead structural: a stateless call cannot carry a
transcript into the next verse, so bounded context stops being a rule.

What the model decides is unchanged. What moves here is everything around it:
choosing which verses still need a pass, writing the accepted file, running
the ledger validator, and sending a rejected verse back with the validator's
own words. A verse the validator rejects costs one more request rather than
another round of reasoning about where the file went.

The state file is the whole design. A batch takes up to a day, so no command
here may assume the previous one is still in memory, and every command has to
be safe to run again. `submit`, `poll`, and `collect` therefore read and write
one small record per chapter and mode, and `status` reads it to say what to do
next.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from .alignment import (
    alignment_status,
    envelope_is_intact,
    mark_reviewed,
    review_receipt_is_fresh,
    validate_response,
)
from .pipeline import ROOT
from .providers import (
    BACKENDS,
    PRICES,
    Backend,
    Job,
    Result,
    Usage,
    build_backend,
    canonical_model,
    model_provider,
)
from .routing import PASS_EFFORTS, Route
from .render import briefing_parts_many, parse_verses
from .scope import ChapterScope, chapter_scope

MODES = ("packet", "review")

# A cache write is not necessarily readable the instant the request that made
# it returns.  HEB.13 proved that the old one-primer rule was too optimistic:
# the first six siblings each wrote roughly the same 52k-token prefix before a
# later wave finally read it.  Spend useful verse requests as serial probes
# until one demonstrates a hot prefix, then open the rolling pool.  The cap is
# only for a provider whose telemetry never shows a hit; after this many
# useful probes, further serialization cannot establish anything new.
CACHE_PROBE_LIMIT = 3


def cache_prefix_ready(cached: int, created: int) -> bool:
    """Whether one completed request demonstrates a readable shared prefix.

    Providers that expose no cache telemetry are allowed through: serializing
    their whole pass would save nothing we can observe.  Where both counters
    exist, a useful hit reads more prefix than it writes.
    """
    if cached == 0 and created == 0:
        return True
    return cached > created


def batch_directory(root: Path, scope: ChapterScope) -> Path:
    return root / "data/batch" / scope.slug


def state_path(root: Path, scope: ChapterScope, mode: str) -> Path:
    return batch_directory(root, scope) / f"{mode}.json"


def read_state(root: Path, scope: ChapterScope, mode: str) -> dict[str, Any]:
    path = state_path(root, scope, mode)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(
    root: Path, scope: ChapterScope, mode: str, state: dict[str, Any]
) -> Path:
    path = state_path(root, scope, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def custom_id(ref: str) -> str:
    """A ref in the character set every provider accepts for a batch key.

    Anthropic restricts a batch `custom_id` to letters, digits, underscore and
    dash, which rules out the dots a verse reference is written with.
    """
    return ref.replace(".", "_")


def ref_from_custom_id(value: str) -> str:
    return value.replace("_", ".")


def verse_of(ref: str) -> int:
    return int(ref.rsplit(".", 1)[1])


def pending_refs(root: Path, chapter: str, mode: str) -> list[str]:
    """The verses a pass in this mode still owes, in verse order.

    Alignment owes any verse with no accepted file and any whose file the
    ledger validator rejects. Review owes any valid verse with no receipt, and
    any whose receipt a later edit staled.
    """
    status = alignment_status(root, chapter)
    if mode == "packet":
        refs = list(status["missing"]) + list(status["invalid"])
    elif mode == "review":
        refs = list(status["unreviewed"]) + list(status["stale_reviews"])
    else:
        raise ValueError(f"unknown mode {mode}; expected one of {list(MODES)}")
    return sorted(set(refs), key=verse_of)


def repair_preamble(errors: list[str]) -> str:
    """Hand a verse back its own validator errors.

    Only ever built from validator output. A request that never produced a
    response — cancelled, timed out, refused by the provider — has nothing for
    a model to correct, and quoting the transport error under this heading
    tells it a contract was violated when none was. PSA.17.1 was resent with
    "the operation was cancelled" presented as the ledger validator's own
    words, which is a briefing that cannot be acted on.
    """
    return "\n".join([
        "=== Your previous response for this verse was rejected",
        *(f"  - {error}" for error in errors),
        "  These are the ledger validator's own words. Partition every supplied",
        "  source and target ID exactly once, and use only IDs listed below.",
        "",
        "",
    ])


def build_jobs(
    root: Path, chapter: str, refs: list[str], mode: str,
    repairs: dict[str, list[str]] | None = None,
) -> list[Job]:
    repairs = repairs or {}
    briefings = briefing_parts_many(
        root, chapter, [verse_of(ref) for ref in refs], mode
    )
    jobs = []
    for ref in refs:
        stable, variable = briefings[verse_of(ref)]
        if ref in repairs:
            variable = repair_preamble(repairs[ref]) + variable
        jobs.append(Job(custom_id=custom_id(ref), stable=stable, variable=variable))
    return jobs


def accepted_path(root: Path, scope: ChapterScope, ref: str) -> Path:
    return root / f"data/alignments/{scope.slug}/verses/{verse_of(ref):02d}.json"


def packet_path(root: Path, scope: ChapterScope, ref: str) -> Path:
    return root / f"data/output/{scope.slug}/verses/{verse_of(ref):02d}.json"


def checkpoint_complete(
    root: Path, scope: ChapterScope, ref: str, mode: str,
) -> bool:
    """Whether one verse has the durable checkpoint this pass owes.

    `pending_refs` is the chapter-wide status view and intentionally validates
    every verse. A rolling executor needs the same answer for the one result
    that just completed: rescanning the whole chapter after every future would
    turn better model concurrency into filesystem contention. Keep this small
    check equivalent to the corresponding branches of `alignment_status`.
    """
    packet_file = packet_path(root, scope, ref)
    accepted_file = accepted_path(root, scope, ref)
    if not packet_file.exists() or not accepted_file.exists():
        return False
    try:
        packet = json.loads(packet_file.read_text(encoding="utf-8"))
        accepted = json.loads(accepted_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if validate_response(packet, accepted):
        return False
    if mode == "packet":
        return True
    if mode != "review":
        raise ValueError(f"unknown mode {mode}; expected one of {list(MODES)}")
    receipt_file = (
        root / f"data/alignments/{scope.slug}/reviews/{verse_of(ref):02d}.json"
    )
    if not receipt_file.exists():
        return False
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return review_receipt_is_fresh(receipt, ref, accepted_file)


def write_payload(root: Path, scope: ChapterScope, ref: str, payload: dict[str, Any]) -> None:
    path = accepted_path(root, scope, ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


#: What a review can change without having corrected anything. The partition
#: and the heads are the alignment; a note is commentary about it. Treating a
#: note as a correction makes a receipt say a verse was fixed when its units
#: never moved.
#:
#: `confidence` stays on this list although nothing records it past
#: `nt-greek-verse-v58` / `ot-hebrew-verse-v14`: a review of a chapter written
#: before that may still drop or adjust the grade it finds, and that is an
#: annotation for exactly the reason a note is.
ANNOTATION_FIELDS = ("note", "confidence")
REVIEW_RESPONSE_KEYS = {"ref", "verdict", "alignments", "review_notes"}


def alignment_shape(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in unit.items() if key not in ANNOTATION_FIELDS}
        for unit in payload.get("alignments", [])
    ]


def review_outcome(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Name what a review did to a verse, at the granularity that matters."""
    if alignment_shape(before) != alignment_shape(after):
        return "corrected"
    if before.get("alignments") != after.get("alignments") or (
        before.get("review_notes") != after.get("review_notes")
    ):
        return "annotated"
    return "unchanged"


def review_candidate(
    payload: dict[str, Any], ref: str, existing: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """What a review says the verse should now be, or why that cannot be read.

    Separated from landing it because the two are wanted apart: the driver
    lands what this returns, and the packed benchmark compares it against the
    accepted file without writing anything. Both have to be judging the same
    response by the same rules, or the benchmark measures a route nobody runs.

    An `unchanged` verdict is the accepted file itself, which is why this takes
    it: "no correction" and "this exact correction" have to come back in the
    same shape for the caller to validate one thing rather than two.
    """
    errors: list[str] = []
    if payload.get("ref") != ref:
        errors.append(f"review ref must be {ref}; got {payload.get('ref')!r}")
    verdict = payload.get("verdict")
    if verdict not in {"unchanged", "corrected"}:
        errors.append("review verdict must be unchanged or corrected")
    alignments = payload.get("alignments")
    if not isinstance(alignments, list):
        errors.append("review alignments must be an array")
    review_notes = payload.get("review_notes")
    if not isinstance(review_notes, list) or not all(
        isinstance(note, str) for note in review_notes
    ):
        errors.append("review_notes must be an array of strings")
    if verdict == "unchanged" and alignments != []:
        errors.append(
            "an unchanged review must return an empty alignments array; "
            "do not reproduce accepted units"
        )
    if errors:
        return None, errors
    if verdict == "unchanged":
        return existing, []
    return {
        "ref": ref,
        "alignments": alignments,
        "review_notes": review_notes,
    }, []


def apply_result(
    root: Path, scope: ChapterScope, mode: str, result: Result, model: str
) -> tuple[str, list[str]]:
    """Land one returned verse, or say why it was not landed.

    Alignment writes what came back and lets the validator judge it, because
    there is nothing to lose: the alternative to a rejected file is no file.
    Review is the opposite case and gets the opposite rule — an accepted
    alignment already passed the validator once, so a correction that does not
    validate is discarded rather than allowed to overwrite good work.
    """
    ref = ref_from_custom_id(result.custom_id)
    if result.error:
        return "failed", [result.error]
    payload = result.payload or {}
    packet = json.loads(packet_path(root, scope, ref).read_text(encoding="utf-8"))
    if mode == "review":
        if not isinstance(payload, dict) or set(payload) != REVIEW_RESPONSE_KEYS:
            got = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            return "malformed", [
                "review response keys must be exactly "
                f"{sorted(REVIEW_RESPONSE_KEYS)}; got {got}"
            ]
        existing = json.loads(
            accepted_path(root, scope, ref).read_text(encoding="utf-8")
        )
        candidate, errors = review_candidate(payload, ref, existing)
        if errors:
            return "rejected", errors
        assert candidate is not None
        errors = validate_response(packet, candidate)
        if errors:
            return "rejected", errors
        outcome = review_outcome(existing, candidate)
        if outcome != "unchanged":
            write_payload(root, scope, ref, candidate)
        # The receipt carries what the review said, which is the response's
        # own notes rather than the candidate's: an `unchanged` verdict returns
        # the accepted file, and that file's notes are the previous pass's.
        mark_reviewed(root, ref, [
            f"Independent review by {model} — {outcome}",
            *payload["review_notes"],
        ], model=model)
        return outcome, []

    if mode != "packet":
        raise ValueError(f"unknown mode {mode}; expected one of {list(MODES)}")
    errors = validate_response(packet, payload)
    if not envelope_is_intact(payload):
        # Not written. Alignment normally trades a bad file for no file because
        # a bad file can still be read and repaired; a payload in a shape nobody
        # asked for cannot be, and leaving it on disk only makes a directory
        # look like it holds work.
        return "malformed", errors
    write_payload(root, scope, ref, payload)
    return ("accepted", []) if not errors else ("rejected", errors)


def require_batch_endpoint(backend: Backend) -> None:
    """Refuse a batch command aimed at a provider that has no batch API.

    Said here rather than left to the backend's own `NotImplementedError`
    because the three commands are separate and idempotent: `poll` and
    `collect` are typed a day after `submit`, and a reader who reaches the
    second one needs to be told what to run instead, not which method is
    missing. `make drive` refuses the same thing earlier still, when the route
    is parsed.
    """
    if backend.supports_batch:
        return
    batching = sorted(
        name for name, kind in BACKENDS.items() if kind.supports_batch
    )
    raise ValueError(
        f"{backend.name} has no batch endpoint; run these verses now with "
        f"--sync (make align-now), or use {', '.join(batching)}"
    )


def submit(
    root: Path, chapter: str, mode: str, backend: Backend,
    refs: list[str] | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Send every verse this mode still owes as one batch."""
    require_batch_endpoint(backend)
    scope = chapter_scope(chapter)
    state = read_state(root, scope, mode)
    if state.get("batch_id") and not state.get("collected"):
        raise RuntimeError(
            f"batch {state['batch_id']} for {scope.ref} {mode} has not been "
            "collected; run collect before submitting another"
        )
    repairs = state.get("rejected") or {}
    pending = pending_refs(root, scope.ref, mode)
    if not pending:
        return {"chapter": scope.ref, "mode": mode, "submitted": 0, "note": "nothing pending"}
    wanted = pending if refs is None else [ref for ref in pending if ref in set(refs)]
    if not wanted:
        # Narrowing a submission is for trying a chapter cheaply before
        # committing to it, never for re-running a verse that is already done:
        # an accepted verse is only resubmitted by deleting it or by failing
        # the validator.
        raise ValueError(
            f"none of the requested verses owe a {mode} pass; pending are {pending}"
        )
    jobs = build_jobs(root, scope.ref, wanted, mode, repairs)
    workdir = batch_directory(root, scope)
    workdir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        preview = workdir / f"{mode}-dry-run.json"
        preview.write_text(json.dumps(
            [backend.request_body(job) for job in jobs],
            ensure_ascii=False, indent=2,
        ) + "\n", encoding="utf-8")
        return {
            "chapter": scope.ref, "mode": mode, "dry_run": True,
            "requests": len(jobs), "refs": wanted,
            # Bytes, not tokens: a token estimate that disagrees with the
            # invoice is worse than none. What this shows is the shape — one
            # cached prefix against many small verse bodies.
            "cached_prefix_bytes": len(jobs[0].stable),
            "verse_body_bytes": sum(len(job.variable) for job in jobs),
            "written": str(preview.relative_to(root)),
        }
    batch_id = backend.submit_batch(jobs, workdir)
    history = state.get("history", [])
    state = {
        "chapter": scope.ref,
        "mode": mode,
        "provider": backend.name,
        "model": backend.model,
        "batch_id": batch_id,
        "attempt": state.get("attempt", 0) + 1,
        "submitted": wanted,
        "repaired": sorted(set(wanted) & set(repairs)),
        "collected": False,
        "rejected": {},
        "usage": state.get("usage", Usage().as_dict()),
        "history": history,
    }
    write_state(root, scope, mode, state)
    return {
        "chapter": scope.ref, "mode": mode, "batch_id": batch_id,
        "attempt": state["attempt"], "submitted": len(wanted),
        "repaired": len(state["repaired"]),
    }


def poll(root: Path, chapter: str, mode: str, backend: Backend) -> dict[str, Any]:
    require_batch_endpoint(backend)
    scope = chapter_scope(chapter)
    state = read_state(root, scope, mode)
    if state.get("collected"):
        return {"chapter": scope.ref, "mode": mode, "note": "batch already collected"}
    if not state.get("batch_id"):
        return {"chapter": scope.ref, "mode": mode, "note": "no batch submitted"}
    report = backend.poll_batch(state["batch_id"])
    report.update({"chapter": scope.ref, "mode": mode, "attempt": state["attempt"]})
    return report


def collect(root: Path, chapter: str, mode: str, backend: Backend) -> dict[str, Any]:
    """Land a finished batch, then say exactly what still owes a pass."""
    require_batch_endpoint(backend)
    scope = chapter_scope(chapter)
    state = read_state(root, scope, mode)
    if not state.get("batch_id"):
        raise RuntimeError(f"no batch submitted for {scope.ref} {mode}")
    if state.get("collected"):
        return {"chapter": scope.ref, "mode": mode, "note": "batch already collected"}
    report = backend.poll_batch(state["batch_id"])
    if not report["ended"]:
        return {
            "chapter": scope.ref, "mode": mode, "batch_id": state["batch_id"],
            "ended": False, "status": report["status"],
            "note": "batch has not finished; poll again",
        }
    outcomes: dict[str, list[str]] = {}
    rejected: dict[str, list[str]] = {}
    failed: dict[str, list[str]] = {}
    usage = Usage()
    for result in backend.collect_batch(state["batch_id"]):
        usage = usage + result.usage
        outcome, errors = apply_result(root, scope, mode, result, state["model"])
        ref = ref_from_custom_id(result.custom_id)
        outcomes.setdefault(outcome, []).append(ref)
        if errors:
            # Kept apart because only one of them is feedback. `rejected` is
            # resent with these words attached; `failed` is a request that
            # never answered, and is resent as if for the first time.
            (failed if outcome == "failed" else rejected)[ref] = errors
    total = Usage(**state.get("usage", {})) + usage
    state.update({
        "collected": True,
        "rejected": rejected,
        "failed": failed,
        "usage": total.as_dict(),
        "history": state.get("history", []) + [{
            "attempt": state["attempt"],
            "batch_id": state["batch_id"],
            "submitted": len(state["submitted"]),
            "outcomes": {key: len(value) for key, value in outcomes.items()},
            "usage": usage.as_dict(),
        }],
        "batch_id": None,
    })
    write_state(root, scope, mode, state)
    summary = {
        "chapter": scope.ref, "mode": mode, "attempt": state["attempt"],
        "outcomes": {key: sorted(value, key=verse_of) for key, value in outcomes.items()},
        "rejected": rejected,
        "failed": failed,
        "usage": usage.as_dict(),
        "still_pending": pending_refs(root, scope.ref, mode),
    }
    price = backend.price()
    if price is not None:
        summary["cost_usd"] = round(price.cost(usage, batch=True), 4)
        summary["cost_usd_total"] = round(price.cost(total, batch=True), 4)
    return summary


def run_sync(
    root: Path, chapter: str, mode: str, backend: Backend,
    refs: list[str] | None = None, width: int = 1,
) -> dict[str, Any]:
    """Align now, at full price, answering immediately.

    Batch is half the cost and the default for a chapter, but it answers in
    hours and cannot be watched. This path answers immediately, which is what a
    first look wants: try two verses, read what came back, and decide whether
    the chapter is worth committing to a batch. It writes the same files, runs
    the same validator, and records into the same state, so a chapter can start
    here and finish in a batch without either path knowing.

    `width` fans the verses out, one alone first so the rest read the prefix
    cache it paid for rather than each writing their own, then a rolling pool
    refills each completed slot immediately. It defaults to 1 — fully serial —
    because that is what trying two verses by hand wants; a driver running a
    whole chapter passes its own width.

    A backend that says it warms no prefix cache skips that opening verse and
    the probes after it, and fans out from the first request. The saving those
    buy is a fraction of an input bill, and on a free endpoint a fraction of
    zero is zero — while the latency is real and paid per verse. What is not
    skipped is `halt_if_malformed` after every wave: the shape of the response
    is the thing that actually goes wrong there, and it is still checked before
    another wave is sent.
    """
    scope = chapter_scope(chapter)
    state = read_state(root, scope, mode)
    repairs = state.get("rejected") or {}
    pending = pending_refs(root, scope.ref, mode)
    if not pending:
        return {"chapter": scope.ref, "mode": mode, "note": "nothing pending"}
    wanted = pending if refs is None else [ref for ref in pending if ref in set(refs)]
    if not wanted:
        raise ValueError(
            f"none of the requested verses owe a {mode} pass; pending are {pending}"
        )
    outcomes: dict[str, list[str]] = {}
    rejected: dict[str, list[str]] = {}
    failed: dict[str, list[str]] = {}
    usage = Usage()
    opening = Usage(**state.get("usage", {}))
    jobs = {ref_from_custom_id(job.custom_id): job
            for job in build_jobs(root, scope.ref, wanted, mode, repairs)}
    # One lock over everything shared: the running usage, the outcome lists, and
    # the state file they are written to. Concurrent `align-now` runs racing on
    # one state file is how a pass lost its cost record before.
    ledger_lock = threading.Lock()

    def run_verse(ref: str) -> Result:
        nonlocal usage
        result = backend.complete(jobs[ref])
        outcome, errors = apply_result(root, scope, mode, result, backend.model)
        with ledger_lock:
            usage = usage + result.usage
            outcomes.setdefault(outcome, []).append(ref)
            if errors:
                (failed if outcome == "failed" else rejected)[ref] = errors
            # Persist the running total after every verse. A synchronous chapter
            # is a long run and will sometimes be interrupted; the verse files
            # already survive that, and what someone spending against a daily
            # token budget must not lose is the record of what it cost.
            state["usage"] = (opening + usage).as_dict()
            write_state(root, scope, mode, state)
        return result

    # Prime the shared prefix before any sibling can race to create the same
    # cache entry. Later requests use a rolling pool: a fast verse refills its
    # slot immediately instead of waiting at a barrier for the slowest peer.
    queue = list(wanted)
    cache_ready = True
    if backend.warms_prefix_cache:
        primer = run_verse(queue.pop(0))
        halt_if_malformed(
            {"outcomes": outcomes, "rejected": rejected}, backend.model
        )
        cache_ready = cache_prefix_ready(
            primer.usage.cached_input_tokens, primer.usage.cache_write_tokens,
        )
    probes = 0
    while queue and not cache_ready and probes < CACHE_PROBE_LIMIT:
        probe = run_verse(queue.pop(0))
        probes += 1
        halt_if_malformed(
            {"outcomes": outcomes, "rejected": rejected}, backend.model
        )
        cache_ready = cache_prefix_ready(
            probe.usage.cached_input_tokens, probe.usage.cache_write_tokens,
        )
    if queue:
        next_index = 0
        with ThreadPoolExecutor(max_workers=max(width, 1)) as pool:
            futures: set[Any] = set()
            while next_index < len(queue) or futures:
                while next_index < len(queue) and len(futures) < max(width, 1):
                    futures.add(pool.submit(run_verse, queue[next_index]))
                    next_index += 1
                done, pending = wait(futures, return_when=FIRST_COMPLETED)
                futures = set(pending)
                for future in done:
                    future.result()
                halt_if_malformed(
                    {"outcomes": outcomes, "rejected": rejected}, backend.model
                )
    total = opening + usage
    state.update({
        "chapter": scope.ref, "mode": mode,
        "provider": backend.name, "model": backend.model,
        "attempt": state.get("attempt", 0) + 1,
        "submitted": wanted, "collected": True, "batch_id": None,
        "rejected": rejected, "failed": failed, "usage": total.as_dict(),
        "history": state.get("history", []) + [{
            "attempt": state.get("attempt", 0) + 1,
            "batch_id": None,
            "submitted": len(wanted),
            "outcomes": {key: len(value) for key, value in outcomes.items()},
            "usage": usage.as_dict(),
        }],
    })
    write_state(root, scope, mode, state)
    summary = {
        "chapter": scope.ref, "mode": mode, "synchronous": True,
        "model": backend.model,
        "outcomes": {key: sorted(value, key=verse_of) for key, value in outcomes.items()},
        "rejected": rejected,
        "failed": failed,
        "usage": usage.as_dict(),
        "still_pending": pending_refs(root, scope.ref, mode),
    }
    price = backend.price()
    if price is not None:
        summary["cost_usd"] = round(price.cost(usage, batch=False), 4)
    return summary


# How often a waiting run asks whether its batch has ended, and how many times
# it will resend the verses that came back rejected.
#
# One minute because polling is free and the cost of a loose interval is real:
# a batch that ends early sits finished until the next ask, and that idle time
# is the whole downside. Three attempts because a rejected verse is resent
# with the validator's own words attached, and a verse that has failed that
# three times is not failing for a reason another identical request will fix —
# it is a finding, and it belongs in the queue where someone will read it.
POLL_SECONDS = 60
MAX_ATTEMPTS = 3


class MalformedResponse(RuntimeError):
    """A provider returned something other than the shape it was asked for.

    Separate from a rejection because the two want opposite handling. A
    rejected verse is a model that read the contract and applied it wrongly,
    and resending it with the validator's words attached is how it gets fixed.
    A malformed one is a request whose response schema was never applied, so
    every verse in the round is wrong the same way and none of them will be
    fixed by asking again — the resends just buy the same answer twice more.

    PSA.17 is why this exists. Gemini's batch endpoint accepted a
    `responseJsonSchema` it did not enforce, and fourteen verses came back in
    an invented format the model named after the contract's own version
    string. The run paid for the round, then queued all fifteen to be paid for
    again. The identical request sent synchronously honors the schema, so
    nothing about the model or the contract was wrong.
    """


def halt_if_malformed(summary: dict[str, Any], model: str) -> None:
    """Stop a run whose provider is not honoring the response schema."""
    malformed = (summary.get("outcomes") or {}).get("malformed") or []
    if not malformed:
        return
    rejected = summary.get("rejected") or {}
    example = rejected.get(malformed[0]) or []
    raise MalformedResponse(
        f"{model} returned {len(malformed)} verse(s) in the wrong shape "
        f"({', '.join(malformed[:6])}"
        f"{', …' if len(malformed) > 6 else ''}). The response schema was not "
        f"applied, so resending buys the same answer again.\n"
        f"  {malformed[0]}: {example[0] if example else 'no detail recorded'}"
    )


# What a provider calls a request that will not change again. Each names its
# own subset, so the sum over whichever keys are present is the one count that
# means the same thing everywhere.
FINISHED_COUNTS = (
    "succeeded", "completed", "errored", "failed",
    "canceled", "cancelled", "expired",
)
FAILED_COUNTS = ("errored", "failed")


def _elapsed(seconds: int) -> str:
    minutes, second = divmod(int(seconds), 60)
    hour, minute = divmod(minutes, 60)
    if hour:
        return f"{hour}h{minute:02d}m"
    if minutes:
        return f"{minutes}m{second:02d}s"
    return f"{second}s"


def progress_line(
    mode: str, status: dict[str, Any], submitted: int, waited: int, poll_seconds: int
) -> str:
    """What a waiting run says about a batch that has not ended.

    A bare provider state leaves a reader unable to tell a batch that is nearly
    done from one that has not started. Anthropic and OpenAI report per-request
    counts, so the line can say how far along it is; Gemini reports only a job
    state, and the honest line there is how big the batch is and how long the
    wait has run. Both beat the state name alone.
    """
    counts = status.get("counts") or {}
    total = counts.get("total") or submitted or 0
    parts = [f"{mode}: batch is {status.get('status')}"]
    if any(key in counts for key in FINISHED_COUNTS):
        done = sum(counts.get(key) or 0 for key in FINISHED_COUNTS)
        failed = sum(counts.get(key) or 0 for key in FAILED_COUNTS)
        parts.append(
            f"{done}/{total} verse(s) done" + (f", {failed} failed" if failed else "")
        )
    elif total:
        parts.append(f"{total} verse(s) in flight")
    if waited:
        parts.append(f"{_elapsed(waited)} so far")
    return ", ".join(parts) + f"; next check in {poll_seconds}s"


def run_batch(
    root: Path, chapter: str, mode: str, backend: Backend, *,
    poll_seconds: int = POLL_SECONDS, max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Submit this pass as a batch and wait for it, resending what it rejects.

    `submit`, `poll`, and `collect` stay separate commands because a batch
    takes a day and nothing should have to be held open across it. This is the
    other way to spend that day: one blocking call that a caller can put in a
    shell and leave, which is what driving a chapter end to end in one command
    needs. It adds no state and no rules — the same three steps in a loop,
    against the same state file — so a run interrupted here is still resumed by
    any of them, and a batch already in flight is waited on rather than sent
    twice.
    """
    scope = chapter_scope(chapter)
    rounds: list[dict[str, Any]] = []
    usage = Usage()

    for attempt in range(1, max_attempts + 1):
        state = read_state(root, scope, mode)
        if state.get("batch_id") and not state.get("collected"):
            # Someone already paid for this batch. Waiting on it is the only
            # correct move: submitting again would buy the same verses twice.
            log(f"{mode}: waiting on batch {state['batch_id']} already in flight")
            submitted = len(state.get("submitted") or [])
        else:
            pending = pending_refs(root, scope.ref, mode)
            if not pending:
                break
            report = submit(root, scope.ref, mode, backend)
            if not report.get("batch_id"):
                break
            log(
                f"{mode}: batch {report['batch_id']} submitted, "
                f"{report['submitted']} verse(s), attempt {report['attempt']}"
            )
            submitted = report["submitted"]

        waited = 0
        while True:
            status = poll(root, scope.ref, mode, backend)
            if status.get("ended") or status.get("note"):
                break
            log(progress_line(mode, status, submitted, waited, poll_seconds))
            sleep(poll_seconds)
            waited += poll_seconds

        summary = collect(root, scope.ref, mode, backend)
        if summary.get("ended") is False:
            # `poll` said ended and `collect` disagrees, which means the batch
            # moved under us. Round again rather than treating it as finished.
            log(f"{mode}: batch is {summary.get('status')}; polling again")
            continue
        # Before any resend: `collect` has already written the state and the
        # usage, so the round is paid for and recorded either way. What this
        # refuses is buying it again.
        halt_if_malformed(summary, backend.model)
        rounds.append(summary)
        usage = usage + Usage(**summary.get("usage", {}))
        log(f"{mode}: {json.dumps(summary.get('outcomes', {}))}")
        still = summary.get("still_pending") or []
        if not still:
            break
        if attempt < max_attempts:
            log(f"{mode}: {len(still)} verse(s) still owed; resending with the "
                "validator's errors attached")

    price = backend.price()
    return {
        "chapter": scope.ref, "mode": mode, "batch": True,
        "model": backend.model, "attempts": len(rounds),
        "outcomes": rounds[-1].get("outcomes", {}) if rounds else {},
        "rejected": rounds[-1].get("rejected", {}) if rounds else {},
        "failed": rounds[-1].get("failed", {}) if rounds else {},
        "usage": usage.as_dict(),
        **({} if price is None else {
            "cost_usd": round(price.cost(usage, batch=True), 4)
        }),
        "still_pending": pending_refs(root, scope.ref, mode),
        "rounds": rounds,
    }


def batch_status(root: Path, chapter: str) -> dict[str, Any]:
    """What the batch pipeline would do next, without calling any provider."""
    scope = chapter_scope(chapter)
    report: dict[str, Any] = {"chapter": scope.ref}
    for mode in MODES:
        state = read_state(root, scope, mode)
        pending = pending_refs(root, scope.ref, mode)
        if state.get("batch_id"):
            action = f"make batch-collect CHAPTER={scope.ref} MODE={mode}"
        elif pending:
            action = f"make batch-submit CHAPTER={scope.ref} MODE={mode}"
        else:
            action = None
        usage = Usage(**state.get("usage", {}))
        price = PRICES.get(state.get("model", ""))
        report[mode] = {
            "pending": len(pending),
            "pending_refs": pending,
            "in_flight": state.get("batch_id"),
            "attempts": state.get("attempt", 0),
            "model": state.get("model"),
            "rejected": state.get("rejected", {}),
            "usage": usage.as_dict(),
            # What this chapter's passes have actually cost so far, from the
            # usage the provider reported rather than from an estimate.
            "cost_usd": None if price is None else round(price.cost(usage, batch=True), 4),
            "next_action": action,
        }
    report["next_action"] = (
        report["packet"]["next_action"]
        or report["review"]["next_action"]
        or f"make finish CHAPTER={scope.ref}"
    )
    return report


def resolve_backend(
    root: Path, chapter: str, mode: str, args: argparse.Namespace
) -> Backend:
    """Use the provider a batch was submitted with, unless told otherwise.

    Collecting a batch from a different provider than submitted it would fail
    obscurely a day after the mistake, so the state file is authoritative and
    the flag is the override rather than the other way round.
    """
    state = read_state(root, chapter_scope(chapter), mode)
    implied = model_provider(args.model) if args.model else None
    provider = args.provider or implied or state.get("provider") or "anthropic"
    model = args.model or (state.get("model") if provider == state.get("provider") else None)
    model = canonical_model(provider, model)
    effort = args.effort or Route(provider, model).resolved_effort(PASS_EFFORTS[mode])
    return build_backend(
        provider, model, root=root, effort=effort, max_tokens=args.max_tokens,
        mode=mode,
    )


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=sorted(BACKENDS))
    parser.add_argument("--model", help="override the provider's default model")
    parser.add_argument(
        "--effort",
        help="reasoning effort (default: medium for alignment, high for review; "
             "model floors still apply)",
    )
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument(
        "--width", type=int, default=1,
        help="how many verses are in flight at once (default: 1, fully "
             "serial). Each verse is its own request either way; this is how "
             "many of those requests run together",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a chapter's verse passes through a provider's batch endpoint"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "poll", "collect"):
        sub = subparsers.add_parser(name)
        sub.add_argument("chapter")
        sub.add_argument("--mode", choices=list(MODES), default="packet")
        add_backend_arguments(sub)
        if name == "submit":
            sub.add_argument(
                "--dry-run", action="store_true",
                help="write the request bodies for inspection without submitting",
            )
            sub.add_argument(
                "--verses",
                help="narrow the submission to a verse, a range such as 1-8, or a "
                     "comma-separated list — for trying a chapter cheaply first",
            )
            sub.add_argument(
                "--sync", action="store_true",
                help="answer now, one request at a time, at full price instead "
                     "of waiting on a batch",
            )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("chapter")
    verse_parser = subparsers.add_parser("verse")
    verse_parser.add_argument("ref")
    verse_parser.add_argument("--mode", choices=list(MODES), default="packet")
    add_backend_arguments(verse_parser)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            report = batch_status(ROOT, args.chapter)
        elif args.command == "verse":
            book, chapter_number, _ = args.ref.upper().split(".")
            chapter = f"{book}.{chapter_number}"
            backend = resolve_backend(ROOT, chapter, args.mode, args)
            report = run_sync(
                ROOT, chapter, args.mode, backend, refs=[args.ref.upper()]
            )
        else:
            backend = resolve_backend(ROOT, args.chapter, args.mode, args)
            if args.command == "submit":
                refs = None
                if args.verses:
                    chapter = chapter_scope(args.chapter).ref
                    refs = [
                        f"{chapter}.{verse}"
                        for verse in parse_verses(args.verses, list(range(0, 200)))
                    ]
                if args.sync:
                    report = run_sync(
                        ROOT, args.chapter, args.mode, backend, refs=refs,
                        width=args.width,
                    )
                else:
                    report = submit(
                        ROOT, args.chapter, args.mode, backend,
                        refs=refs, dry_run=args.dry_run,
                    )
            elif args.command == "poll":
                report = poll(ROOT, args.chapter, args.mode, backend)
            else:
                report = collect(ROOT, args.chapter, args.mode, backend)
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # `failed` counts too: a verse whose request never answered has not been
    # done, and an exit code that calls that success is how a chapter gets
    # reported finished with a hole in it.
    return 1 if (
        report.get("rejected") or report.get("failed") or report.get("errors")
    ) else 0

"""Drive a chapter through isolated, validated verse passes.

This module owns the inexpensive coordination loop rather than asking a model
session to dispatch other model sessions.

Two things it does that a fan-out cannot.

A subagent creates its own prompt cache, so 46 of them wrote the same prefix 46
times; cache writes are 1.25x input and that was 63% of a subagent's cost.
Consecutive `claude -p` sessions instead *share* a prefix cache — a fresh
session starts by reading 15,971 tokens and writing only 7,126 — so the first
verse of a pass runs alone to pay for the cache and the rest use a rolling pool
that reads it.

And per-verse isolation cannot see across verses, which is a real gap rather
than a saving: Genesis 5's review corrected eight verses that had analysed one
construction two ways. So a pass is followed by a reconciliation that looks at
the chapter as a whole — by default the bounded request in `adjudication.py`,
which is handed one packet and
answers with patches this module validates and writes. Everything mechanical around it — the finishing
checks, the commit — stays in this file, because a model session is the
expensive way to run a command that cannot fail interestingly.

A verse pass does not have to be a session at all. On every route but
`code:session` and `codex` it is one request: no tools, a response schema, and
a JSON object back that this module validates and writes. Three turns become
one — a session that renders, writes, and validates re-reads its whole briefing
at each of them — and the model is left with the judgment, which is the only
part of a verse it was ever needed for.

This is also the one entry point for the chapter whichever way it runs. `routes` picks the model for
each verse pass — a Claude Code or Codex CLI session per verse, or the same
verses through a provider endpoint, synchronously or as a batch at half price —
and the rest of the chapter is identical either way, because `pending_refs` asks
the filesystem what is owed rather than asking a run what it did.

The two whole-chapter passes, textual decisions and reconciliation, are routed
too, but only between the two CLIs. Textual decisions use an agentic session;
reconciliation defaults to a bounded, schema-held request and keeps the
agentic shape as `code:session`. Both CLI runners take the same arguments and
return the same result, so nothing downstream of `cli_session` knows which one
it got.

The seam is deliberate: planning, throttling, routing, and prompt text are pure
and tested without spawning anything, and every session goes through an injected
`run_session`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .batching import (
    CACHE_PROBE_LIMIT,
    MODES,
    MalformedResponse,
    accepted_path,
    apply_result,
    checkpoint_complete,
    cache_prefix_ready,
    custom_id,
    pending_refs,
    repair_preamble,
    run_batch,
    run_sync,
    verse_of,
)
from .adjudication import (
    adjudication_parts,
    answer_requests,
    apply_adjudication,
    validate_adjudication,
)
from .adjudication import response_schema as adjudication_schema
from .chapter import (
    REENTRY_GUARD,
    Halt,
    clear_queue_entry,
    ensure_prepared,
    ensure_sources,
    ensure_variant_decisions,
    queue_path,
    record_queue_entry,
)
from .alignment import (
    REVIEWER_ENV,
    file_sha256,
    mark_reviewed,
    record_run_provenance,
    unresolved_variant_units,
)
from .pipeline import ROOT
from .providers import (
    CREDENTIALS,
    PRICES,
    Job,
    Result,
    Usage,
    build_backend,
    missing_credential,
)
from .packing import (
    even_packs,
    looks_transient,
    pack_label,
    pack_payloads,
    packed_prompt,
    packs,
    wrap_schema,
)
from .render import (
    packed_evidence_parts_many,
    render_decisions,
    render_reconciliation,
    verse_number,
    verse_session_parts_many,
    verse_task_parts_many,
)
from .routing import (
    CODEX_DEFAULT_MODEL,
    PACK_ALL,
    PACK_ALL_CAP,
    DECISIONS,
    DEFAULT_ROUTES,
    PASS_EFFORTS,
    RECONCILE,
    Route,
    parse_route,
    resolve_routes,
)
from .scope import ChapterScope, chapter_scope

# Alignment applies the contract to a fresh packet; review has to notice a
# rule misapplied in a file that already validates. Measured on 1KI.11: medium
# reproduced the accepted alignment exactly, and low did not.
EFFORTS = PASS_EFFORTS
RECONCILE_EFFORT = "high"

# Textual decisions are alignment — the contract applied to a closed
# candidate set — so they run at the alignment effort.
DECISIONS_EFFORT = "medium"

# The built-in tools each verse pass is given, which is every tool its briefing
# names and nothing else. The full set is about 26k tokens of schema, and a
# session re-reads its whole prefix on every turn, so the saving is per turn
# rather than per session: measured on PHP.2.1's review, an eight-turn session
# read 245,900 cached tokens with the default set and 125,582 with this one,
# for the same review and the same correction.
#
# This holds the verse passes and is checked against MODES, so a pass added
# without a tool set fails at import rather than mid-chapter.
PASS_TOOLS = {
    # Renders its packet, writes one file, validates it.
    "packet": ("Bash", "Read", "Write"),
    # The same, plus editing an accepted file it decides is wrong. A review
    # that changes nothing calls none of them.
    "review": ("Bash", "Read", "Edit", "Write"),
}

# What a tool-free pass is allowed: nothing. Spelled as a name because the
# empty tuple cannot speak for itself at a call site, and because the bug it
# guards against is silent — `if tools:` is false for `()`, so an empty
# allowlist that fell through to the default emitted no flag at all and handed
# the session the *full* tool set, which is the opposite of what it asked for.
# `claude_session` therefore tests `tools is not None`.
#
# An empty allowlist rather than a deny list naming the tools, which was
# measured: `--tools ""` leaves the cached prefix byte-identical, while naming
# tools in a deny list changes it and re-writes it on every call.
NO_TOOLS: tuple[str, ...] = ()

# The line a review session ends with, and the whole of what it has to produce
# when the alignment is already right. A tool call is not free in turns: the
# session that runs it must be handed the result and speak again, and that
# closing turn re-reads the entire briefing to say nothing. Measured on
# PHP.4.4-7, every review took three turns — one to render, one to judge and
# run `mark-reviewed`, one to report having run it. Handing the briefing over
# killed the first; letting the driver write the receipt from this line kills
# the third, and the judgment itself is the turn that remains.
REVIEW_VERDICT = "REVIEWED:"

# The decisions pass reads nothing and writes nothing: its briefing arrives in
# its prompt and it records the whole chapter with one `decide-many`. It runs
# once per chapter rather than once per verse, so the saving is small in
# absolute terms, but it is the same saving per turn as anywhere else.
# Reconciliation stays unrestricted — its reach across a chapter cannot be
# guessed in advance, and a pass missing a tool it needs is worse off than one
# carrying a schema it never calls.
DECISIONS_TOOLS = ("Bash",)

# Percentage of the 5-hour window below which a run stops fanning out, and below
# which it refuses to start work at all. A pass launched with 2% left cannot
# finish a verse, and an abandoned pass spends the window's last tokens for
# nothing.
SAFE_THRESHOLD = 10
STOP_THRESHOLD = 2

DEFAULT_WIDTH = 6


@dataclass(frozen=True)
class SessionResult:
    """What one `claude -p` invocation returned, including what it cost.

    A driver whose whole purpose is spending less has to say what it spent, and
    say it per session: the interesting number is not the total but the split
    between the first cache-priming verse and the ones that read that cache.
    """

    label: str
    returncode: int
    output: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str = ""
    # What a session held to a response schema returned, already parsed. Only
    # a tool-free pass has one: a session that writes its own file answers in
    # prose, and its accepted JSON is on disk rather than in its reply.
    payload: dict[str, Any] | None = None
    # Model sessions beyond this logical ledger row: delegated children or
    # fresh bounded retries. Claude's closing envelope counts only the
    # coordinator and, after compaction, sometimes only its final segment.
    child_sessions: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CostLedger:
    """Per-session usage for one pass, written to disk as it accrues.

    Threads share this, so it locks. It is written after every session rather
    than at the end, because a pass that is interrupted has still spent what it
    spent — and because a fan-out that loses its cost record is exactly what
    happened when four concurrent `align-now` runs raced on one state file.
    """

    def __init__(self, root: Path, chapter: str, mode: str):
        scope = chapter_scope(chapter)
        self.path = root / f"data/driver/{scope.slug}/{mode}.json"
        self.chapter = scope.ref
        self.mode = mode
        self.lock = threading.Lock()
        self.sessions: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                self.sessions = json.loads(self.path.read_text(encoding="utf-8"))["sessions"]
            except (json.JSONDecodeError, KeyError):
                self.sessions = []

    def record(
        self, result: SessionResult, wave: int, fingerprint: str | None = None,
        checkpoint_error: str | None = None,
        refs: Sequence[str] | None = None,
    ) -> None:
        """One row per request. `refs` names the verses a packed one covered.

        A row without them is a row about one verse, which is what every row
        was before packing existed and what most rows still are. With them the
        row still costs what the request cost — the per-verse figures below
        divide by the verses, not by the requests, or packing would report
        itself as eight times more expensive than it is.
        """
        with self.lock:
            ok = result.ok and checkpoint_error is None
            self.sessions.append({
                "ref": result.label,
                **({"refs": list(refs)} if refs else {}),
                "wave": wave,
                "ok": ok,
                **({"fingerprint": fingerprint} if fingerprint else {}),
                "cost_usd": round(result.cost_usd, 6),
                "turns": result.turns,
                "session_id": result.session_id,
                **({"child_sessions": result.child_sessions}
                   if result.child_sessions else {}),
                # What a failed session said, kept because the terminal it said
                # it in is gone by the time anyone looks — and because a session
                # that died before its first turn has no transcript to resume
                # into, so this is the only account of it there will be.
                **({} if ok else {
                    "error": (checkpoint_error or result.output)[-800:],
                }),
                "usage": {
                    key: result.usage.get(key, 0) for key in (
                        "input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens", "output_tokens",
                    )
                },
            })
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({
                    "chapter": self.chapter, "mode": self.mode,
                    "totals": self.totals(locked=False),
                    "sessions": self.sessions,
                }, indent=2) + "\n",
                encoding="utf-8",
            )

    def sessions_for(self, refs: Sequence[str]) -> dict[str, dict[str, str]]:
        """The session that last worked each of these verses, newest wins.

        A verse can be attempted more than once across re-runs, and what
        someone asking "what happened to verse 14" wants is the attempt that
        left it in its current state, not the first one.
        """
        wanted = set(refs)
        found: dict[str, dict[str, str]] = {}
        for row in self.sessions:
            # A packed row is the answer for every verse it carried: someone
            # asking what happened to verse 14 has to be sent to the request
            # that was holding it, whether or not it was named alone.
            covered = [ref for ref in row.get("refs", [row["ref"]]) if ref in wanted]
            if not covered:
                continue
            entry = {}
            if row.get("session_id"):
                entry["session"] = row["session_id"]
            if row.get("error"):
                entry["error"] = row["error"]
            if entry:
                for ref in covered:
                    found[ref] = entry
        return found

    def totals(self, locked: bool = True) -> dict[str, Any]:
        rows = self.sessions
        verses = sum(len(row.get("refs", [row["ref"]])) for row in rows)
        total = {
            "sessions": len(rows),
            "model_sessions": sum(1 + row.get("child_sessions", 0) for row in rows),
            "cost_usd": round(sum(row["cost_usd"] for row in rows), 4),
        }
        if verses != len(rows):
            total["verses"] = verses
        for key in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens",
        ):
            total[key] = sum(row["usage"].get(key, 0) for row in rows)
        if rows:
            total["cost_usd_per_verse"] = round(total["cost_usd"] / verses, 4)
            # The first session of a pass writes the prefix cache the rest read.
            # Reporting it separately is what makes cache priming falsifiable.
            primer = [row for row in rows if row["wave"] == 0]
            if primer:
                total["priming_session_cost_usd"] = round(primer[0]["cost_usd"], 4)
        return total


def chapter_fingerprint(root: Path, chapter: str) -> str:
    """A hash over every accepted verse file in the chapter.

    Reconciliation is the one session a re-run would otherwise pay for twice, so
    it is bound to the alignments it read, exactly as a review receipt is bound
    to its verse. Edit any verse and the chapter needs reconciling again; edit
    nothing and it does not.
    """
    scope = chapter_scope(chapter)
    directory = root / f"data/alignments/{scope.slug}/verses"
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def reconciled_fingerprint(root: Path, chapter: str) -> str | None:
    """What the last successful reconciliation of this chapter saw, if any."""
    scope = chapter_scope(chapter)
    path = root / f"data/driver/{scope.slug}/reconcile.json"
    if not path.exists():
        return None
    try:
        sessions = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    except (json.JSONDecodeError, KeyError):
        return None
    done = [row for row in sessions if row.get("ok") and row.get("fingerprint")]
    return done[-1]["fingerprint"] if done else None


@dataclass(frozen=True)
class Throttle:
    """Whether to run, how wide, and why — decided from the window alone."""

    action: str  # "go", "safe", or "stop"
    width: int
    reason: str


def throttle(
    percent_left: int | None, width: int,
    safe: bool = False, auto_safe: bool = True,
) -> Throttle:
    """Decide how to spend what is left of the window.

    An unreadable report is not a reason to stop: the run proceeds at full width
    and says so, because refusing to work on a missing number would strand a
    chapter over a display detail.
    """
    if percent_left is None:
        return Throttle("go", 1 if safe else width, "quota unreadable, proceeding")
    left = percent_left
    if left <= STOP_THRESHOLD:
        return Throttle("stop", 0, f"{left}% of the window left, nothing to start work with")
    if safe:
        return Throttle("safe", 1, f"{left}% left, sequential by request")
    if auto_safe and left < SAFE_THRESHOLD:
        return Throttle("safe", 1, f"{left}% left, dropping to sequential")
    return Throttle("go", width, f"{left}% left")


WINDOW_PATTERNS = {
    "session_left": r"Current session:\s*(\d+)%\s*used",
    "week_left": r"Current week \(all models\):\s*(\d+)%\s*used",
}


def read_window(run: Callable[..., SessionResult]) -> dict[str, int | None]:
    """How much of each limit is left, from one `/usage` reply.

    Free to call — `/usage` is synthetic and spends no model tokens — so a run
    reads it at the start and at the end and reports the difference. The weekly
    figure comes back in the same reply, and it is the one that equates to
    money, so it is worth printing even though the throttle only watches the
    five-hour window.
    """
    result = run("/usage", effort=None, label="quota")
    if not result.ok:
        return {key: None for key in WINDOW_PATTERNS}
    found: dict[str, int | None] = {}
    for key, pattern in WINDOW_PATTERNS.items():
        match = re.search(pattern, result.output)
        found[key] = 100 - int(match.group(1)) if match else None
    return found


def read_quota(run: Callable[..., SessionResult]) -> int | None:
    """Percentage of the 5-hour window already spent, via `/usage`.

    `/usage` is a synthetic reply and costs no model tokens, so this is free to
    call after every `width` launches rather than once per pass.
    """
    result = run("/usage", effort=None, label="quota")
    if not result.ok:
        return None
    found = re.search(r"Current session:\s*(\d+)%\s*used", result.output)
    if not found:
        return None
    return 100 - int(found.group(1))


# Where the Codex CLI keeps its session transcripts, which is the only place it
# says anything about its own allowance. `codex exec --json` does not emit the
# rate-limit event on stdout, and there is no `/usage` to ask: the figures ride
# along with the token counts inside the rollout, as an `event_msg` record whose
# payload is a `token_count`. So this reads the file rather than the stream —
# and reads it rather than spending a turn to ask, which is what any probe
# session would cost. Claude's `/usage` is synthetic and free; this is the
# equivalent, and it is free for a different reason.
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

# The two windows Codex reports, against the two the run report already prints.
# They arrive as `primary` and `secondary`, and those slots are NOT fixed to a
# duration: one machine's reading had primary at 300 minutes with secondary at
# 10080, and the same machine's reading a few months later had primary at 10080
# and secondary null. So a window is identified by the length it states, never
# by the slot it came in — reading the slot names cost one run that reported a
# weekly allowance under a five-hour heading, which is the exact confusion this
# whole block exists to remove.
CODEX_WINDOW_SLOTS = ("primary", "secondary")
CODEX_WINDOW_KEYS = ("session_left", "week_left")

# Anything at or under six hours is the short window; longer is the weekly one.
# A bound rather than an equality, because the short window is quoted as 300
# minutes today and the value that matters is which of the two headings it goes
# under, not whether it is exactly five hours.
SHORT_WINDOW_MINUTES = 360


def newest_rollout(home: Path = CODEX_HOME) -> Path | None:
    """The most recently written Codex session transcript, if there is one.

    Sessions are partitioned by date, so this walks down the newest year, month
    and day rather than stat-ing every rollout on the machine — there were 337
    of them on the first machine this ran on, and that number only grows.

    Newest rather than "this run's": the driver's own sessions are the most
    recent thing to have written here by the time it asks. A session started
    from another shell would be read instead, which is the same imprecision
    `/usage` already has — the window belongs to the account, not to the run.
    """
    directory = home / "sessions"
    for _ in range(3):  # year, month, day
        if not directory.is_dir():
            return None
        children = [child for child in directory.iterdir() if child.is_dir()]
        if not children:
            return None
        directory = max(children, key=lambda child: child.name)
    rollouts = list(directory.glob("rollout-*.jsonl"))
    if not rollouts:
        return None
    return max(rollouts, key=lambda path: path.stat().st_mtime)


def rollout_rate_limits(path: Path) -> dict[str, Any] | None:
    """The last rate-limit reading in one transcript, or nothing if it has none.

    Scanned forward keeping the last match rather than read whole: a
    reconciliation transcript runs to megabytes, and only the lines that mention
    the field are ever parsed.
    """
    found: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8") as transcript:
            for line in transcript:
                if '"rate_limits"' not in line:
                    continue
                try:
                    payload = json.loads(line).get("payload") or {}
                except json.JSONDecodeError:
                    continue
                limits = payload.get("rate_limits")
                if isinstance(limits, dict):
                    found = limits
    except OSError:
        return None
    return found


def codex_window_left(
    limits: dict[str, Any] | None, now: float | None = None,
) -> dict[str, int | None]:
    """How much of each Codex window is left, from one rate-limit reading.

    Each reading is filed under the heading its own `window_minutes` implies
    rather than the slot it arrived in, because those slots move. A reading with
    an expired `resets_at` is reported as a full window, since it rolled over
    after the transcript was written.
    """
    left: dict[str, int | None] = {key: None for key in CODEX_WINDOW_KEYS}
    if not limits:
        return left
    moment = time.time() if now is None else now
    for position, name in enumerate(CODEX_WINDOW_SLOTS):
        window = limits.get(name)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        minutes = window.get("window_minutes")
        if isinstance(minutes, (int, float)):
            key = "session_left" if minutes <= SHORT_WINDOW_MINUTES else "week_left"
        else:
            # Nothing said how long it is, so fall back to the slot it arrived
            # in. Worse than the length and better than dropping the reading.
            key = CODEX_WINDOW_KEYS[position]
        resets_at = window.get("resets_at")
        if isinstance(resets_at, (int, float)) and resets_at <= moment:
            # The window rolled over since this was written, so none of it is
            # spent. Reporting the stale percentage would be worse than
            # reporting nothing, because it looks current.
            left[key] = 100
            continue
        left[key] = max(0, min(100, 100 - int(round(float(window["used_percent"])))))
    return left


def read_codex_window(
    home: Path = CODEX_HOME, now: float | None = None,
) -> dict[str, int | None]:
    """How much of the ChatGPT allowance is left, as the run report prints it."""
    rollout = newest_rollout(home)
    if rollout is None:
        return {key: None for key in CODEX_WINDOW_KEYS}
    return codex_window_left(rollout_rate_limits(rollout), now=now)


def review_verdict(output: str) -> str | None:
    """The judgment a review session ended with, or nothing if it did not.

    Read from the end backwards. A session reasoning aloud may quote the format
    it was given before it has decided anything, and the line that counts is
    the one it stopped on.
    """
    for line in reversed((output or "").splitlines()):
        stripped = line.strip().lstrip("*#- ").strip()
        if stripped.upper().startswith(REVIEW_VERDICT):
            note = stripped[len(REVIEW_VERDICT):].strip().strip("*").strip()
            if note:
                return note
    return None


def land_review(
    root: Path, scope: ChapterScope, result: SessionResult, before: str,
    reviewer: str = "",
) -> str | None:
    """Record what a review session judged, or say why it could not be.

    The session states the judgment; the driver writes the receipt. That is the
    division `ALIGNMENT_REVIEWER` already follows — a fact the caller holds is
    not asked of the model — and here it also buys the pass a turn, since a
    session that runs `mark-reviewed` has to be told it worked and then say so.
    Nothing is taken on the session's word: `mark_reviewed` validates the file
    it signs, so a correction that does not validate leaves the verse owed, and
    whether the verse was corrected is read off the file rather than reported.
    """
    path = accepted_path(root, scope, result.label)
    if not path.exists():
        return f"the accepted file for {result.label} is no longer on disk"
    note = review_verdict(result.output)
    if note is None:
        return (
            "session exited successfully but left no durable review "
            f"checkpoint: it ended with no `{REVIEW_VERDICT} ...` line to "
            f"record. Its final response was: {result.output[-600:]}"
        )
    outcome = "unchanged" if file_sha256(path) == before else "corrected"
    try:
        mark_reviewed(
            root, result.label, [f"Independent review — {outcome}", note],
            model=reviewer or None,
        )
    except (FileNotFoundError, ValueError) as error:
        return f"the verdict could not be recorded: {error}"
    return None


def verse_prompt(ref: str, mode: str, briefing: str) -> str:
    """The whole briefing for one verse session, carried inline.

    Naming the commands here keeps a worker from exploring, which is the
    behaviour that makes a session expensive. Handing it the briefing keeps it
    from rendering, which is the turn every session used to open with — and a
    turn is a re-read of everything before it. The contract half of that
    briefing arrives as the session's system prompt; what is below is the verse.
    """
    chapter, verse = ref.rsplit(".", 1)
    parent_note = (
        "You were launched by the active `make drive` process. That parent is "
        "expected and is waiting for your checkpoint; do not inspect running "
        "processes, and do not hold back a write or a verdict because the "
        "driver is live. Other verse workers use disjoint files.\n\n"
    )
    inline_note = (
        "Your complete briefing is the binding contract and this chapter's "
        "conventions in your system prompt, and below them the response shape, "
        "the pass actions, and the verse. It is current as of this moment and "
        "there is nothing to render. Reason from it directly.\n\n"
    )
    if mode == "packet":
        return (
            f"Align {ref}, and nothing else.\n\n"
            f"{parent_note}"
            f"{inline_note}"
            f"Write data/alignments/{chapter}/verses/{int(verse):02d}.json and "
            f"validate it in the same tool interaction:\n"
            f"  PYTHONPATH=src python3 scripts/generate_alignments.py validate-verse {ref}\n\n"
            "Do not open packet JSON, the response schema, README.md, AGENTS.md, or "
            "any other verse. Do not list or create directories. Do not commit. "
            f"Stop when the validator passes.\n\n{briefing}"
        )
    return (
        f"Review {ref}, and nothing else.\n\n"
        f"{parent_note}"
        f"{inline_note}"
        "The briefing includes the accepted units with their IDs and heads. "
        "Check the partition, the edges, the heads, and "
        "the statuses against the token table.\n\n"
        "A verse that is already right takes one reply and no tool call at all: "
        f"state your judgment and stop. Only if {ref} is wrong do you open the "
        "accepted JSON, apply the edit, and re-validate it in the same tool "
        "interaction:\n"
        f"  PYTHONPATH=src python3 scripts/generate_alignments.py validate-verse {ref}\n\n"
        f"Either way, end your reply with one line:\n"
        f"  {REVIEW_VERDICT} <one sentence a later reader can act on>\n"
        "The driver records the receipt from that line, and names what reviewed "
        "the verse on its own — do not run `mark-reviewed` yourself and do not "
        "name a model in the note.\n\n"
        "Do not open the response schema, README.md, or AGENTS.md, do not read "
        "other verses, do not harmonise notes across the chapter, and do not "
        f"commit.\n\n{briefing}"
    )


# How many times one verse is asked before the pass gives up on it. Three: the
# first ask and two retries, each a fresh session rather than a continuation.
# A model that has just partitioned a verse wrongly is the reader least able to
# see it, and the transcript it would carry is the rejected answer. What a
# retry gets instead is the same briefing with the validator's own words in
# front of it, which is the repair the batch route has always sent, and a
# second one costs a verse rather than a chapter where the first lands close.
# Past three, the verse is reported still owed and a person looks at it —
# asking a fourth time is money spent to reach the same misreading.
TOOL_FREE_ATTEMPTS = 3

# How many times a request that never reached an answer is sent again, and how
# long the pass waits first. This is not the repair loop above: nothing came
# back to repair. A connection dropped mid-response or an upstream said it was
# overloaded, and the same request a moment later is the whole remedy — where
# the alternative, on the packed route, was seventeen single-verse requests
# billed at a chapter's price to work around one lost socket.
#
# Two, because a transport that is still failing on the third send is not a
# blip and a pass that keeps sending into it burns the window it will need to
# resume. The wait doubles so the second attempt is not sent into the same
# second of whatever went wrong.
TRANSIENT_RETRIES = 2
TRANSIENT_PAUSE = 20.0

RESPONSE_SCHEMAS = {
    "packet": "config/alignment-response.schema.json",
    "review": "config/review-response.schema.json",
}
# Kept as the public name for callers and tests that mean the canonical
# alignment response. Review has its own transport schema, not a new accepted
# ledger shape.
RESPONSE_SCHEMA = RESPONSE_SCHEMAS["packet"]


def response_schema(root: Path = ROOT, mode: str = "packet") -> str:
    """This pass's transport schema, in the form the CLI will accept.

    The `$schema` line is dropped and nothing else is: Claude Code validates the
    schema document itself before sending it, under a draft-07 validator that
    does not know the 2020-12 meta-schema, and refuses the whole request with
    `no schema with key or ref "https://json-schema.org/draft/2020-12/schema"`.
    The keywords this file uses are the ones both drafts share, so removing the
    declaration changes what is enforced not at all — and it is removed here,
    at the point of the call that objects, rather than in the file, which is the
    contract the provider routes and the validator's own documentation cite.
    """
    if mode not in RESPONSE_SCHEMAS:
        raise ValueError(f"unknown mode {mode}; expected one of {list(MODES)}")
    schema = json.loads(
        (root / RESPONSE_SCHEMAS[mode]).read_text(encoding="utf-8")
    )
    schema.pop("$schema", None)
    return json.dumps(schema, ensure_ascii=False)


def combined_result(
    attempts: Sequence[SessionResult], output: str, failed: bool,
    label: str = "",
) -> SessionResult:
    """One unit of work's attempts as one row in the ledger.

    A retried verse cost what both of its sessions cost, and a ledger that
    recorded only the last would report the repair as though the first had been
    free. So the money and the turns add up, while the session id is the last
    one — that is the session someone asking what happened to verse 14 wants to
    resume into, and `sessions_for` already keeps the newest attempt for the
    same reason.

    The label is the exception, and needs saying: for a single verse the last
    attempt's label is the verse, but for a pack the last attempt is whichever
    verse was re-asked alone at the end. HEB.11 recorded a 40-verse request as
    `HEB.11.16` for that reason — the one verse the pack got wrong — which
    reads as a single verse that cost $4.95. So a caller that knows what the
    row is about says so.
    """
    last = attempts[-1]
    usage: dict[str, Any] = {}
    for attempt in attempts:
        for key, value in attempt.usage.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    return SessionResult(
        label=label or last.label,
        returncode=1 if failed else 0,
        output=output,
        usage=usage,
        cost_usd=sum(attempt.cost_usd for attempt in attempts),
        turns=sum(attempt.turns for attempt in attempts),
        session_id=last.session_id,
        payload=last.payload,
        child_sessions=(
            sum(1 + attempt.child_sessions for attempt in attempts) - 1
        ),
    )


def transient_pause(seconds: float) -> None:
    """Wait between sends. A seam, so a test never sleeps for real."""
    time.sleep(seconds)


def send_until_answered(
    request: Callable[[], SessionResult], *, label: str,
    tries: list[SessionResult],
    retries: int = TRANSIENT_RETRIES,
    log: Callable[[str], None] = print,
) -> SessionResult:
    """Send one request, resending it while the transport is what failed.

    The distinction this draws is the one the packed route was missing. A
    request that came back wrong is evidence — about the verse, about the size
    of the pack — and every caller here already knows what to do with it. A
    request that never came back is not evidence about anything, and the two
    were being treated alike: `2TI.3` lost one review pack to a dropped
    connection and answered it by asking all seventeen verses on their own,
    which is the most expensive thing the pass can do and had nothing to do
    with what went wrong.

    Every attempt is appended to `tries`, failures included, so the ledger
    charges the pass what the retries actually cost rather than reporting the
    one that worked as though it had been the only one.
    """
    for attempt in range(max(retries, 0) + 1):
        result = request()
        tries.append(result)
        if result.ok or not looks_transient(result.output):
            return result
        if attempt >= max(retries, 0):
            return result
        log(f"    {label}: {first_line(result.output)}; "
            "the transport failed, sending it again")
        transient_pause(TRANSIENT_PAUSE * (attempt + 1))
    raise AssertionError("unreachable: the loop returns on every path")


def first_line(text: str) -> str:
    """What a failure said, short enough to sit in a log line."""
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()[:120]
    return "no output"


def run_tool_free_verse(
    ref: str, mode: str, briefing: str, *,
    session: Callable[..., SessionResult],
    scope: ChapterScope, stable: str, effort: str, schema: str,
    reviewer: str, root: Path = ROOT, attempts: int = TOOL_FREE_ATTEMPTS,
    log: Callable[[str], None] = print,
) -> SessionResult:
    """Ask one verse for its alignment, and land it here.

    The division the provider routes have always used, brought to the CLI: the
    model is given the briefing and no tools, returns the response object and
    nothing else, and the parent validates it and writes the file. Three turns
    become one — a session that renders, writes, and validates re-reads its
    whole briefing at each of them — and the failure that used to be possible
    stops being: a model that cannot reach the filesystem cannot report a write
    it did not make, which is what `PHP.4.11` did with the tools it had.

    `apply_result` is called rather than reimplemented. It is the same landing
    the batch and synchronous routes use, down to the asymmetry that matters —
    alignment writes what came back because the alternative is no file at all,
    while a review correction that fails the validator is discarded rather than
    allowed over work that already passed it.
    """
    sent = briefing
    tries: list[SessionResult] = []
    for attempt in range(1, max(attempts, 1) + 1):
        prompt = sent
        result = send_until_answered(
            lambda: session(
                prompt, effort=effort, label=ref, tools=NO_TOOLS,
                system_append=stable, isolated=True, schema=schema,
            ),
            label=ref, tries=tries, log=log,
        )
        if not result.ok:
            return combined_result(tries, result.output, failed=True)
        if result.payload is None:
            # The schema was asked for and the envelope came back without it,
            # so nothing was judged and there is nothing to repair. Reported
            # against the request rather than the verse, which is what it is.
            return combined_result(
                tries,
                "the session returned no structured output; its reply was: "
                f"{result.output[-600:]}",
                failed=True,
            )
        outcome, errors = apply_result(
            root, scope, mode,
            Result(custom_id=custom_id(ref), payload=result.payload),
            reviewer,
        )
        if not errors:
            return combined_result(tries, outcome, failed=False)
        summary = "; ".join(errors)[:600]
        if outcome == "malformed" or attempt >= max(attempts, 1):
            return combined_result(
                tries, f"{outcome} after {attempt} attempt(s): {summary}",
                failed=True,
            )
        log(f"    {ref}: {outcome}, asking again with the validator's words")
        sent = repair_preamble(errors) + briefing
    raise AssertionError("unreachable: the loop returns on every path")


def run_packed_verses(
    group: Sequence[str], mode: str, bodies: dict[int, str], *,
    session: Callable[..., SessionResult],
    scope: ChapterScope, stable: str, effort: str, schema: str,
    packed_schema: str, reviewer: str,
    root: Path = ROOT, attempts: int = TOOL_FREE_ATTEMPTS,
    log: Callable[[str], None] = print,
) -> SessionResult:
    """Ask for several verses in one request, and land each of them here.

    The saving is the shared half of a briefing. A verse's own evidence is a
    few hundred tokens and the contract in front of it is fifteen thousand, and
    although a prefix cache already makes the second reading of that contract
    cheap, it does not make it free — eight verses in one request read it once
    at full price instead of once at full price and seven at cache rates.

    What is deliberately *not* shared is the judgment. Every returned object
    goes through `apply_result` on its own, against its own packet, exactly as
    a single-verse request's does: a packed pass writes nothing a one-verse
    pass would not have written, and a verse whose object is missing, malformed
    or rejected is not landed because its neighbours were fine.

    **A pack is one request, and it either works or it fails.** There is no
    fallback of any kind: no fanning out to a request per verse, no halving the
    pack, no asking the pack again with the validator's words. Every one of
    those was tried and each turns a bad pack into several bad requests — the
    fan-out most expensively of all, since it buys the whole unpacked pass to
    repair a fraction of it. One measured review returned one object for a
    pack of eight and paid for thirteen requests to land seven verses; one
    failed request would have said the same thing much more cheaply.

    The single exception is not a fallback: a request that never reached an
    answer is sent again unchanged by `send_until_answered`, because a dropped
    connection is not evidence about the work.

    So a pack that comes back wrong reports what was wrong with it and the pass
    halts for a person to read. Verses that did validate are still written,
    because an accepted file is this project's checkpoint and a resume skips
    it; what is refused is another request, not the work already paid for.
    """
    tries: list[SessionResult] = []
    landed: list[str] = []
    failures: dict[str, str] = {}

    def attempt(refs: Sequence[str]) -> None:
        if not refs:
            return
        label = pack_label(refs)
        result = send_until_answered(
            lambda: session(
                packed_prompt(refs, bodies, mode), effort=effort, label=label,
                tools=NO_TOOLS, system_append=stable, isolated=True,
                schema=packed_schema,
            ),
            label=label, tries=tries, log=log,
        )
        if not result.ok:
            log(f"    {label}: the request failed; {first_line(result.output)}")
            for ref in refs:
                failures[ref] = result.output
            return
        if result.payload is None:
            log(f"    {label}: the request returned no structured output")
            for ref in refs:
                failures[ref] = result.output
            return
        payloads, shape_errors = pack_payloads(result.payload, refs)
        if shape_errors:
            log(f"    {label}: {'; '.join(shape_errors)[:300]}")
        for ref in refs:
            payload = payloads.get(ref)
            if payload is None:
                # The commonest way a pack goes wrong, and the one no response
                # schema catches: the wrapper is well-formed and holds fewer
                # objects than it was asked for.
                log(f"    {ref}: the pack did not return this verse")
                failures[ref] = "the pack did not return this verse"
                continue
            outcome, errors = apply_result(
                root, scope, mode,
                Result(custom_id=custom_id(ref), payload=payload),
                reviewer,
            )
            if errors:
                log(f"    {ref}: {outcome} in the pack; {'; '.join(errors)[:200]}")
                failures[ref] = "; ".join(errors)
                continue
            landed.append(ref)

    attempt(list(group))
    kept = sorted(set(landed), key=verse_number)
    label = pack_label(group)
    if failures:
        summary = "; ".join(
            f"{ref}: {reason[-200:]}" for ref, reason in sorted(
                failures.items(), key=lambda item: verse_number(item[0])
            )
        )
        return combined_result(
            tries,
            f"landed {len(kept)} of {len(group)} verse(s); {summary}",
            failed=True, label=label,
        )
    return combined_result(
        tries,
        f"landed {len(kept)} verse(s) in {len(tries)} request(s)",
        failed=False, label=label,
    )


# How many requests one bounded reconciliation is allowed to make of the
# driver. Three: the packet, one evidence round or one repair, and the answer.
# The bound is the point — a reconciliation that keeps asking has become the
# session this replaces, one round trip at a time.
ADJUDICATION_ROUNDS = 3


def run_adjudication(
    chapter: str, *,
    session: Callable[..., SessionResult],
    root: Path = ROOT, log: Callable[[str], None] = print,
    model: str = "", report: str = "", effort: str = RECONCILE_EFFORT,
    parts: Callable[..., tuple[str, str]] = adjudication_parts,
    answers: Callable[..., str] = answer_requests,
    rounds: int = ADJUDICATION_ROUNDS, repairs: int = 0,
) -> tuple[SessionResult, dict[str, Any]]:
    """Reconcile one chapter as a bounded request, and land what it decided.

    The packet is built once and the chapter is asked once. Two things can buy
    a second ask and nothing else can: a response that is not about this
    chapter, which gets its own errors back, and a response that asks for
    evidence, which gets that evidence from machinery — a verse's token table
    or a corpus query, both of which are function calls here and were tool
    turns before. Each ask is a fresh session carrying what it needs, never a
    continuation, for the reason a rejected verse gets a fresh session: the
    reader least able to see a wrong reading is the one that just produced it.

    What comes back is applied by `apply_adjudication`, which validates every
    patch against its own packet before it overwrites anything. So the worst
    case is a chapter unchanged and a run that says which patches it refused —
    never a chapter half-corrected by a model that could not be checked.

    `repairs` raises the number of *sends* this pass may make without raising
    the number of times it may ask. The two are not the same thing and only one
    of them is what `rounds` is protecting: an evidence request is the
    reconciliation reaching for something, and letting it reach twenty times is
    how it becomes the open-ended session this shape replaced. A rejected
    response is the opposite — it asked for nothing and got nothing, and on an
    endpoint that enforces no schema it is the ordinary failure rather than a
    signal. So a free route resends a malformed answer as often as a free verse
    is resent, while the evidence bound below stays exactly where it was.
    """
    stable, packet = parts(root, chapter, report=report)
    schema = adjudication_schema(root)
    prompt = packet
    tries: list[SessionResult] = []
    sends = max(max(rounds, 1), repairs)
    for attempt in range(1, sends + 1):
        sent = prompt
        # A resend is not one of the rounds. The rounds bound how many times
        # this reconciliation may *ask* for something; a lost connection asked
        # for nothing.
        result = send_until_answered(
            lambda: session(
                sent, effort=effort, label="reconcile", tools=NO_TOOLS,
                system_append=stable, isolated=True, schema=schema,
            ),
            label="reconcile", tries=tries, log=log,
        )
        if not result.ok:
            return combined_result(tries, result.output, failed=True), {}
        if result.payload is None:
            # Not a fatal answer where a resend is free and the endpoint
            # enforces nothing: a reply that is prose rather than an object is
            # the same failure as one that is an object of the wrong shape, and
            # it is repaired the same way. Where nothing raised the repair
            # budget it stays fatal on the first reply, as it always was — a
            # billed route that answers with prose is a finding, and asking it
            # twice is paying twice for the same one.
            if repairs <= max(rounds, 1) or attempt >= sends:
                return combined_result(
                    tries,
                    "the reconciliation returned no structured output; its "
                    f"reply was: {result.output[-600:]}",
                    failed=True,
                ), {}
            log("    reconcile: the reply was not the JSON object this asks for")
            prompt = "\n".join([
                "=== Your previous response was not the object this pass asks for",
                "  Return one JSON object matching the schema and nothing else",
                "  — no prose, no markdown fence.",
                "",
                "",
                packet,
            ])
            continue
        errors = validate_adjudication(chapter, result.payload)
        if errors and attempt < sends:
            log(f"    reconcile: {'; '.join(errors)[:300]}")
            prompt = "\n".join([
                "=== Your previous response for this chapter was rejected",
                *(f"  - {error}" for error in errors),
                "  Return the whole object again, corrected.",
                "",
                "",
                packet,
            ])
            continue
        if errors:
            return combined_result(
                tries, f"rejected after {attempt} attempt(s): "
                f"{'; '.join(errors)[:600]}", failed=True,
            ), {}
        requests = result.payload.get("requests") or []
        if requests and result.payload.get("verdict") == "needs-evidence":
            if attempt >= rounds - 1:
                # Out of rounds with the chapter still asking. Nothing is
                # written, and the run says so: a reconciliation that ends
                # owing evidence is a chapter for a person to look at, not one
                # to keep paying for.
                return combined_result(
                    tries,
                    f"still asking for evidence after {attempt} round(s): "
                    + "; ".join(
                        f"{item.get('kind')} {item.get('value')}"
                        for item in requests[:4]
                    ),
                    failed=True,
                ), {}
            log(f"    reconcile: answering {len(requests)} evidence request(s)")
            prompt = answers(root, chapter, requests) + "\n\n" + packet
            continue
        outcome = apply_adjudication(root, chapter, result.payload, model=model)
        failed = bool(outcome["rejected"])
        summary = outcome["verdict"] or "no verdict"
        if outcome["applied"]:
            summary += f", {len(outcome['applied'])} verse(s) corrected"
        if outcome["rejected"]:
            summary += (
                f", {len(outcome['rejected'])} patch(es) refused by the "
                "validator: " + "; ".join(
                    f"{ref}: {'; '.join(reasons)}"
                    for ref, reasons in outcome["rejected"].items()
                )[:600]
            )
        promoted = outcome.get("promoted") or {}
        if promoted.get("rules") or promoted.get("amended"):
            edits = []
            if promoted.get("rules"):
                edits.append(f"{len(promoted['rules'])} rule(s) promoted")
            if promoted.get("amended"):
                edits.append(f"{len(promoted['amended'])} amended")
            summary += f", {' and '.join(edits)} in {promoted['version']}"
        if promoted.get("refused"):
            summary += f", promotion refused: {promoted['refused']}"
        return combined_result(tries, summary, failed=failed), outcome
    raise AssertionError("unreachable: the loop returns on every path")


def decisions_prompt(chapter: str, briefing: str) -> str:
    """The whole briefing for one chapter's textual decisions, carried inline.

    The render is included rather than cited. Telling a session to produce its
    own briefing costs a turn — and a turn is a re-read of everything before
    it, which for this pass is the largest thing it pays for. The driver can
    build the same render in-process for nothing, and a pass that opens with
    its units already in front of it has one job left to do.
    """
    return (
        f"Resolve every textual decision {chapter} still owes, and nothing else.\n\n"
        "Below is your complete briefing: the policy, the rule for deciding a "
        "unit, the command that records them, and every unit awaiting judgment "
        "with its candidate readings, its ASV verse, and that verse's notes. "
        "Reason from it directly — it is current as of this moment and there is "
        "nothing to render.\n\n"
        "A unit is selected only where the ASV distinguishes a reading, "
        "underdetermined otherwise; both need evidence naming what decided it. "
        "Record every unit below in a single `decide-many` command.\n\n"
        "Do not edit the decisions file yourself, do not open the vendor "
        "apparatus, README.md, or AGENTS.md, do not align any verse, and do not "
        "commit. Stop when the briefing's units are all recorded.\n\n"
        f"{briefing}"
    )


def reconcile_prompt(chapter: str, briefing: str) -> str:
    """The coordinating session that alone is allowed the whole chapter.

    Everything here is either cross-verse or a judgment, which is exactly what
    per-verse isolation cannot produce and a script cannot make. Its briefing
    carries the protocol for delegating independent evidence investigations
    without giving those agents the chapter or making them competing writers.

    The render is included rather than cited, for the reason `decisions_prompt`
    gives: telling a session to produce its own briefing costs a turn, and this
    briefing is the largest thing the pass reads. Worse than the turn, it is
    large enough that a harness may hand it back as a path to a saved file, so
    the session pays several more turns paging in what the driver already had
    in memory.

    The briefing leads and the instructions follow, which is `render_stable_prefix`
    holding at chapter granularity: that briefing opens with the testament
    contract, which is byte-identical for every chapter of the testament, so a
    prompt that opens with it lets one reconciliation's contract stay cached
    for the next chapter's. Naming the chapter first would make every chapter a
    fresh prefix and pay for the contract once per chapter — the same mistake
    the render already refuses for verses. Nothing is lost by the order: the
    briefing opens by saying what this session is.
    """
    return (
        f"{briefing}\n\n"
        f"That briefing is complete and current as of this moment; there is "
        "nothing to render. The chapter it describes is "
        f"{chapter}, which has been aligned and reviewed one verse at a time, "
        "by sessions that could not see each other. Coordinate its "
        "reconciliation, working from the briefing above directly. The only "
        "views worth running are corpus-query, and reconcile-view after a "
        "compile; the briefing says when.\n\n"
        "Delegate the bounded investigations that briefing describes, in "
        "parallel where they are independent. This is a standing request, so a "
        "session told not to spawn agents unless the user asks has been asked "
        "here; if something still forbids it, say so in your report rather than "
        "silently working alone.\n\n"
        "Do not read AGENTS.md or README.md — they are the coordinator's process "
        "and none of it governs these judgments — and do not reopen any chapter but "
        f"{chapter}. Do not run the finishing checks and do not commit; this run "
        "does both when you return."
    )


# Tool inputs worth showing while a long session runs; everything else prints
# just the tool name.
PROGRESS_KEYS = ("file_path", "command", "pattern", "description", "prompt")


def progress_line(event: dict[str, Any]) -> str | None:
    """One readable line for a stream-json event, or nothing worth printing."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return f"session {event.get('session_id', '?')} ({event.get('model', '?')})"
    if kind != "assistant":
        return None
    for block in event.get("message", {}).get("content", []):
        if block.get("type") == "text":
            text = " ".join(block.get("text", "").split())
            if text:
                return text[:160]
        if block.get("type") == "tool_use":
            payload = block.get("input", {}) or {}
            name = block.get("name", "?")
            for key in PROGRESS_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return f"{name}: {' '.join(value.split())[:120]}"
            return name
    return None


def result_from_event(label: str, event: dict[str, Any], returncode: int) -> SessionResult:
    """Build a result from the stream's closing `result` event."""
    failed = returncode != 0 or bool(event.get("is_error"))
    return SessionResult(
        label,
        returncode=(returncode or 1) if failed else 0,
        output=str(event.get("result") or "")[-4000:],
        usage=event.get("usage") or {},
        cost_usd=float(event.get("total_cost_usd") or 0.0),
        turns=int(event.get("num_turns") or 0),
        session_id=str(event.get("session_id") or ""),
    )


TRANSCRIPT_USAGE_KEYS = (
    "input_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "output_tokens",
)


def _claude_jsonl_usage(path: Path) -> dict[str, dict[str, int]]:
    """Deduplicated request usage from one Claude transcript.

    One assistant response may be persisted as several growing fragments with
    the same request and message IDs. Retaining the maximum value of each field
    counts the completed request once and tolerates a truncated final fragment.
    """
    requests: dict[str, dict[str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return requests
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        request_id = record.get("requestId") or message.get("id")
        if not isinstance(request_id, str) or not request_id:
            continue
        usage = requests.setdefault(request_id, {})
        for key in TRANSCRIPT_USAGE_KEYS:
            value = message["usage"].get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = max(usage.get(key, 0), int(value))
    return requests


def claude_transcript_usage(
    session_id: str, env: dict[str, str] | None = None,
) -> tuple[dict[str, int], int] | None:
    """Whole parent-and-child usage for one persisted Claude session.

    Returns no result unless the main transcript itself contains usable
    requests, so an absent, disabled, or changing transcript format falls back
    to the provider's closing envelope rather than replacing it with zeros.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    effective = os.environ if env is None else env
    configured = effective.get("CLAUDE_CONFIG_DIR")
    if configured:
        config = Path(configured)
    else:
        process_home = effective.get("HOME")
        config = (
            Path(process_home) / ".claude"
            if process_home else Path.home() / ".claude"
        )
    candidates = list((config / "projects").glob(f"*/{session_id}.jsonl"))
    if not candidates:
        return None
    try:
        main = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    except OSError:
        return None
    main_requests = _claude_jsonl_usage(main)
    if not main_requests:
        return None
    transcript_requests = [main_requests]
    child_sessions = 0
    children = main.with_suffix("") / "subagents"
    if children.exists():
        for child in children.rglob("*.jsonl"):
            child_requests = _claude_jsonl_usage(child)
            if child_requests:
                child_sessions += 1
                transcript_requests.append(child_requests)
    usage = {
        key: sum(
            request.get(key, 0)
            for requests in transcript_requests for request in requests.values()
        )
        for key in TRANSCRIPT_USAGE_KEYS
    }
    return usage, child_sessions


def with_claude_transcript_usage(
    result: SessionResult, env: dict[str, str] | None = None,
) -> SessionResult:
    """Replace a compacted envelope's usage with the complete transcript."""
    aggregate = claude_transcript_usage(result.session_id, env)
    if aggregate is None:
        return result
    usage, child_sessions = aggregate
    return replace(result, usage=usage, child_sessions=child_sessions)


# What one `claude -p` session may write before the CLI refuses the response.
#
# Claude Code stops a response at 64,000 output tokens by default and says so:
# `API Error: Claude's response exceeded the 64000 output token maximum. To
# configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment
# variable.` That is the CLI's ceiling, not the model's — Opus 5 writes up to
# 128,000 — and a packed alignment pass is the one thing here that comes near
# it: LUK.1 answered 80 verses in 126,681 output tokens, while MAT.26 (75
# verses) and MRK.14 (72) each hit the cap four times over and halted, at $8.13
# and $7.81 for nothing landed. The pack size was never what separated them:
# LUK.1 is the longer chapter and answered under the same cap.
#
# Raising a ceiling costs nothing when it is not reached, and the pass that
# reaches it is bisected rather than halted. An explicit value in the
# environment still wins, because a person who exported one meant it.
SESSION_OUTPUT_TOKENS = "128000"


# What Claude Code reads to bill the API instead of the subscription. A session
# started with any of these set is charged at list rates — roughly ten times
# what the same work costs on the subscription — and says nothing about it: the
# run looks identical and the difference appears on an invoice later.
#
# This is not a hypothetical. GEN.6 was aligned on Gemini and reviewed on
# Claude Code, and loading the Gemini key put every key from `keys.txt` into
# this process's environment, including the Anthropic one. All 22 review
# sessions inherited it. The credential loader no longer writes to the
# environment at all, and this closes the same door from the other side, where
# it also catches a key the user exported for their own reasons.
SESSION_BILLING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


# The same door on the OpenAI side. `codex` signed in through ChatGPT spends
# the subscription; `codex` with a key in its environment spends the API, at
# rates this repository's own `openai` route already exists to reach on purpose.
#
# A key here is fatal rather than stripped — `ensure_routes_usable` refuses the
# run before its first session, because a route billed to the wrong account
# completes normally and reports nothing unusual, exactly as GEN.6's 22 reviews
# did on the Anthropic side. The scrub below is the second lock on the same
# door, for the paths that do not come through a route check.
CODEX_BILLING_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL")


def session_env(
    extra: dict[str, str] | None = None,
    strip: Sequence[str] = SESSION_BILLING_VARS,
) -> dict[str, str]:
    """This process's environment, with anything that changes billing removed.

    A `claude -p` session is meant to spend the subscription. Nothing in this
    repository ever wants one billed to the API, so rather than being careful
    about what gets exported, the subprocess is simply never handed the means.
    `strip` is which vendor's means: a `codex exec` session wants the same
    protection from a different variable.

    `extra` is how the driver tells a session a fact about the run rather than
    about the verse — which model is reviewing, so the receipt can say so.
    """
    return {
        **{
            name: value for name, value in os.environ.items()
            if name not in strip
        },
        **(extra or {}),
    }


# Linux caps one argv entry at MAX_ARG_STRLEN — 32 pages, 128 KiB — however
# much room `ARG_MAX` leaves for the rest. Nothing raises it; a process that
# hands `execve` a longer string gets E2BIG, which arrives from inside `Popen`
# as `OSError: [Errno 7] Argument list too long` with no mention of which
# argument was too long.
#
# Prompts avoid the cap entirely by travelling on stdin, and the contract by
# travelling in a file: the Greek contract crossed 128 KiB during James and
# every JAS.5 verse died before it was spawned, so `--append-system-prompt`
# gave way to `--append-system-prompt-file`, whose value is a path. Nothing
# this driver builds is bounded by anything but a file size now.
#
# The cap is still checked before spawning, because an argument that has no
# file form can still grow into it — a response schema, a tool list. A session
# that would have died in the kernel fails with a sentence naming the flag
# instead, in the same channel every other session failure uses, so a pass
# records it and a run resumes rather than unwinding as a traceback.
ARGUMENT_LIMIT = 128 * 1024


def oversized_argument(command: Sequence[str]) -> str:
    """The complaint about the first argument too long to spawn, if any."""
    for index, argument in enumerate(command):
        size = len(argument.encode("utf-8"))
        if size <= ARGUMENT_LIMIT:
            continue
        flag = command[index - 1] if index and command[index - 1].startswith("-") else ""
        named = f"the value of {flag}" if flag else f"argument {index}"
        return (
            f"{named} is {size} bytes, past the {ARGUMENT_LIMIT}-byte limit one "
            "command-line argument can be; it has to travel on stdin or shrink"
        )
    return ""


@contextmanager
def argument_file(text: str, suffix: str = ".md") -> Iterator[str]:
    """A temporary file holding `text`, for a flag that takes a path.

    It is deleted when the session that read it has exited, which is what the
    caller's `ExitStack` is for: the CLI reads the file at startup, so the file
    has to outlive the spawn and nothing else.
    """
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix="asv-", suffix=suffix, delete=False,
    )
    try:
        with handle:
            handle.write(text)
        yield handle.name
    finally:
        Path(handle.name).unlink(missing_ok=True)


def claude_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a `claude -p` session runs in.

    `session_env` minus the means to bill the API, plus the output ceiling this
    work actually needs. A value already in the environment is left alone: a
    person who exported one meant it, and this is a default rather than a
    policy.
    """
    return session_env({
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": (
            os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or SESSION_OUTPUT_TOKENS
        ),
        **(extra or {}),
    })


def stream_session(
    command: list[str], label: str, root: Path, log: Callable[[str], None],
    env: dict[str, str] | None = None, prompt: str = "",
) -> SessionResult:
    """Run a session and narrate it, line by line, as it works.

    A reconciliation session can run for over an hour on a long chapter, and a
    process that prints nothing for an hour is indistinguishable from one that
    has hung. Streaming is what tells those apart — and it is also how a run
    that genuinely is stuck becomes something a person can see and interrupt,
    which is the job a timeout would otherwise be doing badly.

    `prompt` is written to the session's stdin rather than carried in `command`,
    for the reason `claude_session` gives. It is written and the pipe closed
    before the first line is read: these prompts are far larger than a pipe
    buffer, but the CLI reads stdin to EOF before it prints anything, so there
    is no output to deadlock against.
    """
    closing: dict[str, Any] = {}
    effective_env = claude_env(env)
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, text=True, bufsize=1, env=effective_env,
    )
    assert process.stdin is not None
    process.stdin.write(prompt)
    process.stdin.close()
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            closing = event
            continue
        message = progress_line(event)
        if message:
            log(f"    [{label}] {message}")
    process.wait()
    if not closing:
        return SessionResult(
            label, process.returncode or 1, "session ended without a result event",
        )
    return with_claude_transcript_usage(
        result_from_event(label, closing, process.returncode), effective_env,
    )


def claude_session(
    prompt: str, effort: str | None = None, label: str = "session",
    model: str = "opus", root: Path = ROOT, timeout: int | None = None,
    stream: bool = False, log: Callable[[str], None] = print,
    env: dict[str, str] | None = None, tools: Sequence[str] | None = None,
    system_append: str | None = None, isolated: bool = False,
    schema: str | None = None,
) -> SessionResult:
    """Spawn one headless Claude Code session and wait for it.

    `--output-format json` supplies the authoritative `total_cost_usd`. For an
    agentic session its usage object can cover only the coordinator's final
    compacted segment, so after the process exits the driver reconstructs token
    usage from the persisted parent-and-child transcript tree. If persistence
    is disabled or that tree is unavailable, the envelope remains the fallback.

    There is no timeout by default. A verse pass is minutes and a reconciliation
    can be an hour, so any bound tight enough to catch a hang is also tight
    enough to kill real work — and killing an hour-old session throws away
    everything it had not yet written. `timeout` remains for a caller that
    genuinely wants one.

    The prompt travels on stdin, never as an argument. Linux caps a single
    argv entry at MAX_ARG_STRLEN — 128 KiB, a constant no `ulimit` raises — and
    a reconciliation briefing carrying its chapter's candidate table passes that
    on an ordinary chapter: 1JN.2 built a 149 KiB prompt and died in `Popen`
    with `Argument list too long`, after both verse passes had been paid for.
    stdin has no such cap, both CLIs read a prompt from it, and no prompt this
    driver builds has a bound worth trusting, so none of them go through argv.

    `tools` narrows the built-in set the session is given. It is passed before
    the model flag deliberately: `--tools` is variadic, so anything after it and
    before the next flag is read as another tool name — and every flag after it
    here supplies its own value, which is what keeps the list from eating one.
    `NO_TOOLS` empties the set; `None`, the default, asks for all of them. The
    two are told apart by `is not None` and not by truthiness, because an empty
    allowlist read as "unset" is a session handed every tool there is.

    `schema` holds the reply to a JSON Schema — the same one the provider
    routes send — and turns the envelope's `structured_output` into a parsed
    object the caller can validate and land itself. It is what makes a verse
    pass tool-free: a model that cannot write the file has to return it, and a
    model returning it need not be trusted to have written it. Note that the
    CLI validates the schema document under draft-07, so `response_schema`
    hands it the contract's schema without its 2020-12 `$schema` line.

    `system_append` is the half of a briefing that does not change between the
    sessions of a pass. It goes to the system prompt rather than the prompt
    because that is the part of a request consecutive sessions share a cache
    for: sent in the prompt it would be written fresh by every verse, and
    written is five times the price of read.

    It travels as a file, on `--append-system-prompt-file`, for the reason the
    prompt travels on stdin: a contract that grows a few KiB a chapter reached
    128 KiB and every verse of JAS.5 failed to spawn. The text reaching the
    request is identical either way, so the prefix cache does not notice.

    `isolated` is what makes that cache actually hold. Everything this
    repository configures — `CLAUDE.md`, the `align-chapter` skill, any
    `settings.local.json`, hooks, MCP servers — is assembled into the system
    prompt *ahead* of `--append-system-prompt`, so editing any one of them
    changes the prefix and the whole pass pays to write the contract again
    rather than read it. That is four fifths of a verse briefing lost to files
    the pass never uses, and nothing reports it: the pass still runs, still
    validates, and simply costs what an uncached one costs.

    Dropping them is also right on its own terms for a verse pass. The briefing
    says in as many words that it is complete and there is nothing to open,
    while `CLAUDE.md` is a pointer at a thousand-line coordinator's process — a
    worker that follows it has read all of that to align one verse. A pass that
    genuinely ranges over this repository, reconciliation and textual decisions,
    keeps its configuration; hence a flag and not a default.

    `--no-session-persistence` travels with it. A driven verse session is
    resumed by re-running the pass against the files it did or did not write,
    never by replaying its transcript, so writing that transcript buys nothing.
    """
    with ExitStack() as stack:
        command = ["claude", "-p"]
        if isolated:
            command += ["--safe-mode", "--no-session-persistence"]
        if tools is not None:
            command += ["--tools", ",".join(tools)]
        if system_append:
            command += [
                "--append-system-prompt-file",
                stack.enter_context(argument_file(system_append)),
            ]
        if schema:
            command += ["--json-schema", schema]
        command += ["--model", model, "--permission-mode", "auto"]
        command += (
            ["--output-format", "stream-json", "--verbose"] if stream
            else ["--output-format", "json"]
        )
        if effort:
            command += ["--effort", effort]
        oversized = oversized_argument(command)
        if oversized:
            return SessionResult(label, returncode=1, output=oversized)
        if stream:
            try:
                return stream_session(command, label, root, log, env=env, prompt=prompt)
            except FileNotFoundError:
                return SessionResult(label, 127, "claude executable not found")
        effective_env = claude_env(env)
        try:
            finished = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=timeout,
                env=effective_env, input=prompt,
            )
        except subprocess.TimeoutExpired:
            return SessionResult(label, returncode=124, output="session timed out")
        except FileNotFoundError:
            return SessionResult(
                label, returncode=127, output="claude executable not found",
            )
    return with_claude_transcript_usage(
        decode_session(label, finished.returncode, finished.stdout, finished.stderr),
        effective_env,
    )


def decode_session(
    label: str, returncode: int, stdout: str | None, stderr: str | None,
) -> SessionResult:
    """Read the JSON envelope, falling back to raw text if it is not there.

    A session that died before printing its envelope still has to produce a
    result, and one that reports `is_error` has failed even where the process
    exited zero.
    """
    text = (stdout or "").strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return SessionResult(label, returncode or 1, text + (stderr or ""))
    if not isinstance(envelope, dict):
        return SessionResult(label, returncode or 1, text)
    failed = returncode != 0 or bool(envelope.get("is_error"))
    structured = envelope.get("structured_output")
    return SessionResult(
        label,
        returncode=(returncode or 1) if failed else 0,
        output=str(envelope.get("result") or "")[-4000:],
        usage=envelope.get("usage") or {},
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        turns=int(envelope.get("num_turns") or 0),
        session_id=str(envelope.get("session_id") or ""),
        payload=structured if isinstance(structured, dict) else None,
    )


# What `codex exec` is given, beyond the model and the prompt.
#
# `--json` is the measurable form, exactly as `--output-format json` is for
# Claude Code: the events carry the thread id and the closing token counts, so
# a run reports what it spent without anyone reading a transcript.
#
# `--sandbox workspace-write` is the `--permission-mode auto` of this CLI: the
# session writes its verse and runs the validator without being asked, and
# cannot reach outside the repository. `codex exec` never prompts, but the
# approval policy is pinned anyway so that the user's configuration
# cannot make an unattended run sit waiting for an answer.
#
# `project_doc_max_bytes=0` is the one that is about this repository rather than
# about Codex. Codex loads `AGENTS.md` into every session automatically, and
# AGENTS.md here is the coordinator's process — a thousand lines that govern
# none of a verse's judgments, and which every pass is explicitly told not to
# read. Measured on a bare prompt it was 7,846 tokens per session; a chapter of
# 40 verses over two passes pays that 80 times for instructions it must ignore.
CODEX_FLAGS = (
    "--json",
    "--sandbox", "workspace-write",
    "-c", "approval_policy=\"never\"",
    "-c", "project_doc_max_bytes=0",
)

# Codex counts its input the way OpenAI does — `input_tokens` is the whole
# prompt, with the cached part named separately as a subset of it. The report's
# columns are Claude Code's, where the two are disjoint, so the cached half is
# subtracted rather than double-counted.
def codex_usage(usage: dict[str, Any]) -> dict[str, int]:
    """One turn's token counts, in the columns the run report already has."""
    total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    return {
        "input_tokens": max(total - cached, 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": int(usage.get("cache_write_input_tokens") or 0),
        # Reasoning tokens are billed as output and are most of a high-effort
        # turn's output, so a figure that left them out would understate the
        # expensive half of the pass.
        "output_tokens": (
            int(usage.get("output_tokens") or 0)
            + int(usage.get("reasoning_output_tokens") or 0)
        ),
    }


def codex_cost(model: str, usage: dict[str, int]) -> float:
    """What this session would have cost at the model's list price.

    A ChatGPT subscription is not billed per token, so no invoice will ever
    carry this number — but a run report whose cost column reads $0.00 for a
    pass that plainly did work is worse than one that reads a comparable
    figure. Priced from the same `PRICES` table the batch path uses, so a
    chapter aligned on `codex` and one aligned on `sol` produce numbers that
    mean the same thing and can be set beside each other.

    An unpriced model is zero rather than a guess. `PRICES` carries only the
    models this project talks to, and inventing a rate for one it does not
    would put a made-up figure where a missing one belongs.
    """
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return price.cost(
        Usage(
            input_tokens=usage.get("input_tokens", 0),
            cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        ),
        batch=False,
    )


def codex_progress_line(event: dict[str, Any]) -> str | None:
    """One readable line for a codex event, or nothing worth printing."""
    kind = event.get("type")
    if kind == "thread.started":
        return f"session {event.get('thread_id', '?')}"
    if kind not in ("item.started", "item.completed"):
        return None
    item = event.get("item") or {}
    shape = item.get("type")
    if shape == "command_execution":
        if kind == "item.started":
            return f"shell: {' '.join(str(item.get('command', '')).split())[:120]}"
        return None
    if kind != "item.completed":
        return None
    if shape == "agent_message":
        text = " ".join(str(item.get("text", "")).split())
        return text[:160] or None
    if shape == "file_change":
        changed = item.get("changes") or []
        paths = ", ".join(str(change.get("path", "?")) for change in changed[:3])
        return f"edit: {paths[:120]}" if paths else "edit"
    if shape in ("mcp_tool_call", "web_search"):
        return shape
    return None


def codex_session(
    prompt: str, effort: str | None = None, label: str = "session",
    model: str = CODEX_DEFAULT_MODEL, root: Path = ROOT, timeout: int | None = None,
    stream: bool = False, log: Callable[[str], None] = print,
    env: dict[str, str] | None = None, tools: Sequence[str] | None = None,
    system_append: str | None = None, isolated: bool = False,
    schema: str | None = None,
) -> SessionResult:
    """Spawn one headless Codex CLI session and wait for it.

    The same contract as `claude_session` — same arguments, same result — so
    that a pass does not know which CLI ran it. Three of those arguments mean
    something different here, and each difference is a real one rather than a
    gap to be papered over:

    `system_append` has no flag to travel on: Codex has no
    `--append-system-prompt`, and its system half is `AGENTS.md`, which this
    repository deliberately keeps out of a verse session. So the shared half is
    prepended to the prompt instead. That still buys what it was sent for —
    prompt caching is a prefix match, the shared half is identical across the
    verses of a pass, and it is first — but it is written into every request's
    prompt rather than its system block.

    `tools` is not passed. Codex has no built-in tool allowlist; a session gets
    the shell and its patch tool and nothing narrower. What the flag buys on the
    Claude side — 26k tokens of schema a session re-reads every turn — is not
    available to be bought here, and a pass that quietly ran with the full set
    while believing it had asked for three tools would be worse than one that
    knows it did not ask.

    `isolated` is accepted and does nothing, for the same reason `tools` does
    nothing: there is no Codex equivalent to buy. What it asks for on the Claude
    side is that no repository configuration precede the shared half in the
    system prompt, and here the shared half is not in the system prompt at all —
    it leads the user turn, where nothing this repository configures can get in
    front of it. The argument stays in the signature so that a pass can be
    routed to either CLI without knowing which it got.

    `schema` travels on `--output-schema`, which takes a file. The reply's last
    agent message is then the object itself, parsed here into `payload` exactly
    as Claude Code's envelope is, so the parent can validate and land it. This
    used to say no such flag existed; it does, and a Codex verse pass can be
    tool-free after all — which is what `codex:packed=N` runs.

    What is still not available is an empty tool set: Codex has no allowlist, so
    a schema-held session keeps its shell whether or not it needs one. That
    costs tokens it should not and is why plain `codex` remains the agentic
    route rather than being quietly switched over.

    `timeout` behaves as it does there: none by default, for the same reason.
    """
    # The schema travels as a file, and the file has to outlive the spawn that
    # reads it — hence the stack around the call rather than around its
    # construction.
    with ExitStack() as stack:
        command = ["codex", "exec", *CODEX_FLAGS]
        command += ["--model", model]
        if effort:
            command += ["-c", f"model_reasoning_effort={effort}"]
        if schema:
            command += [
                "--output-schema",
                stack.enter_context(argument_file(schema, suffix=".json")),
            ]
        # `-` is this CLI's "the prompt is on stdin", which is where every prompt
        # here goes; `claude_session` says why an argument is the wrong place for
        # one. The shared half still leads, since it is still the prompt's prefix.
        command.append("-")
        stdin = f"{system_append}\n\n{prompt}" if system_append else prompt
        oversized = oversized_argument(command)
        if oversized:
            return SessionResult(label, returncode=1, output=oversized)
        try:
            return codex_stream(
                command, label, root, log if stream else None,
                env=env,
                timeout=timeout, model=model, prompt=stdin,
                structured=bool(schema),
            )
        except FileNotFoundError:
            return SessionResult(
                label, returncode=127, output="codex executable not found",
            )
        except subprocess.TimeoutExpired:
            return SessionResult(label, returncode=124, output="session timed out")


def codex_stream(
    command: list[str], label: str, root: Path,
    log: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None, timeout: int | None = None,
    model: str = CODEX_DEFAULT_MODEL, prompt: str = "",
    structured: bool = False,
) -> SessionResult:
    """Run a codex session, reading its events as they arrive.

    `codex exec --json` has only the streaming form — there is no single closing
    envelope to wait for, as `--output-format json` gives — so the events are
    read either way and `log` decides whether they are also narrated. Which
    means a long reconciliation is watchable for free, and a verse pass costs
    nothing extra for being readable.

    Nothing here is taken on the process's exit code alone: a turn that failed
    says so in its own event, and a session that never produced one has not
    finished whatever the shell thought.
    """
    usage: dict[str, Any] = {}
    session_id = ""
    messages: list[str] = []
    failure = ""
    finished = False
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, stdin=subprocess.PIPE,
        env=session_env(env, strip=CODEX_BILLING_VARS),
    )
    assert process.stdin is not None
    process.stdin.write(prompt)
    process.stdin.close()
    assert process.stdout is not None
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "thread.started":
                session_id = str(event.get("thread_id") or "")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                finished = True
            elif kind in ("turn.failed", "error"):
                error = event.get("error") or {}
                failure = str(
                    error.get("message") if isinstance(error, dict) else error
                ) or str(event.get("message") or "the turn failed")
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    messages.append(str(item.get("text") or ""))
            if log is not None:
                message = codex_progress_line(event)
                if message:
                    log(f"    [{label}] {message}")
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise
    stderr = (process.stderr.read() if process.stderr else "") or ""
    if failure or not finished:
        return SessionResult(
            label,
            returncode=process.returncode or 1,
            output=(failure or "session ended without completing a turn")
            + (f"\n{stderr.strip()[-800:]}" if stderr.strip() else ""),
            usage=codex_usage(usage),
            session_id=session_id,
        )
    # A session held to an `--output-schema` answers with the object as its last
    # message. Parsed here, so that a caller sees the same `payload` a Claude
    # envelope gives it and neither the pass nor the landing knows which CLI
    # answered. A reply that does not parse leaves it None, which the driver
    # already reports as a request that returned no structured output.
    payload = None
    if structured and messages:
        try:
            parsed = json.loads(messages[-1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    return SessionResult(
        label,
        returncode=process.returncode or 0,
        output="\n\n".join(messages)[-4000:],
        payload=payload,
        usage=codex_usage(usage),
        # What the same tokens would cost on the API, since the CLI reports no
        # price of its own and a subscription is not billed per token. Nobody
        # is charged this; it is here so the cost column means one thing across
        # every route, and so a `codex` chapter can be compared with a `sol` one.
        cost_usd=codex_cost(model, codex_usage(usage)),
        # Codex reports no turn count. What a reader wants this column for is
        # how much back-and-forth a verse took, and the assistant messages are
        # the closest honest answer: one for a review that changed nothing, more
        # where it worked.
        turns=len(messages),
        session_id=session_id,
    )


# A provider reports its input split differently from Claude Code, and the run
# report's columns are the Code ones. Translating here rather than widening the
# report keeps one set of columns: what was paid fresh, what a cache served.
API_USAGE_KEYS = {
    "input_tokens": "input_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "output_tokens": "output_tokens",
}


def run_sync_rounds(
    run_sync_pass: Callable[..., dict[str, Any]],
    root: Path, scope: ChapterScope, mode: str, backend: Any, *,
    width: int, rounds: int, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Ask a synchronous pass again for whatever it still owes.

    One round is what a billed route gets and what this has always done. More
    than one is for a route where a resend costs nothing, and the round after
    the first is not a repetition: `run_sync` starts by reading the rejections
    the last one recorded, and `repair_preamble` puts the validator's own words
    in front of every verse it resends. REV.15 is the measurement — four of its
    eight verses were rejected on the first round, for dropped IDs and unit
    shapes the validator names exactly.

    The last round's summary is returned, with its usage carrying the whole
    pass: what a report has to say is what the chapter owes now and what the
    attempts cost altogether, not what the final attempt alone did.
    """
    summary: dict[str, Any] = {}
    usage = Usage()
    cost = 0.0
    for round_number in range(1, max(rounds, 1) + 1):
        summary = run_sync_pass(root, scope.ref, mode, backend, width=width)
        usage = usage + Usage(**summary.get("usage", {}))
        cost += summary.get("cost_usd", 0.0) or 0.0
        remaining = summary.get("still_pending")
        if remaining is None:
            remaining = pending_refs(root, scope.ref, mode)
        if not remaining:
            break
        if round_number < max(rounds, 1):
            log(
                f"    {len(remaining)} verse(s) still owed after attempt "
                f"{round_number}; asking again with the validator's words"
            )
    summary["usage"] = usage.as_dict()
    summary["cost_usd"] = cost
    return summary


def run_api_pass(
    chapter: str, mode: str, route: Route, *,
    width: int = DEFAULT_WIDTH,
    root: Path = ROOT, log: Callable[[str], None] = print,
    run_batch_pass: Callable[..., dict[str, Any]] = run_batch,
    run_sync_pass: Callable[..., dict[str, Any]] = run_sync,
) -> dict[str, Any]:
    """Run every verse this mode still owes through a provider endpoint.

    The same pass as the one above and the same work list — `pending_refs`
    asks the filesystem either way — so a chapter half aligned by one route
    is finished by the other and neither knows. What differs is only what a
    verse costs and how long it takes to answer, which is the whole reason a
    route is a flag rather than a fork in the pipeline.

    A synchronous route fans out exactly as a session pass does — one verse
    alone to pay for the prefix cache, then a rolling pool of `width` readers.
    A batch ignores `width`: the whole pass is one submission and the provider
    decides what runs when.

    There is no quota back-off here because there is no window to protect: an
    API bills per token rather than rationing a five-hour allowance. What
    bounds this pass is the batch runner's attempt limit and, past that, the
    verses it reports as still owed.
    """
    scope = chapter_scope(chapter)
    owed = pending_refs(root, scope.ref, mode)
    effort = route.resolved_effort(EFFORTS[mode])
    backend = build_backend(
        route.provider, route.model, root=root, effort=effort, mode=mode,
    )
    log(
        f"==> {mode} {scope.ref}: {len(owed)} verse(s) through "
        f"{route.describe()} at {effort} effort"
    )
    if route.understates_the_floor(effort):
        # Honoured, because a stated flag that is quietly overridden is worse
        # than one that is refused — but said out loud, because a pass run
        # below what its model needs comes back wrong or comes back rejected,
        # and either way it is paid for.
        log(f"    note: {backend.model} is asked for {route.floor} here by default")
    # A synchronous pass sends every owed verse once. A batch keeps its own
    # retry loop and limit.
    rounds = 1
    try:
        summary = (
            run_batch_pass(root, scope.ref, mode, backend, log=log)
            if route.batch
            else run_sync_rounds(
                run_sync_pass, root, scope, mode, backend,
                width=width, rounds=rounds, log=log,
            )
        )
    except MalformedResponse as broken:
        # A halt rather than a crash, so it lands in the queue with the rest.
        # The resume names the synchronous route deliberately: this is what a
        # provider that accepted a response schema without applying it looks
        # like, and the same model on the same contract has been observed to
        # honor it off the batch endpoint.
        raise Halt(
            reason=str(broken),
            resume=f"make drive CHAPTER={scope.ref} {mode.upper()}_ROUTE={route.name}",
            detail={"still_pending": pending_refs(root, scope.ref, mode)},
        ) from broken
    usage = summary.get("usage", {})
    spent = {
        "sessions": len(owed),
        "cost_usd": round(summary.get("cost_usd", 0.0), 4),
        **{
            report_key: usage.get(provider_key, 0)
            for provider_key, report_key in API_USAGE_KEYS.items()
        },
    }
    remaining = summary.get("still_pending")
    if remaining is None:
        remaining = pending_refs(root, scope.ref, mode)
    log(f"==> {mode} {scope.ref}: ${spent['cost_usd']} over {len(owed)} verse(s)")
    report = {
        "chapter": scope.ref, "mode": mode, "route": route.spec(),
        # What it actually ran at, which is not always what either the route or
        # the pass asked for: a model's floor can raise it.
        "model": backend.model, "effort": effort,
        "owed": len(owed), "ran": len(owed) - len(remaining),
        "outcomes": summary.get("outcomes", {}),
        "rejected": summary.get("rejected", {}),
        "failed": summary.get("failed", {}),
        "still_pending": remaining, "complete": not remaining,
        "spent": spent,
    }
    if summary.get("failed"):
        # Separate line from the one below, because the two want different
        # readers. A rejection is a verse to align; a failure is a request
        # that never answered, and says nothing about the alignment.
        log(f"    {len(summary['failed'])} verse(s) whose request never answered")
    if summary.get("rejected"):
        # A verse the validator rejected after every resend is a finding, not a
        # retry: the run says so here and the halt carries it into the queue.
        log(f"    {len(summary['rejected'])} verse(s) the validator still rejects")
    return report


def cli_session(
    route: Route | None,
    run_session: Callable[..., SessionResult],
    run_codex: Callable[..., SessionResult],
) -> Callable[..., SessionResult]:
    """Which CLI runs this pass, and on what model.

    The two runners take the same arguments and return the same result, so
    everything downstream — scheduling, the ledger, the checkpoint check — is
    written once and does not know which one it got. All that is bound here
    is the model, which only one of them takes.
    """
    if route is not None and route.is_codex:
        def routed_codex(prompt: str, *, effort: str | None = None, **kwargs: Any) -> SessionResult:
            # Chapter passes supply their own defaults here too, so a route's
            # effort applies equally to decisions, reconciliation, and verses.
            return run_codex(
                prompt, model=route.name,
                effort=route.resolved_effort(effort or "medium"), **kwargs,
            )

        return routed_codex
    return run_session


def api_session(
    route: Route, mode: str, *, effort: str, root: Path = ROOT,
) -> Callable[..., SessionResult]:
    """Answer one request from a provider endpoint, in the shape a pass expects.

    `run_packed_verses` and `run_tool_free_verse` are both written against a
    single callable — a prompt in, a `SessionResult` out — and everything they
    do around it is transport-independent: the wrapper is taken apart the same
    way, each verse is landed against its own packet the same way, an oversized
    pack is halved the same way, and a verse the pack got wrong is re-asked
    alone the same way. What made packing a CLI-only feature was the absence of
    this adapter and not anything about HTTP, so it is written here rather than
    duplicated inside `batching`, and the failure handling that a chapter of
    packed CLI runs paid for is inherited rather than rewritten.

    A backend is built per distinct schema and kept, because the schema is the
    one thing a packed request changes about it: the wrapper travels as the
    enforced response format.

    The label is the request's `custom_id`, so a pack is charged to the ledger
    under the same name the log names it by.
    """
    backends: dict[str, Any] = {}

    def backend_for(schema: str) -> Any:
        if schema not in backends:
            backends[schema] = build_backend(
                route.provider, route.model, root=root, effort=effort,
                mode=mode, schema=json.loads(schema) if schema else None,
            )
        return backends[schema]

    def send(
        prompt: str, *, label: str = "request", schema: str | None = None,
        system_append: str | None = None, **ignored: Any,
    ) -> SessionResult:
        """One request, reported as one session.

        The keyword arguments a CLI takes and an endpoint has no use for —
        `tools`, `isolated`, `env`, `effort` — are accepted and dropped. They
        describe how a local agent is confined, and a stateless request is
        confined by being one; refusing them here would mean teaching every
        caller which transport it was talking to, which is the coupling this
        adapter exists to remove.
        """
        backend = backend_for(schema or "")
        # The cache boundary the batch path already splits on: the briefing
        # every verse of the chapter shares goes in `stable`, the verses in
        # `variable`. A packed request shares more of it than an unpacked one,
        # which is the whole saving.
        job = Job(custom_id=label, stable=system_append or "", variable=prompt)
        try:
            result = backend.complete(job)
        except Exception as failure:  # noqa: BLE001 - reported, not raised
            # Reported as a failed session rather than raised, so that a
            # dropped connection reaches `send_until_answered` as the transient
            # it is instead of ending the pass.
            return SessionResult(label=label, returncode=1, output=str(failure))
        price = backend.price()
        cost = price.cost(result.usage, batch=False) if price else 0.0
        usage = result.usage.as_dict()
        if result.error or result.payload is None:
            return SessionResult(
                label=label, returncode=1,
                output=result.error or "response carried no object",
                usage=usage, cost_usd=cost, turns=1,
            )
        return SessionResult(
            label=label, returncode=0,
            output=json.dumps(result.payload, ensure_ascii=False),
            usage=usage, cost_usd=cost, turns=1, payload=result.payload,
        )

    return send


def run_pass(
    chapter: str, mode: str, *,
    run_session: Callable[..., SessionResult] = claude_session,
    run_codex: Callable[..., SessionResult] = codex_session,
    quota: Callable[[], int | None] | None = None,
    width: int = DEFAULT_WIDTH, safe: bool = False, auto_safe: bool = True,
    route: Route | None = None,
    briefings: Callable[..., tuple[str, dict[int, str]]] | None = None,
    root: Path = ROOT, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run every verse this mode still owes, one session each.

    The work list is read from disk before the pass and again after it, so an
    interrupted run resumes by being run again and a verse that quietly failed
    is reported rather than assumed done.

    A `route` that is not the Claude Code one sends the same verses to a
    provider endpoint instead. The dispatch is here rather than in the caller
    so that everything a pass owes — which verses, in what shape, reported how
    — stays answered in one place whichever endpoint answers it.

    A Claude Code route splits once more, on `Route.tool_free`. The default
    gives the session no tools and a response schema and lands what it returns
    here; `code:session` is the older shape, where the verse renders, writes,
    and validates for itself. Both are briefed from the same two halves and
    both leave the same durable checkpoint, so a chapter half aligned by one is
    finished by the other — the property that lets the two be compared on real
    verses at all.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}; expected one of {list(MODES)}")
    scope = chapter_scope(chapter)
    owed = pending_refs(root, scope.ref, mode)
    if not owed:
        return {"chapter": scope.ref, "mode": mode, "owed": 0, "note": "nothing pending"}
    if route is not None and not route.is_cli and not route.packed:
        # `safe` narrows a fan-out here as it does on the session path, though
        # for a different reason: there is no window to run out of, but a
        # sequential pass is still how a chapter gets watched one verse at a
        # time when something looks wrong.
        #
        # A packed provider route falls through instead. `run_api_pass` sends
        # one verse per request, which is what a packed pass is asking not to
        # do; below is the path that groups verses, holds the answer to a
        # wrapper and lands each verse on its own, and `api_session` is what
        # lets it send those requests to an endpoint.
        return run_api_pass(
            chapter, mode, route, width=1 if safe else width, root=root, log=log,
        )
    effort = route.resolved_effort(EFFORTS[mode]) if route else EFFORTS[mode]
    session = (
        cli_session(route, run_session, run_codex)
        if route is None or route.is_cli
        else api_session(route, mode, effort=effort, root=root)
    )
    taken = route or DEFAULT_ROUTES[mode]
    tool_free = taken.tool_free
    packed = taken.packed
    if briefings is None:
        # A tool-free verse is told there is nothing to open and no command to
        # run, and a session verse is told which file to write and how to
        # validate it. Both statements are true of the route that makes them
        # and a lie about the other, which is why the render is chosen here
        # with the tool set rather than left to a default.
        #
        # A packed request is the third case: the same evidence, with the "return
        # one object" instruction left out, because `packed_prompt` states it
        # once for the wrapper and two of them would contradict each other.
        if packed:
            briefings = packed_evidence_parts_many
        else:
            briefings = verse_task_parts_many if tool_free else verse_session_parts_many
    # The five-hour window is Claude Code's. A Codex pass does not draw on it
    # and cannot be told how much of its own allowance is left, so it is neither
    # throttled by it nor charged the `/usage` call that reads it — the same
    # reason the API path has no back-off. What still bounds the pass is the
    # rolling pool, and what still reports it is the ledger.
    watches_window = route is None or route.is_code
    # Said for every route rather than only for Codex. A packed provider pass
    # runs here and not in `run_api_pass`, which was the only place that named
    # its endpoint out loud, so `ALIGN=sol` printed what a default Claude Code
    # run printed and the only evidence of where the chapter had gone was the
    # per-token rate the ledger happened to cost it at. A route is a paid
    # decision and a run says which one it took, while it is taking it: the
    # summary names the routes too, but only once the chapter has finished, and
    # a run that is watched for an hour is watched before then.
    log(
        f"==> {mode} {scope.ref}: {len(owed)} verse(s) through "
        f"{taken.describe()} at {effort} effort"
    )
    if taken.understates_the_floor(effort):
        log(f"    note: {taken.name} is asked for {taken.floor} here by default")
    # What the session should record about the run rather than about the verse.
    # A review receipt has to say what reviewed it, and the driver is what knows
    # — asking the session to name itself would be trusting a model with a fact
    # about itself that its caller already holds.
    session_facts = {REVIEWER_ENV: taken.label} if mode == "review" else {}
    # Read once, before the pass spends anything, for the same reason the
    # briefings are: a schema that cannot be read is a halt, and discovering it
    # per verse would discover it once per verse.
    try:
        schema = response_schema(root, mode) if tool_free else None
        packed_schema = wrap_schema(schema) if packed and schema else None
    except (OSError, json.JSONDecodeError) as error:
        return {
            "chapter": scope.ref, "mode": mode, "owed": len(owed), "ran": 0,
            "still_pending": owed,
            "halted": f"{RESPONSE_SCHEMAS[mode]} could not be read: {error}",
        }
    # Built once for the pass, at the moment it starts, so every session opens
    # with its verse in front of it. A briefing that cannot be built is a halt
    # before anything is spent rather than a session that discovers it.
    try:
        verses = [verse_number(ref) for ref in owed]
        stable, bodies = briefings(root, scope.ref, verses, mode)
    except (ValueError, FileNotFoundError) as error:
        return {
            "chapter": scope.ref, "mode": mode, "owed": len(owed), "ran": 0,
            "still_pending": owed,
            "halted": f"{scope.ref} could not be briefed for its {mode} pass: {error}",
        }
    # What one request covers. Without packing that is one verse, which is the
    # production default and every path below reads the same either way: a pass
    # schedules, throttles and records units of work, and a unit is a list of
    # verses that happens usually to hold one.
    # `:packed=all` is every verse this pass still owes in as few requests as
    # one response can answer, which is a size only the work list knows — a
    # resumed pass owes fewer verses than a fresh one. Up to `PACK_ALL_CAP`
    # that is one request; past it the owed verses are divided into equal packs
    # rather than a full one and a remainder, so no request of the pass is
    # larger than any other.
    if not packed:
        units = [[ref] for ref in owed]
    elif taken.pack == PACK_ALL:
        units = even_packs(owed, PACK_ALL_CAP)
    else:
        units = packs(owed, taken.pack)
    if packed:
        log(
            f"==> {mode} {scope.ref}: {len(owed)} verse(s) in {len(units)} "
            f"packed request(s) of up to {max(len(unit) for unit in units)}"
        )
    ledger = CostLedger(root, scope.ref, mode)
    failures: list[str] = []
    ran = 0
    # What each verse looked like before anyone reviewed it, so the receipt can
    # say whether the review changed it without asking the session to
    # characterise its own work. Read once before any concurrent result can
    # change its file.
    before = {
        ref: file_sha256(accepted_path(root, scope, ref))
        for ref in owed
        if mode == "review" and accepted_path(root, scope, ref).exists()
    }

    def next_throttle() -> Throttle:
        if watches_window:
            percent_left = quota() if quota else None
            decision = throttle(percent_left, width, safe=safe, auto_safe=auto_safe)
            log(f"==> {mode} {scope.ref}: {decision.reason}")
            return decision
        return Throttle(
            "go", 1 if safe else width, "no five-hour window on this route",
        )

    def halted(reason: str) -> dict[str, Any]:
        remaining = pending_refs(root, scope.ref, mode)
        return {
            "chapter": scope.ref, "mode": mode,
            "route": taken.spec(), "model": taken.name, "effort": effort,
            "owed": len(owed), "ran": ran,
            "halted": reason, "still_pending": remaining,
            "spent": ledger.totals(), "ledger": str(ledger.path),
            "sessions": ledger.sessions_for(
                sorted(set(failures) | set(remaining), key=verse_of)
            ),
        }

    def run_unit(group: list[str]) -> SessionResult:
        if packed:
            return run_packed_verses(
                group, mode, bodies, session=session, scope=scope,
                stable=stable, effort=effort, schema=schema or "",
                packed_schema=packed_schema or "", reviewer=taken.label,
                root=root, log=log,
                attempts=TOOL_FREE_ATTEMPTS,
            )
        ref = group[0]
        body = bodies[verse_number(ref)]
        if tool_free:
            return run_tool_free_verse(
                ref, mode, body, session=session, scope=scope,
                stable=stable, effort=effort, schema=schema or "",
                reviewer=taken.label, root=root, log=log,
                attempts=TOOL_FREE_ATTEMPTS,
            )
        return session(
            verse_prompt(ref, mode, body),
            effort=effort, label=ref,
            env=session_facts, tools=PASS_TOOLS[mode],
            system_append=stable, isolated=True,
        )

    def record_result(
        group: list[str], result: SessionResult, wave: int,
    ) -> None:
        """Land and record one completed future before another is launched."""
        nonlocal ran
        ran += len(group)
        checkpoint_error = None
        # The review pass's checkpoint is written here rather than by the
        # session that earned it. A worker that already wrote its own receipt
        # is left alone rather than signed over a second time.
        if (
            mode == "review" and not tool_free and result.ok
            and not checkpoint_complete(root, scope, group[0], mode)
        ):
            checkpoint_error = land_review(
                root, scope, result, before.get(group[0], ""),
                reviewer=session_facts.get(REVIEWER_ENV, ""),
            )
        # A zero process exit means only that the CLI returned an envelope.
        # Completion is the valid verse or fresh receipt now on disk. Check the
        # verses that completed instead of rescanning the chapter — and check
        # every verse of a pack, because one request answering successfully
        # says nothing about which of its verses were landed.
        missing = [
            ref for ref in group
            if not checkpoint_complete(root, scope, ref, mode)
        ]
        if not checkpoint_error and result.ok and missing:
            checkpoint_error = (
                "session exited successfully but left no durable "
                f"{mode} checkpoint for {', '.join(missing)}; its final "
                f"response was: {result.output[-600:]}"
            )
        ledger.record(
            result, wave=wave, checkpoint_error=checkpoint_error,
            refs=group if len(group) > 1 else None,
        )
        trail = f" [{result.session_id}]" if result.session_id else ""
        if not result.ok or checkpoint_error:
            # A pack fails per verse, not as a block: whatever it did land is
            # done and owed by nobody, so only the verses without a checkpoint
            # are reported as failures.
            failures.extend(missing or group)
            if checkpoint_error:
                log(f"    {result.label}: no durable checkpoint for "
                    f"{', '.join(missing) or result.label}{trail}")
            else:
                log(f"    {result.label}: session failed ({result.returncode}){trail}")
        else:
            log(
                f"    {result.label}: ${result.cost_usd:.4f}, "
                f"{result.turns} turns{trail}"
            )

    # The first verse runs to completion by itself. It creates the stable
    # prefix cache before any sibling can race to pay the write again.
    decision = next_throttle()
    if decision.action == "stop":
        return halted(decision.reason)
    log(f"    starting 1: {pack_label(units[0])}")
    primer = run_unit(units[0])
    record_result(units[0], primer, wave=0)

    queue = list(units[1:])
    cache_ready = cache_prefix_ready(
        primer.usage.get("cache_read_input_tokens", 0),
        primer.usage.get("cache_creation_input_tokens", 0),
    )
    probe_wave = 1
    probes = 0
    while queue and not cache_ready and probes < CACHE_PROBE_LIMIT:
        decision = next_throttle()
        if decision.action == "stop":
            return halted(decision.reason)
        group = queue.pop(0)
        log(f"    cache probe: {pack_label(group)}")
        probe = run_unit(group)
        record_result(group, probe, wave=probe_wave)
        probes += 1
        cache_ready = cache_prefix_ready(
            probe.usage.get("cache_read_input_tokens", 0),
            probe.usage.get("cache_creation_input_tokens", 0),
        )
    if queue:
        decision = next_throttle()
        if decision.action == "stop":
            return halted(decision.reason)
        active_width = max(decision.width, 1)
        launched_since_check = 0
        next_index = 0
        wave = probe_wave + probes
        halt_reason: str | None = None
        # Keep the executor at the configured ceiling. `active_width` may fall
        # to one mid-pass; existing futures finish, and no replacement is
        # launched until the in-flight count is below the new limit.
        with ThreadPoolExecutor(max_workers=max(width, 1)) as pool:
            futures: dict[Any, tuple[int, list[str]]] = {}
            while next_index < len(queue) or futures:
                while (
                    halt_reason is None and next_index < len(queue)
                    and len(futures) < active_width
                ):
                    if launched_since_check >= active_width:
                        decision = next_throttle()
                        wave += 1
                        if decision.action == "stop":
                            halt_reason = decision.reason
                            break
                        active_width = max(decision.width, 1)
                        launched_since_check = 0
                        if len(futures) >= active_width:
                            break
                    group = queue[next_index]
                    next_index += 1
                    launched_since_check += 1
                    log(f"    starting 1: {pack_label(group)}")
                    futures[pool.submit(run_unit, group)] = (wave, group)
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    result_wave, group = futures.pop(future)
                    record_result(group, future.result(), wave=result_wave)
            # A stop prevents new work but never abandons already-paid futures;
            # the loop drains them and records each checkpoint before halting.
            if halt_reason is not None:
                return halted(halt_reason)
    remaining = pending_refs(root, scope.ref, mode)
    spent = ledger.totals()
    log(f"==> {mode} {scope.ref}: ${spent['cost_usd']} over {spent['sessions']} session(s)")
    return {
        "chapter": scope.ref, "mode": mode,
        # The same three keys `run_api_pass` reports, for the same reason: a
        # pass that does not record which model ran it leaves the route
        # readable only from the cost, and the two shapes of pass are read by
        # the same eyes. `model` is what actually ran and `effort` what it
        # actually ran at, neither of which the route alone always says.
        "route": taken.spec(), "model": taken.name, "effort": effort,
        "owed": len(owed), "ran": ran,
        **({"pack": "all" if taken.pack == PACK_ALL else taken.pack,
            "requests": len(units)} if packed else {}),
        "failed_sessions": failures, "still_pending": remaining,
        "complete": not remaining, "spent": spent, "ledger": str(ledger.path),
        # Every verse this pass did not finish, against the session that tried
        # it. A halt that names what is owed without naming what to open makes
        # the reader go and find the ledger by hand, and the ledger is exactly
        # the file nobody remembers the path of.
        "sessions": ledger.sessions_for(sorted(set(failures) | set(remaining), key=verse_of)),
    }


def finish_chapter(
    chapter: str, *, root: Path = ROOT,
    run_command: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Run the finishing checks. Mechanical, and kept out of a model session.

    The subprocess is marked so that nothing under `make finish` can start a
    second driver: one that did would re-enter this and fork without bound.
    """
    environment = {**os.environ, REENTRY_GUARD: chapter_scope(chapter).ref}
    runner = run_command or (
        lambda args: subprocess.run(
            args, cwd=root, capture_output=True, text=True, env=environment,
        )
    )
    scope = chapter_scope(chapter)
    finished = runner(["make", "finish", f"CHAPTER={scope.ref}"])
    return {
        "chapter": scope.ref,
        "ok": finished.returncode == 0,
        "output": (finished.stdout or "")[-4000:] + (finished.stderr or "")[-4000:],
    }


def report_chapter(
    chapter: str, *, root: Path = ROOT,
    run_command: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Compile the ledger and write the validation report, before reconciling.

    The reconciliation briefing includes the validation report written by
    `evaluate-alignment` off the compiled ledger. Both used to run only in
    `make finish`, which is *after* the session, so the session was pointed at a
    file that did not exist and spent four calls working out which target makes
    one. Two mechanical commands here cost nothing and make the instruction true
    when it is read.

    A failure is not fatal on its own: the render says plainly when the report
    is missing, and reconciliation can still proceed. Compilation
    failing at all would be caught again by `make finish`.
    """
    environment = {**os.environ, REENTRY_GUARD: chapter_scope(chapter).ref}
    runner = run_command or (
        lambda args: subprocess.run(
            args, cwd=root, capture_output=True, text=True, env=environment,
        )
    )
    scope = chapter_scope(chapter)
    for target in ("compile-alignment", "evaluate-alignment"):
        done = runner(["make", target, f"CHAPTER={scope.ref}"])
        if done.returncode != 0:
            return {
                "chapter": scope.ref, "ok": False, "failed": target,
                "output": (done.stdout or "")[-2000:] + (done.stderr or "")[-2000:],
            }
    return {"chapter": scope.ref, "ok": True}


def record_provenance(
    chapter: str, routes: dict[str, Route], *,
    root: Path = ROOT, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Write what ran this chapter into its run record.

    The one fact a finished chapter could not otherwise state. Every artifact
    says what it is and what governed it — the contract version, the hash of
    every input — and none of them said what produced it, so the model that
    aligned a chapter survived only in `data/batch/`, which is not committed.
    The reviewer was always the exception, recorded per verse in its receipt,
    and this brings the other two passes up to it.

    A run that stamps a route it did not entirely use is possible — switch
    models halfway through a chapter and this names the one the last run
    carried — and is not worth guarding against: the per-verse truth is in the
    receipts and the ledgers, and this is the chapter-level summary of a run
    that finished. Failing to write it is likewise not a halt. The chapter is
    correct, checked, and about to be committed; a missing note about what made
    it is not a reason to stop, only to say so.
    """
    def packing(route: Route) -> str:
        """What ran a pass, and how many verses shared each of its requests.

        Two details, both learned the hard way. `PACK_ALL` is zero, so a stamp
        that formatted the number raw said `(packed=0)` for a chapter aligned
        in one request. And a Codex label already carries a parenthetical, so
        appending a second one gave `gpt-5.6-sol (codex) (packed=all)`; the
        note joins the existing group instead.
        """
        if not route.packed:
            return route.label
        note = "packed={}".format(
            "all" if route.pack == PACK_ALL else route.pack
        )
        if route.label.endswith(")"):
            return f"{route.label[:-1]}, {note})"
        return f"{route.label} ({note})"

    reconcile_route = routes.get(RECONCILE, DEFAULT_ROUTES[RECONCILE])
    align_route = routes.get("packet", DEFAULT_ROUTES["packet"])
    review_route = routes.get("review", DEFAULT_ROUTES["review"])
    stamped = {
        # Packing is recorded for the same reason bounded reconciliation is:
        # the model is the same and the system is not. A chapter aligned eight
        # verses to a request was aligned with seven other verses in the
        # context, and a run record that said only `claude-code` would be
        # unable to tell anyone which chapters those were.
        "aligned_by": packing(align_route),
        # A bounded reconciliation and an agentic one are the same model and
        # not the same system — one reads the chapter across forty turns and
        # writes to it, the other answers one packet and writes nothing — so a
        # record saying only `claude-code` would name two things that differ in
        # the way this field exists to record. Same argument as `codex`.
        # Only when the review was packed, and `PROVENANCE_FIELDS` says why:
        # every verse already carries its reviewer in its own receipt, and the
        # one thing no receipt can say is that the review had seven other
        # verses in front of it. An empty string is dropped by the stamp, so an
        # ordinary review leaves this field absent rather than redundant.
        "reviewed_by": packing(review_route) if review_route.packed else "",
        "reconciled_by": reconcile_route.label + (
            " (bounded)" if reconcile_route.tool_free else ""
        ),
    }
    try:
        path = record_run_provenance(root, chapter, **stamped)
    except (FileNotFoundError, ValueError, OSError) as error:
        log(f"==> {chapter}: its run record could not be stamped ({error})")
        return {"ok": False, "error": str(error), **stamped}
    return {"ok": True, "path": str(path), **stamped}


def commit_chapter(
    chapter: str, *, root: Path = ROOT,
    run_command: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Commit the finished chapter under a formulaic message.

    The message is deliberately dull. A chapter that merely got done says
    nothing a reader needs; where this run learned something, the reconciliation
    session has already written it into the conventions log or the contract,
    which is where someone would look for it.

    It carries no co-author trailer. A fixed one lies on every chapter a Codex
    route drove, and a computed one would only restate `aligned_by` in the run
    record, which is already the place that answers who ran a chapter.

    It never asks git what changed first. This repository lives on a WSL mount
    of a Windows filesystem, where `git status` over the whole tree — sources,
    caches, every generated artifact — takes minutes, and it was being paid
    once per chapter to answer a question the commit itself answers for free:
    `git commit` on an empty index fails and says so. So this stages and
    commits, and reads the empty case off the failure.
    """
    runner = run_command or (
        lambda args: subprocess.run(args, cwd=root, capture_output=True, text=True)
    )
    scope = chapter_scope(chapter)
    runner(["git", "add", "-A"])
    message = (
        f"Finalize {scope.ref}\n\n"
        "Aligned and reviewed one session per verse, then reconciled across "
        "the chapter. Every finishing check passed.\n"
    )
    committed = runner(["git", "commit", "-m", message])
    output = (committed.stdout or "") + (committed.stderr or "")
    result: dict[str, Any] = {
        "chapter": scope.ref,
        "committed": committed.returncode == 0,
        "output": output[-2000:],
    }
    if not result["committed"] and "nothing to commit" in output:
        # Not a failure: a rerun of a chapter whose finishing steps rewrote
        # every file byte-for-byte has nothing left to record.
        result["note"] = "nothing to commit"
    return result



def format_duration(seconds: float) -> str:
    """A wall-clock span, in the largest units it actually has."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, remaining = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {remaining:02d}s"
    if minutes:
        return f"{minutes}m {remaining:02d}s"
    return f"{remaining}s"


def clis_in_use(routes: dict[str, Route]) -> dict[str, bool]:
    """Which CLI allowances this run actually draws on.

    A window nobody spent is the confusing thing to print, not a window that is
    missing: a fully-Codex run that reported Claude's five-hour figure was
    reporting a number it had not moved, next to a cost table it had.
    """
    return {
        "code": any(route.is_code for route in routes.values()),
        "codex": any(route.is_codex for route in routes.values()),
    }


def clis_in_stage(routes: dict[str, Route], stage: str) -> dict[str, bool]:
    """Which subscription windows a not-yet-started chapter may spend.

    Textual decisions are setup for every stage. Alignment and review spend
    only their named routes, while reconciliation belongs only to a full run.
    Provider routes deliberately contribute no window here: they are billed
    per token rather than drawn from either five-hour subscription allowance.
    """
    modes = {
        "align": (DECISIONS, "packet"),
        "review": (DECISIONS, "review"),
        "full": (DECISIONS, "packet", "review", RECONCILE),
    }
    if stage not in modes:
        raise ValueError(f"unknown stage {stage}")
    return clis_in_use({mode: routes[mode] for mode in modes[stage]})


def short_window_availability(
    routes: dict[str, Route], stage: str, *,
    run_session: Callable[..., SessionResult] = claude_session,
    read_codex: Callable[[], dict[str, int | None]] = read_codex_window,
) -> dict[str, int | None]:
    """Read the five-hour allowances relevant to the next chapter."""
    used = clis_in_stage(routes, stage)
    available: dict[str, int | None] = {}
    if used["code"]:
        available["code"] = read_window(run_session).get("session_left")
    if used["codex"]:
        available["codex"] = read_codex().get("session_left")
    return available


def windows_below_threshold(
    available: dict[str, int | None], threshold: int,
) -> dict[str, int]:
    """Known short windows below the start threshold; equality may start."""
    return {
        name: left for name, left in available.items()
        if left is not None and left < threshold
    }


def _close_window(
    report: dict[str, Any], opening: dict[str, int | None],
    run_session: Callable[..., SessionResult], check_quota: bool,
    started: float | None = None,
    codex_opening: dict[str, int | None] | None = None,
    used: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Stamp the report with what the run cost, in time and in window terms.

    Every exit from `drive_chapter` comes through here, which is why the clock
    is stopped here rather than at each of them.

    Elapsed time is this invocation's alone. A chapter that halted and was
    resumed reports the resumed run, not the sum: the earlier attempt's clock
    stopped when it stopped, and nothing on disk claims otherwise. Monotonic,
    so a clock change mid-run cannot produce a negative chapter.

    The window is read again at the end rather than inferred from the passes:
    the percentage is the provider's own accounting, and this run is not the
    only thing that may have spent against it.
    """
    if started is not None:
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    if not check_quota:
        return report
    spent = used or {"code": True, "codex": False}
    if spent["code"]:
        closing = read_window(run_session)
        report["window"] = {
            "session_left": closing.get("session_left"),
            "week_left": closing.get("week_left"),
            "session_left_before": opening.get("session_left"),
        }
    if spent["codex"]:
        closing = read_codex_window()
        report["codex_window"] = {
            "session_left": closing.get("session_left"),
            "week_left": closing.get("week_left"),
            "session_left_before": (codex_opening or {}).get("session_left"),
        }
    return report


def run_decisions_pass(
    chapter: str, *,
    run_session: Callable[..., SessionResult] = claude_session,
    render: Callable[[Path, Any], str] = render_decisions,
    root: Path = ROOT, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Resolve the chapter's textual decisions in one session.

    One session for the chapter rather than one per unit: the units are small,
    they share a briefing, and they are recorded into a single configuration
    file, which concurrent sessions would race on. That file is also the work
    list, so an interrupted pass resumes by being run again — the briefing is
    built here, at the moment the session starts, and only ever shows what is
    still owed.
    """
    scope = chapter_scope(chapter)
    owed = unresolved_variant_units(root, scope.ref)
    if not owed:
        return {"chapter": scope.ref, "mode": "decisions", "owed": 0,
                "note": "nothing pending"}
    log(f"==> decisions {scope.ref}: {len(owed)} unit(s) awaiting judgment")
    try:
        briefing = render(root, scope)
    except (ValueError, FileNotFoundError) as error:
        # Nothing has been spent yet, and a session sent a briefing that could
        # not be built would have discovered this the expensive way.
        return {
            "chapter": scope.ref, "mode": "decisions", "owed": len(owed),
            "still_pending": owed,
            "halted": f"{scope.ref} could not be briefed for its decisions: {error}",
        }
    ledger = CostLedger(root, scope.ref, "decisions")
    result = run_session(
        decisions_prompt(scope.ref, briefing), effort=DECISIONS_EFFORT,
        label=f"{scope.ref} decisions", tools=DECISIONS_TOOLS,
    )
    ledger.record(result, wave=0)
    remaining = unresolved_variant_units(root, scope.ref)
    report: dict[str, Any] = {
        "chapter": scope.ref, "mode": "decisions", "owed": len(owed),
        "resolved": len(owed) - len(remaining), "spent": ledger.totals(),
        "ledger": str(ledger.path),
    }
    if remaining:
        # The session is the only thing that can have failed here: the units
        # were owed before it ran and are owed after it. Name them, because a
        # decision left unrecorded is the one thing preparation cannot proceed
        # without.
        report["still_pending"] = remaining
        report["halted"] = (
            f"{scope.ref} still has {len(remaining)} textual decision(s) "
            "after its decisions pass"
        )
        if not result.ok:
            report["session"] = result.output[-500:]
    else:
        log(f"    decided {len(owed)} unit(s): ${result.cost_usd:.4f}")
    return report


def _resolve_decisions(
    run_session: Callable[..., SessionResult], root: Path,
    log: Callable[[str], None], report: dict[str, Any],
    route: Route | None = None,
    run_codex: Callable[..., SessionResult] = codex_session,
) -> Callable[[str], None]:
    """Give the setup step a way to resolve decisions rather than only refuse.

    `ensure_variant_decisions` still owns the rule — unresolved units do not
    proceed to preparation — and still halts when they are unresolved after
    this runs. What changes is that a run now has something to try first.
    """
    def resolve(chapter: str) -> None:
        outcome = run_decisions_pass(
            chapter, run_session=cli_session(route, run_session, run_codex),
            root=root, log=log,
        )
        if route is not None:
            outcome["route"] = route.spec()
        report["passes"].append(outcome)

    return resolve


def codex_billing_is_the_subscription(
    run_command: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str | None:
    """Why a codex route must not run, or nothing if it may.

    A `codex` route is a request for the ChatGPT subscription, and it is only
    ever that. The same models are reachable per token as `sol` and `terra`,
    which is what someone who wants to spend a key should type — so a key found
    anywhere near a codex run is not a thing to quietly work around. It is a
    run about to bill the wrong account under the wrong name, and it stops.

    Two ways in, and both are checked because either alone would let the other
    through: the key can be in this process's environment, where the CLI would
    pick it up, or the CLI can already be signed in with one.
    """
    for name in CODEX_BILLING_VARS:
        if os.environ.get(name):
            return (
                f"{name} is set in this environment, and a codex route must "
                "spend the ChatGPT subscription rather than an API key"
            )
    runner = run_command or (
        lambda args: subprocess.run(args, capture_output=True, text=True, timeout=60)
    )
    try:
        status = runner(["codex", "login", "status"])
    except FileNotFoundError:
        return "the `codex` executable is not on PATH"
    except subprocess.SubprocessError as error:
        return f"`codex login status` could not be read: {error}"
    said = ((status.stdout or "") + (status.stderr or "")).strip()
    if status.returncode != 0 or not said:
        return f"codex is not signed in: {said or 'no answer from `codex login status`'}"
    if "chatgpt" not in said.lower():
        # Anything that is not the subscription. Named rather than guessed at,
        # so the reader can see what it actually found.
        return (
            f"codex is signed in with something other than a ChatGPT account "
            f"(`codex login status` said: {said})"
        )
    return None


def ensure_routes_usable(
    routes: dict[str, Route], root: Path = ROOT,
    codex_billing: Callable[[], str | None] = codex_billing_is_the_subscription,
) -> None:
    """Refuse a route that cannot run, or would bill the wrong account.

    A credential is discovered at the moment the client is built, which is the
    first verse of the first routed pass — by which point the chapter has been
    fetched, its textual decisions have been made by a paid session, and its
    packets have been prepared. None of that is wasted, because it is all on
    disk and a re-run skips it, but the run still stopped for something that
    was knowable before it started. So it is checked here, where the answer
    costs nothing and the fix is one line.

    The codex check is here for a second reason as well. Its failure is not a
    stopped run but a completed one that spent the wrong money, which nothing
    downstream would ever report — so it has to be caught before the first
    session, and it is fatal rather than corrected in passing.
    """
    for mode, route in sorted(routes.items()):
        if route.is_codex:
            wrong = codex_billing()
            if wrong:
                raise Halt(
                    f"the {mode} pass is routed to codex and {wrong}",
                    "unset it and run `codex login` for the subscription, or "
                    "route this pass to sol or terra to spend the key on purpose",
                    {"route": route.spec()},
                )
            continue
        if route.is_code:
            continue
        env_name = missing_credential(route.provider, root)
        if env_name:
            raise Halt(
                f"the {mode} pass is routed to {route.provider} and "
                f"{env_name} is not set",
                f"add a {CREDENTIALS[route.provider][1]}= line to keys.txt, "
                f"or export {env_name}, then re-run",
                {"route": route.spec(), "provider": route.provider},
            )


def session_resume(route: Route | None, session_id: str) -> str:
    """How to reopen one finished session, in the CLI that ran it.

    A resume command for the wrong CLI is worse than none: it looks like an
    answer, and the id is real, so the reader tries it before concluding that
    the transcript is gone.

    A Claude session gets none at all, and that is not an omission. Every
    session this driver spawns is `isolated`, which carries
    `--no-session-persistence`, so no transcript is written and
    `claude --resume` finds nothing — `2TI.3` halted offering exactly that for
    a verse that had never left one. What the failed session said is recorded
    in the ledger and printed beside it, and that is the whole of what there is
    to read; an invitation to open a transcript that was never written only
    costs the reader the minute it takes to discover it.
    """
    if route is not None and route.is_codex:
        return f"codex resume {session_id}"
    return ""


def session_account(route: Route | None, entry: dict[str, Any]) -> str:
    """One session that did not finish, in whatever form an account exists.

    A way back into it where there is one, and otherwise what it said — which
    for the CLI routes is all there ever is.
    """
    said = (entry.get("error") or "").strip().splitlines()
    tail = f" — {said[0][:200]}" if said else ""
    if not entry.get("session"):
        return f"no session started{tail}"
    resume = session_resume(route, entry["session"])
    if resume:
        return f"`{resume}`{tail}"
    return f"session {entry['session']}, no transcript kept{tail}"


def resume_command(
    chapter: str, stage: str, routes: dict[str, Route], *, auto_commit: bool = True,
) -> str:
    """The command that carries on, including which models it was carrying on with.

    A resume that dropped the routes would restart the chapter on the defaults,
    so half its verses would be aligned by one model and half by another
    with nothing on disk to say so. The receipts would still name each verse's
    model, but nobody reads twenty receipts to discover a flag was forgotten.
    """
    parts = [f"make drive CHAPTER={chapter}", f"STAGE={stage}"]
    for flag, mode in (
        ("ALIGN", "packet"), ("REVIEW", "review"),
        ("RECONCILE", RECONCILE), ("DECISIONS", DECISIONS),
    ):
        route = routes.get(mode)
        if route is not None and route != DEFAULT_ROUTES[mode]:
            parts.append(f"{flag}={route.spec()}")
    if not auto_commit:
        parts.append("COMMIT=0")
    return " ".join(parts)


def drive_chapter(
    chapter: str, stage: str = "full", *,
    run_session: Callable[..., SessionResult] = claude_session,
    run_codex: Callable[..., SessionResult] = codex_session,
    width: int = DEFAULT_WIDTH, safe: bool = False, auto_safe: bool = True,
    auto_commit: bool = True,
    routes: dict[str, Route] | None = None,
    root: Path = ROOT, log: Callable[[str], None] = print,
    check_quota: bool = True,
) -> dict[str, Any]:
    """Take one chapter from a bare name to a finished, normally committed state.

    Everything a chapter needs is done here, including fetching and preparing
    it, so the only thing anyone has to know is the reference. What this will
    not do is decide a textual variant or where a Hebrew verse divides: those
    halt the run and are written to `needs-alignment/<CHAPTER>.md`, because a
    run nobody is watching cannot report a halt by printing it.

    `routes` says which model runs each of the four passes, defaulting to a
    Claude Code session for all of them. What a route may be differs by pass,
    and the difference is the pass's shape rather than a preference: a verse is
    one stateless request, so it can go to a provider endpoint; the two
    whole-chapter passes read a chapter off disk over several turns and write
    back to it, so they can only be a local agent session — `code` or `codex`.
    """
    scope = chapter_scope(chapter)
    routes = routes or dict(DEFAULT_ROUTES)
    report: dict[str, Any] = {
        "chapter": scope.ref, "stage": stage, "passes": [],
        "auto_commit": auto_commit,
        "routes": {mode: route.spec() for mode, route in routes.items()},
    }
    # The compiled ledger is the authoritative completed-chapter inventory,
    # unless the durable queue says the run halted after compilation. In
    # particular, reconciliation compiles before it starts, so a failed
    # reconciliation leaves both a ledger and a queue entry and must resume.
    # This guard still belongs before provider-window reads and setup, so an
    # explicit completed chapter or range can skip completed work for free.
    if (
        (root / f"data/alignments/{scope.slug}.jsonl").exists()
        and not queue_path(root, scope).exists()
    ):
        log(f"==> {scope.ref} already complete; skipping")
        report["already_complete"] = True
        report["chapter_cost_usd"] = 0.0
        return report

    started = time.monotonic()
    quota = (lambda: read_quota(run_session)) if check_quota else None
    # Read once at the start, and only the allowance this run will actually
    # spend: a fully-Codex run should neither report Claude's window nor pay two
    # sessions to look at it.
    used = clis_in_use(routes)
    opening_window = (
        read_window(run_session) if check_quota and used["code"] else {}
    )
    codex_opening = read_codex_window() if check_quota and used["codex"] else {}

    # Fetch, scaffold and resolve decisions, build, and prepare. Each is a
    # no-op when it has already been done, so this is also how an interrupted
    # chapter resumes.
    try:
        ensure_routes_usable(routes, root=root)
        ensure_sources(root, scope)
        ensure_variant_decisions(root, scope, resolve=_resolve_decisions(
            run_session, root, log, report,
            route=routes.get(DECISIONS, DEFAULT_ROUTES[DECISIONS]),
            run_codex=run_codex,
        ))
        ensure_prepared(root, scope)
    except Halt as halt:
        record_queue_entry(
            root, scope, "setup needs a judgment",
            [f"- {halt.reason}", "", f"Resume with: `{halt.resume}`"],
        )
        report.update(halt.as_dict())
        report["queued"] = str(queue_path(root, scope))
        return _close_window(
            report, opening_window, run_session, check_quota, started,
            codex_opening=codex_opening, used=used,
        )

    wanted = {"align": ["packet"], "review": ["review"], "full": list(MODES)}[stage]

    def run_and_halt(mode: str) -> tuple[dict[str, Any], str | None]:
        """One run of a pass, with whatever it did not finish named."""
        outcome = run_pass(
            scope.ref, mode, run_session=run_session, run_codex=run_codex,
            quota=quota, width=width,
            safe=safe, auto_safe=auto_safe, route=routes.get(mode),
            root=root, log=log,
        )
        return outcome, outcome.get("halted") or (
            f"{mode} left {len(outcome['still_pending'])} verse(s) owed"
            if outcome.get("still_pending") else None
        )

    for mode in wanted:
        outcome, halted = run_and_halt(mode)
        # The align pass fails transiently often enough that running it a second
        # time usually finishes the chapter, and the second run pays only for
        # what is still owed: `run_pass` reads the work list off disk, so a
        # verse that landed is not aligned twice. Once only — a pass that is
        # broken rather than unlucky halts exactly as it did before, having
        # been paid for twice and no more.
        if halted and mode == "packet":
            log(f"==> {STAGE_NAMES[mode]} {scope.ref}: {halted}; running it once more")
            outcome, halted = run_and_halt(mode)
        # The second run's outcome replaces the first rather than joining it:
        # the ledger it reports is the pass's whole ledger read off disk, both
        # runs included, so two rows here would bill the retry's verses twice.
        report["passes"].append(outcome)
        if halted:
            report["halted"] = halted
            resume = resume_command(
                scope.ref, stage, routes, auto_commit=auto_commit,
            )
            # The queue entry is the only account an unattended run leaves, so
            # it carries the way back into each session rather than only the
            # count. Asking the session that failed what it hit is the first
            # thing anyone does next, and reconstructing the command from the
            # ledger by hand is the step that makes them not bother.
            sessions = outcome.get("sessions") or {}
            record_queue_entry(
                root, scope, "a pass did not finish",
                [
                    f"- {halted}",
                    f"- still owed: {', '.join(outcome.get('still_pending', [])[:12]) or 'none'}",
                    *([
                        "",
                        "What the sessions that did not finish said:",
                        *(
                            f"  - {ref}: "
                            + session_account(routes.get(mode), entry)
                            for ref, entry in sessions.items()
                        ),
                    ] if sessions else []),
                    *([
                        "",
                        "Or read what the provider returned:",
                        f"  - `make batch-status CHAPTER={scope.ref}`",
                    ] if not sessions and not routes[mode].is_cli else []),
                    "",
                    f"Resume with: `{resume}` — verses already done are skipped.",
                ],
            )
            report["queued"] = str(queue_path(root, scope))
            return _close_window(
                report, opening_window, run_session, check_quota, started,
                codex_opening=codex_opening, used=used,
            )

    if stage != "full":
        return _close_window(
            report, opening_window, run_session, check_quota, started,
            codex_opening=codex_opening, used=used,
        )

    fingerprint = chapter_fingerprint(root, scope.ref)
    if reconciled_fingerprint(root, scope.ref) == fingerprint:
        log(f"==> {scope.ref} already reconciled at this alignment; skipping")
        report["reconciled"] = "unchanged"
        report["reconcile_cost_usd"] = 0.0
    else:
        # The passes are done, so a queue entry saying one did not finish is now
        # false, and a stale halt file is worse than none: PHP.3 reconciled with
        # its own entry on disk still claiming review owed it a verse. Any later
        # halt writes a fresh one.
        clear_queue_entry(root, scope)
        report["report"] = report_chapter(scope.ref, root=root)
        if not report["report"]["ok"]:
            log(f"==> {scope.ref}: {report['report']['failed']} failed; "
                "reconciling without the validation report")
        reconcile_route = routes.get(RECONCILE, DEFAULT_ROUTES[RECONCILE])
        bounded = reconcile_route.tool_free
        # Built here, after report_chapter, so the briefing carries the report
        # the session is asked to judge. A briefing that cannot be built is
        # worth halting on before anything is spent, exactly as the decisions
        # pass does: a session sent to render its own would have discovered
        # this the expensive way. The bounded route builds its packet inside
        # `run_adjudication` and reads the same report from disk, so only the
        # session route needs one here.
        try:
            reconcile_briefing = (
                "" if bounded else render_reconciliation(root, scope)
            )
        except (ValueError, FileNotFoundError, KeyError) as error:
            report["reconciled"] = False
            report["halted"] = (
                f"{scope.ref} could not be briefed for reconciliation: {error}"
            )
            record_queue_entry(
                root, scope, "reconciliation could not be briefed",
                [
                    "- the briefing could not be built, so no session was started",
                    "  and nothing was spent; the verses are aligned and reviewed",
                    "",
                    f"Resume with: `{resume_command(scope.ref, stage, routes, auto_commit=auto_commit)}`",
                ],
            )
            report["queued"] = str(queue_path(root, scope))
            return _close_window(
                report, opening_window, run_session, check_quota, started,
                codex_opening=codex_opening, used=used,
            )
        log(f"==> reconciling {scope.ref} with {reconcile_route.describe()}")
        session = cli_session(reconcile_route, run_session, run_codex)
        if bounded:
            validation = root / f"reports/{scope.slug}-validation.md"
            reconciled, adjudicated = run_adjudication(
                scope.ref, session=session, root=root, log=log,
                model=reconcile_route.label,
                repairs=TOOL_FREE_ATTEMPTS,
                report=(
                    validation.read_text(encoding="utf-8")
                    if validation.exists() else ""
                ),
            )
            if adjudicated:
                report["adjudication"] = adjudicated
                # On disk before anything else looks at it. An unattended run's
                # terminal output is gone by the time anyone reads it, and this
                # is the only account of what the reconciliation judged, what
                # it promoted, and what the contract says because of it — the
                # rule this project already applies to a halt, applied to the
                # one pass that can change a document every later chapter
                # reads.
                account = root / f"data/driver/{scope.slug}/reconcile-report.json"
                account.parent.mkdir(parents=True, exist_ok=True)
                account.write_text(
                    json.dumps(adjudicated, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                log(f"    reconcile: {reconciled.output[:400]}")
        else:
            reconciled = session(
                reconcile_prompt(scope.ref, reconcile_briefing),
                effort=RECONCILE_EFFORT, label="reconcile",
                stream=True, log=log,
            )
        # Fingerprinted with what the session actually read: a correction it
        # applied changes the chapter, and the next run reconciles that.
        CostLedger(root, scope.ref, "reconcile").record(
            reconciled, wave=0, fingerprint=chapter_fingerprint(root, scope.ref),
        )
        report["reconciled"] = reconciled.ok
        report["reconcile_cost_usd"] = round(reconciled.cost_usd, 4)
        report["reconcile_session_id"] = reconciled.session_id
        # Kept split by kind. Collapsing these into one input figure is exactly
        # what makes a summary lie about where the money went.
        report["reconcile"] = {
            key: reconciled.usage.get(key, 0)
            for key in ("input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens", "output_tokens")
        }
        report["reconcile"]["model_sessions"] = 1 + reconciled.child_sessions
        if not reconciled.ok:
            report["halted"] = "reconciliation session failed"
            record_queue_entry(
                root, scope, "reconciliation did not finish",
                [
                    "- the reconciliation session failed; the verses are aligned",
                    "  and reviewed, so a re-run resumes at reconciliation",
                    "",
                    f"Resume with: `{resume_command(scope.ref, stage, routes, auto_commit=auto_commit)}`",
                ],
            )
            report["queued"] = str(queue_path(root, scope))
            return _close_window(
                report, opening_window, run_session, check_quota, started,
                codex_opening=codex_opening, used=used,
            )
    report["chapter_cost_usd"] = round(
        sum(p.get("spent", {}).get("cost_usd", 0.0) for p in report["passes"])
        + report["reconcile_cost_usd"],
        4,
    )

    report["finish"] = finish_chapter(scope.ref, root=root)
    if not report["finish"]["ok"]:
        # A failing check is a real finding, and diagnosing it is judgment. Hand
        # it back with its own output rather than retrying blindly.
        #
        # Queued like any other halt. The entry on disk is the only account an
        # unattended run leaves, and one written by an earlier stage is worse
        # than none: PHP.1 failed its textual report while its queue file still
        # said it owed sixteen textual decisions it had in fact already made.
        #
        # The output goes to disk before anything else. It only exists in this
        # process, `--json` is not what an unattended run passes, and rerunning
        # `make finish` by hand does not reproduce a check that failed on state
        # the failing run has since repaired — so a halt that only printed it
        # was a halt nobody could diagnose.
        log_path = root / f"data/driver/{scope.slug}/finish.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(report["finish"].get("output", ""), encoding="utf-8")
        report["finish"]["log"] = str(log_path)
        report["halted"] = f"finishing checks failed; see {log_path}"
        record_queue_entry(
            root, scope, "the finishing checks failed",
            [
                "- the chapter is aligned, reviewed, and reconciled; a check on",
                "  the finished result failed. What it printed is in",
                f"  `{log_path}`, and rerunning it is",
                f"  `make finish CHAPTER={scope.ref}`",
                "",
                f"Resume with: `{resume_command(scope.ref, stage, routes, auto_commit=auto_commit)}`"
                " — no verse is redone.",
            ],
        )
        report["queued"] = str(queue_path(root, scope))
        return _close_window(
            report, opening_window, run_session, check_quota, started,
            codex_opening=codex_opening, used=used,
        )
    # Stamped after the checks and before the commit, so that what lands in the
    # commit is the finished record. It has to be after `finish_chapter`
    # regardless: that target recompiles the chapter, and a stamp written before
    # it would be rewritten by it — which is also why the compile carries these
    # two fields forward rather than recomputing them.
    report["provenance"] = record_provenance(scope.ref, routes, root=root, log=log)
    if auto_commit:
        report["commit"] = commit_chapter(scope.ref, root=root)
    else:
        report["commit"] = {
            "chapter": scope.ref,
            "committed": False,
            "skipped": True,
            "note": "disabled by --no-commit",
        }
    # The chapter owes nothing, so it should not still be sitting in the queue.
    clear_queue_entry(root, scope)
    return _close_window(
        report, opening_window, run_session, check_quota, started,
        codex_opening=codex_opening, used=used,
    )


STAGE_NAMES = {
    "packet": "align", "review": "review",
    "reconcile": "reconcile", "decisions": "decisions",
}

# How many resume lines the summary prints before deferring to the queue entry.
# Enough to cover a full concurrency pool going down together, which is the
# common shape of a pass that stopped, and short of a screenful.
SHOWN_SESSIONS = 8


def format_report(report: dict[str, Any]) -> str:
    """The run, as a few lines someone would actually read.

    The JSON is still the record and `--json` still prints it; this is the part
    a person looks at when the run ends, so it says whether it worked, what it
    cost, and how to go back and ask the reconciliation what it did.
    """
    chapter = report.get("chapter", "?")
    halted = report.get("halted")
    lines = [
        "",
        f"  {chapter}  {'✗ halted' if halted else '✓ finished'}",
        "",
    ]
    if report.get("already_complete"):
        lines.append("  already complete; skipped")

    # Input is split rather than summed. Cache reads are a tenth of the input
    # rate and cache writes are 1.25x it, so a single "tokens in" column can
    # move by half a million while the bill does not notice. What costs money is
    # the uncached half: fresh input, and the writes that create a cache entry.
    def split(usage: dict[str, Any]) -> tuple[int, int]:
        uncached = (
            usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        )
        return uncached, usage.get("cache_read_input_tokens", 0)

    # Requests and verses were the same number until a request could carry
    # several verses. The table counts requests, because that is what a session
    # column means; the per-verse figure below divides by verses, or packing
    # would report a chapter as costing nineteen times what it did.
    rows: list[tuple[str, int, int, int, int, float, int]] = []
    for outcome in report.get("passes", []):
        spent = outcome.get("spent")
        if not spent:
            continue
        uncached, cached = split(spent)
        rows.append((
            STAGE_NAMES.get(outcome.get("mode", ""), outcome.get("mode", "?")),
            spent.get("sessions", 0), uncached, cached,
            spent.get("output_tokens", 0), spent.get("cost_usd", 0.0),
            spent.get("verses", spent.get("sessions", 0)),
        ))
    # Presence, not truthiness: a reconciliation that reported no usage still
    # cost what it cost, and dropping its row would understate the total.
    reconcile = report.get("reconcile")
    if reconcile is not None:
        uncached, cached = split(reconcile)
        rows.append((
            "reconcile", reconcile.get("model_sessions", 1), uncached, cached,
            reconcile.get("output_tokens", 0), report.get("reconcile_cost_usd", 0.0),
            0,
        ))

    if rows:
        lines.append(
            f"  {'stage':<12}{'sessions':>9}{'in (new)':>11}{'in (cached)':>13}"
            f"{'out':>10}{'cost':>10}"
        )
        for name, sessions, uncached, cached, out, cost, _ in rows:
            lines.append(
                f"  {name:<12}{sessions:>9}{uncached:>11,}{cached:>13,}{out:>10,}"
                f"{'$' + format(cost, '.2f'):>10}"
            )
        lines.append(f"  {'':<12}{'':>9}{'':>11}{'':>13}{'':>10}{'-' * 8:>10}")
        total_uncached = sum(r[2] for r in rows)
        total_cached = sum(r[3] for r in rows)
        lines.append(
            f"  {'total':<12}{sum(r[1] for r in rows):>9}{total_uncached:>11,}"
            f"{total_cached:>13,}{sum(r[4] for r in rows):>10,}"
            f"{'$' + format(sum(r[5] for r in rows), '.2f'):>10}"
        )
        # Per verse, with the reconciliation included, because that is the
        # figure the fan-out was measured against: $0.345 of subagent plus
        # $0.493 of coordinator, per verse, per pass.
        verses = max((r[6] for r in rows if r[0] != "reconcile"), default=0)
        if verses:
            total_cost = sum(r[5] for r in rows)
            lines.append("")
            lines.append(
                f"  {verses} verses · ${total_cost / verses:.2f} per verse, "
                "every stage included"
            )
        served = total_cached + total_uncached
        if served:
            lines.append("")
            lines.append(
                f"  cache served {total_cached / served:.0%} of input"
                f" ({total_uncached:,} tokens paid at full rate or above)"
            )
        lines.append("")

    # Which model ran which pass, whenever it was not the default. A cost table
    # that does not say what produced it is unreadable a week later, and this is
    # the line that makes two chapters' figures comparable.
    routed = [
        f"{STAGE_NAMES.get(mode, mode)} on {parse_route(spec).describe()}"
        for mode, spec in (report.get("routes") or {}).items()
        if spec != DEFAULT_ROUTES[mode].spec()
    ]
    if routed:
        lines.append(f"  {' · '.join(routed)}")
        lines.append("")

    elapsed = report.get("elapsed_seconds")
    if elapsed is not None:
        # This invocation only. A chapter driven in three sittings reports the
        # third, which is the number that answers "how long until it is done"
        # for the way these runs are actually restarted.
        lines.append(f"  elapsed: {format_duration(elapsed)}")
        lines.append("")

    # One block per allowance this run actually spent, each named. A run has
    # two of these now, they are not interchangeable, and an unlabelled
    # percentage beside a chapter's cost reads as *the* number — which is how a
    # fully-Codex run came to display a Claude window it had never touched.
    windows = 0
    for field, whose in (("window", "Claude Code"), ("codex_window", "Codex")):
        window = report.get(field) or {}
        left = window.get("session_left")
        weekly = window.get("week_left")
        if left is None and weekly is None:
            continue
        windows += 1
        if left is not None:
            before = window.get("session_left_before")
            if before is None:
                spent_note = ""
            elif left > before:
                # The five-hour window rolls on a timer, so a run that straddles
                # a reset genuinely has more left than it started with.
                spent_note = " — the window reset partway through this run"
            else:
                spent_note = f" — this run used {before - left}%"
            lines.append(f"  5-hour window ({whose}): {left}% left{spent_note}")
        if weekly is not None:
            lines.append(f"  weekly limit ({whose}): {weekly}% left")
    if windows:
        lines.append("")

    # A promoted rule is the one thing a run does that outlives its own
    # chapter, so it is said plainly rather than left to the JSON. The version
    # is the part worth reading twice: every chapter aligned after this run
    # records it, and no chapter before it is touched.
    promoted = (report.get("adjudication") or {}).get("promoted") or {}
    for rule in promoted.get("rules", []):
        lines.append(
            f"  promoted rule {rule['number']} into {promoted['version']}: "
            f"{rule.get('summary') or rule['text'][:120]}"
        )
    # An amendment rewrites a rule whole, so it can drop a qualification as
    # easily as add one. The line says which rule moved and why; the text it
    # replaced is in the reconcile report next to it, which is the only copy
    # outside git.
    for rule in promoted.get("amended", []):
        lines.append(
            f"  amended rule {rule['number']} in {promoted['version']}: "
            f"{rule.get('why') or rule['text'][:120]}"
        )
    if promoted.get("refused"):
        lines.append(f"  promotion refused: {promoted['refused']}")
    if report.get("reconciled") == "unchanged":
        lines.append("  reconciliation skipped; the chapter has not changed since the last one")
    if report.get("finish"):
        lines.append(
            "  finishing checks passed" if report["finish"].get("ok")
            else "  finishing checks FAILED — what they printed is in "
            f"{report['finish'].get('log', 'the JSON report')}"
        )
    commit = report.get("commit") or {}
    if commit:
        lines.append(
            "  committed" if commit.get("committed")
            else f"  not committed ({commit.get('note', 'see the JSON report')})"
        )
    routes = {
        mode: parse_route(spec)
        for mode, spec in (report.get("routes") or {}).items()
    }
    if halted:
        lines.append(f"  halted: {halted}")
        if report.get("queued"):
            lines.append(f"  written to: {report['queued']}")
        lines.append(
            f"  resume with: {resume_command(chapter, report.get('stage', 'full'), routes, auto_commit=report.get('auto_commit', True))}"
            "  (finished verses are skipped)"
        )

    # A halt says how many verses did not finish; this says how to go and ask
    # them why. The session ids were always recorded — printing the count
    # without them left the reader to open the ledger and reconstruct the
    # command, which is the point at which the question stops being asked.
    unfinished = [
        (outcome.get("mode", "?"), ref, entry)
        for outcome in report.get("passes", [])
        for ref, entry in (outcome.get("sessions") or {}).items()
    ]
    if unfinished:
        lines.append("")
        lines.append(
            f"  the {len(unfinished)} session(s) that did not finish, "
            "and what they said:"
        )
        for mode, ref, entry in unfinished[:SHOWN_SESSIONS]:
            label = f"{ref} ({STAGE_NAMES.get(mode, mode)})"
            resume = (
                session_resume(routes.get(mode), entry["session"])
                if entry.get("session") else ""
            )
            if resume:
                lines.append(f"    {label}:  {resume}")
            elif entry.get("session"):
                # No resume line: these sessions are not persisted, so the id
                # is a receipt and not a door.
                lines.append(f"    {label}:  session {entry['session']}")
            else:
                lines.append(f"    {label}:  no session was started")
            error = (entry.get("error") or "").strip().splitlines()
            if error:
                # The first line of what it said, which for a session with no
                # transcript is the only account of it there will be.
                lines.append(f"      {error[0][:96]}")
        if len(unfinished) > SHOWN_SESSIONS:
            # The rest are in the queue entry and the ledger; a screen of
            # identical lines is not more useful than a pointer to them.
            lines.append(
                f"    … and {len(unfinished) - SHOWN_SESSIONS} more, in "
                f"{report.get('queued') or 'the queue entry'}"
            )

    reconciled = report.get("reconcile_session_id")
    resume = session_resume(routes.get(RECONCILE), reconciled) if reconciled else ""
    if resume:
        lines.append("")
        lines.append(f"  ask the reconciliation what it did:  {resume}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Drive a chapter end to end through bounded verse passes.",
        epilog=(
            "A route is provider[/model][:batch|:session|:packed=N][@effort], "
            "or a bare model name, or a local CLI — `code` for Claude Code, "
            "`codex` for a Codex CLI session on the ChatGPT "
            "subscription, which runs GPT-6 Astra at medium for every pass "
            "(`codex-astra` says the same thing out loud, `codex-sol` picks "
            "GPT-5.6 Sol at the pass's own effort). "
            "Alignment and review pack all owed verses by default, in requests "
            "of at most 75 verses; every returned verse is validated and landed "
            "separately. `:packed=N` sets another size, `:unpacked` sends one "
            "verse per request, and `code:session` selects the older agentic "
            "shape where a verse writes its own file. "
            "Examples: code, code@high, code:session, code:packed=8, codex, "
            "codex/terra, codex-astra, codex-sol, sol, sol:batch, terra, "
            "terra:batch, gemini, "
            "openai/gpt-5.6-sol:batch@medium. Reconciliation takes only a "
            "CLI route and operates on the whole chapter."
        ),
    )
    parser.add_argument("stage", choices=("align", "review", "full"))
    parser.add_argument("chapter", help="a chapter such as GEN.6")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="rolling verse concurrency after the cache primer")
    parser.add_argument("--safe", action="store_true", help="one verse at a time")
    parser.add_argument("--no-auto-safe", action="store_true",
                        help="do not drop to sequential when the window runs thin")
    parser.add_argument("--no-quota-check", action="store_true")
    parser.add_argument("--no-commit", action="store_true",
                        help="leave the finished chapter unstaged and uncommitted")
    parser.add_argument("--align", metavar="ROUTE",
                        help="which model aligns (default: code)")
    parser.add_argument("--review", metavar="ROUTE",
                        help="which model reviews (default: code)")
    parser.add_argument("--reconcile", metavar="ROUTE",
                        help="how the chapter is reconciled: code for the "
                             "default bounded request, code:session for the "
                             "agentic fallback, or codex")
    parser.add_argument("--decisions", metavar="ROUTE",
                        help="which CLI resolves the textual decisions (default: code)")
    parser.add_argument("--json", action="store_true",
                        help="print the raw report instead of the summary")
    args = parser.parse_args(argv)

    # Nothing `make finish` runs may start another driver, however that target
    # grows: a check that drove a chapter would fork without bound. Cheap to
    # enforce here and painful to rediscover, so it stays even now that finish
    # runs no step that could.
    #
    # It belongs here, at the only entry point that starts a run, and not in
    # drive_chapter. Guarding the function meant reading the ambient
    # environment, and the suite calls it directly with stubbed sessions that
    # spawn nothing: back when finish ran the suite, six honest tests failed
    # under it and only under it, which halted every unattended drive at its
    # finishing checks and passed the moment anyone ran `make finish` by hand
    # to find out why.
    if os.environ.get(REENTRY_GUARD):
        halt = Halt(
            "the chapter driver is already running and reached its finishing "
            f"checks for {os.environ[REENTRY_GUARD]}; it will not start another",
            "run it outside `make finish`",
        )
        print(json.dumps(halt.as_dict(), indent=2), file=sys.stderr)
        return 1

    # Parsed before anything is fetched or spent: a route that cannot be read
    # is a typo, and a typo found after a chapter's packets are prepared is a
    # typo found later than it needed to be.
    try:
        routes = resolve_routes(
            args.align, args.review, args.reconcile, args.decisions,
        )
    except ValueError as error:
        parser.error(str(error))

    report = drive_chapter(
        args.chapter, args.stage, width=args.width, safe=args.safe,
        auto_safe=not args.no_auto_safe, check_quota=not args.no_quota_check,
        auto_commit=not args.no_commit,
        routes=routes,
    )
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 1 if report.get("halted") else 0

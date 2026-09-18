"""Which model runs a pass, written as one string.

A chapter has two verse passes and they do not want the same model. The
alignment is the contract applied to a fresh packet and runs cheaply; the
review has to notice a rule misapplied in a file that already validates, and
AGENTS.md records the measurement that says it must be at least as strong as
the pass it checks. So the choice is per pass, and the whole choice — endpoint,
model, batch or not, and how hard to think — is one token a caller can type
without opening a file:

    code                              a Claude Code session per verse
    code@high                         the same, at a stated effort
    code:packed=8                     eight verses per tool-free request
    codex                             a Codex CLI session per verse, on GPT-6
                                      Astra at medium for every pass
    codex/terra                       the same, on a named model
    codex-astra                       what a bare `codex` already is, said out
                                      loud
    codex-sol                         the same subscription CLI on GPT-5.6 Sol,
                                      at the pass's own effort
    openai                            the GPT default, synchronously
    gemini                            the Gemini default, synchronously
    sol:batch                         GPT-5.6 Sol, at half price
    terra                             GPT-5.6 Terra, synchronously
    openai/gpt-5.6-sol:batch@medium   every part said out loud

That shape exists because of the scale this runs at. A route that has to be
assembled from three environment variables is a route that gets assembled
wrongly on chapter four hundred, and the wrong assembly is not visible until
the invoice. One string is one thing to get right, and it round-trips into the
run report, so a finished chapter says what actually aligned it.

Nothing here talks to a provider or knows what a pass does. It parses, and it
refuses what it cannot parse — before the run spends anything.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .providers import (
    BACKENDS,
    DEFAULT_MODELS,
    MODEL_ALIASES,
    canonical_model,
)

# `code` is not a provider in `providers.py` and deliberately is not one: a
# Claude Code session has a filesystem and more than one turn, so it is not the
# stateless request the backends there model. It is a peer here because at the
# level of "what ran this pass" it is exactly that, and because Claude Code
# tokens are subsidized far below the API rates — it stays the default.
CODE = "code"

# The same argument, one vendor over: `codex exec` is a local agent session with
# a filesystem and several turns, billed to a ChatGPT subscription rather than
# per token. So it is the other CLI peer, and the two are spelled apart from the
# API providers for the reason `claude` and `code` are — the work is the same
# and the bill is not.
#
# Unlike `code` it takes a model, because the Codex CLI does: `codex/terra` runs
# the same slug the `openai` backend would, through the subscription instead of
# the API. Left unnamed it uses the model below rather than whatever
# `~/.codex/config.toml` happens to say, so that a run report names what ran.
#
# That default is GPT-6 Astra rather than the `openai` backend's own default,
# because the two answer different questions. `DEFAULT_MODELS["openai"]` is
# what an API request runs; this is which model a bare `codex` should be, and
# the reason to type `codex` at all is that Claude refused the pass. A fallback
# wants the strongest thing on the other subscription, and reaching the weaker
# one would have to be remembered rather than typed.
CODEX = "codex"
CODEX_PROVIDER = "openai"
CODEX_DEFAULT_MODEL = "gpt-6-astra"

# The subscription routes that pin one model, so the two worth naming are one
# word rather than a `/model` suffix whose spelling has to be looked up. They
# are the same transport as `codex` and differ from it only in leaving nothing
# unstated: `codex-astra` is what a bare `codex` already is, kept because it is
# what every chapter and test before this said, and `codex-sol` is the older
# default now that it is no longer the default. A model suffix on either is
# refused rather than honored — `codex-sol/terra` is a route that would run
# something other than what it says.
CODEX_ASTRA = "codex-astra"
CODEX_ASTRA_MODEL = "gpt-6-astra"
CODEX_SOL = "codex-sol"
CODEX_PINNED = {
    CODEX_ASTRA: CODEX_ASTRA_MODEL,
    CODEX_SOL: DEFAULT_MODELS[CODEX_PROVIDER],
}

CLIS = (CODE, CODEX, *CODEX_PINNED)
PROVIDERS = (*CLIS, *sorted(BACKENDS))

# Typing the endpoint is the fussy half of a route, and a model name already
# implies its endpoint. So a bare model is a complete route: `gemini-3.7-flash`
# means the same as `gemini/gemini-3.7-flash`. Inverted from the defaults each
# backend already declares rather than restated, so this cannot drift from them.
# A model not named there still routes, by naming its provider.
MODEL_PROVIDERS = {
    **{model: provider for provider, model in DEFAULT_MODELS.items()},
    **{alias: provider for alias, (provider, _) in MODEL_ALIASES.items()},
    **{model: provider for provider, model in MODEL_ALIASES.values()},
}

ALIASES = {
    "claude-code": CODE,
    "cc": CODE,
    # `claude` is the API, not the CLI: the CLI is `code`. Spelling them apart
    # matters because they bill differently by an order of magnitude.
    "claude": "anthropic",
    "gpt": "openai",
    "google": "gemini",
    # And the same distinction on the OpenAI side: `openai` is the API, `codex`
    # is the CLI in front of it.
    "codex-cli": CODEX,
    "cx": CODEX,
}

# The three every backend takes. Gemini maps them onto `thinking_level` and
# rejects `minimal`, so the shared set is the intersection rather than any one
# provider's full range.
EFFORTS = ("low", "medium", "high")

# What a `:` may say about a route. `:batch` halves the price of a provider
# request and answers in hours; `:session` gives a Claude Code verse pass the
# filesystem and the several turns it used to have, and exists so the tool-free
# default has something to be compared against; `:packed=N` puts N verses in
# one tool-free request instead of one each.
MODIFIERS = ("batch", "session", "packed", "unpacked")

# Packing is spelled in the route rather than as a separate flag because a
# route round-trips into the run report, and how many verses shared a request
# is exactly the kind of fact a finished chapter has to be able to state about
# itself. `:packed` without a number takes the default below.
#
# Eight is a starting point rather than a measurement: a verse's alignment
# object runs to a few thousand output tokens, so eight of them sit well inside
# one response while still spending the shared contract prefix once instead of
# eight times. The driver bisects a pack whose response does not fit, so a size
# that turns out to be too large costs a refused request rather than a halt.
PACK_DEFAULT = 8

# `:packed=all` — every verse this pass still owes, in one request. Spelled as
# a word rather than a number because the number is a fact about the chapter
# that the caller would otherwise have to look up per chapter, and looking it
# up wrongly is silent. It is also the one size the ceiling below does not
# apply to: asking for the whole chapter is a decision, not a typo.
PACK_ALL = 0

# How many verses `:packed=all` actually puts in one request. `all` is a
# decision about the *pass* — every verse it still owes, in as few requests as
# will answer — and this bounds what one lost request costs to re-ask rather
# than what a response can hold. Nothing has been measured to break at any pack
# size: MAT.26 aligned 75 verses in one request and reviewed the same 75 in
# another, in 55,679 output tokens against the 128,000 the driver exports.
# Seventy-five is therefore the largest chapter seen to answer whole, and a
# chapter past it is split into equal packs by `even_packs` — never into one
# full pack and a remainder, which would keep sending the size the cap exists
# to bound.
PACK_ALL_CAP = 75

# A pack of one is the ordinary route spelled the long way, and is refused so
# that nobody reads a run report as packed when it was not. The ceiling is a
# typo guard: the longest chapter in the Bible is 176 verses, and a pack that
# large would be bisected most of the way back down anyway.
PACK_LIMIT = 32
EFFORT_RANK = {effort: rank for rank, effort in enumerate(EFFORTS)}

# Which endpoints answer in hours at half price and which only answer now.
# Read off the backends rather than restated, so a provider that gains or loses
# a batch API is answered in one place. A `:batch` aimed at an endpoint without
# one used to be accepted and then discovered at submission, after the chapter
# had been fetched and prepared.
BATCHLESS = tuple(
    provider for provider, backend in sorted(BACKENDS.items())
    if not backend.supports_batch
)

# How hard a model has to think to do this work at all, where that is more than
# the pass would otherwise ask for. The per-pass defaults — medium to
# align, high to review — are about the *task*: applying the contract to a
# fresh packet is easier than catching a rule misapplied in a file that already
# validates. This table is about the *model*, and the two are separate facts, so
# they are separate tables.
#
# Gemini 3.7 Flash and GPT-5.6 Terra align at high. Opus 5 and GPT-5.6 Sol
# take the pass's own default and are absent here, so Sol runs at medium for
# alignment and high for review.
#
# A floor, never a ceiling: it can only raise what a pass asks for. Reviewing is
# high for everything already, so this changes alignment only.
MODEL_EFFORTS = {
    "gemini-3.7-flash": "high",
    "gpt-5.6-terra": "high",
}

# Where a model runs every pass at one effort whatever the pass would ask for.
# Not the table above and not a floor: this one may lower as well as raise, so
# GPT-6 Astra reviews at medium too. It is keyed on the model rather than on
# the route that reaches it, because it is a fact about the model — the same
# weights are the same weights whether they arrive as `codex`, `codex-astra`,
# or a `/gpt-6-astra` suffix, and keying it on the route name meant a bare
# `codex` inherited or escaped it by accident. A stated `@effort` still wins.
MODEL_PASS_EFFORTS = {
    CODEX_ASTRA_MODEL: "medium",
}

# The task's defaults, shared by `make drive` and the lower-level provider
# commands. A model floor may raise one, and an explicit @effort/--effort may
# still override it.
PASS_EFFORTS = {"packet": "medium", "review": "high"}


@dataclass(frozen=True)
class Route:
    """Where one pass of one chapter goes."""

    provider: str
    model: str | None = None
    batch: bool = False
    # None means "this pass's own effort" — the driver holds that table,
    # because it is a fact about alignment and review rather than about
    # routing. A route only records that the caller did not override it.
    effort: str | None = None
    # Whether a Claude Code verse pass runs as an agentic session — one that
    # renders its own briefing, writes its own file, and runs the validator
    # itself — rather than as the tool-free request the same verse takes on
    # every other route. It is off by default because the session route pays
    # for a filesystem it does not need: the whole tool set is schema the model
    # re-reads on every turn, and the three turns a verse takes to write and
    # validate one JSON object are three re-reads of its briefing. The parent
    # can do the writing for nothing. Kept spellable because it is the fallback
    # if the tool-free route ever aligns worse, and comparing them is what
    # settles that.
    session: bool = False
    # How many verses share one tool-free request: one, a stated number, or
    # `PACK_ALL` for every verse the pass owes. `None` means the caller did not
    # say, which is not the same as saying one — a verse pass left unstated
    # packs, and `resolve_routes` is where that is decided, because it is the
    # only place that knows which pass a route is for.
    pack: int | None = None

    @property
    def is_code(self) -> bool:
        return self.provider == CODE

    @property
    def is_codex(self) -> bool:
        """Whether a `codex exec` subscription session runs this pass."""
        return self.provider in (CODEX, *CODEX_PINNED)

    @property
    def tool_free(self) -> bool:
        """Whether this pass is one request that returns only its JSON object.

        True of every route but a Claude Code session and a Codex one. What it
        decides for the driver is who writes the accepted file: on a tool-free
        route the parent validates the response and lands it, so a model that
        misremembers a path or reports a write it never made cannot leave a
        chapter looking finished. The model owns the judgment and nothing else.

        Codex used to be excluded outright, on the grounds that nothing there
        held a reply to a response schema. `codex exec --output-schema` does,
        so a Codex verse pass can be tool-free, and a packed one is — packing
        has no other shape. `codex:unpacked` is the agentic per-verse route
        that used to be spelled `codex`, kept because it is what every Codex
        chapter before this was aligned by.
        """
        # Keyed on `packed` and not on `pack`, because an unstated route has
        # neither packed nor unpacked yet: reading `pack is None` as "not one,
        # therefore packed, therefore tool-free" made every unresolved
        # `code:session` and `codex` route tool-free, which is the opposite of
        # what both of them are.
        if self.packed:
            return True
        return not (self.is_codex or (self.is_code and self.session))

    @property
    def packs_by_default(self) -> bool:
        """Whether a verse pass on this route packs when nobody says.

        Both CLIs and every synchronous API provider hold a reply to the
        wrapper, which is all packing needs. Two route shapes are left out and
        neither is a preference: `code:session` writes its own files, so there
        is no wrapper for the parent to land, and a batch already groups the
        whole pass at the provider.

        The cost of including Codex is that its session keeps a shell it never
        uses, since Codex has no tool allowlist. That is tokens, not
        correctness, and it is the same bill an unpacked Codex verse already
        paid once per verse rather than once per chapter.

        """
        return not self.session and not self.batch

    @property
    def packed(self) -> bool:
        """Whether this pass sends several verses per request.

        Never true of a route that is not tool-free: a packed request is the
        parent validating and landing each returned object, and a session that
        writes its own files has no wrapper to return.
        """
        # An unstated route is not packed until something resolves it. Every
        # route the driver acts on has been through `resolve_routes`, which
        # leaves no `None` behind.
        return self.pack is not None and self.pack != 1

    @property
    def is_cli(self) -> bool:
        """Whether this pass runs as a local agent session rather than a request.

        The distinction the driver actually acts on: a CLI route spawns a
        process that reads the repository over several turns, so it needs no
        API key, it cannot be batched, and the passes that are not per-verse
        units can take it. Everything else is a single stateless request.
        """
        return self.provider in CLIS

    @property
    def name(self) -> str:
        """The model this route will actually run, for a report to name."""
        if self.is_code:
            return "claude-code"
        if self.provider in CODEX_PINNED:
            return CODEX_PINNED[self.provider]
        if self.is_codex:
            return self.model or CODEX_DEFAULT_MODEL
        return self.model or DEFAULT_MODELS[self.provider]

    @property
    def label(self) -> str:
        """What ran this pass, for a receipt to name.

        The model alone is not the whole answer on the Codex route: the same
        slug reached through the CLI and through the API is a different system
        prompt, a different tool set, and a different bill, so a receipt that
        said only `gpt-5.6-sol` would name two things that are not the same.
        """
        return f"{self.name} ({self.provider})" if self.is_codex else self.name

    @property
    def floor(self) -> str | None:
        """The least this model can do this work at, if it needs more than the pass."""
        return MODEL_EFFORTS.get(self.name)

    def resolved_effort(self, pass_default: str) -> str:
        """What this route will actually be run at.

        A stated effort wins — someone who typed `@medium` meant it, and a flag
        that is quietly overridden is worse than one that is refused. Otherwise
        the pass's default, raised to the model's floor where it has one.
        """
        if self.effort:
            return self.effort
        fixed = MODEL_PASS_EFFORTS.get(self.name)
        if fixed:
            return fixed
        floor = self.floor
        if floor and EFFORT_RANK[floor] > EFFORT_RANK[pass_default]:
            return floor
        return pass_default

    def understates_the_floor(self, effort: str) -> bool:
        """Whether a stated effort is below what this model needs to do the work."""
        floor = self.floor
        return floor is not None and EFFORT_RANK[effort] < EFFORT_RANK[floor]

    def spec(self) -> str:
        """The string this route parses back from."""
        text = self.provider
        if self.model:
            text += f"/{self.model}"
        if self.batch:
            text += ":batch"
        if self.session:
            text += ":session"
        if self.pack == PACK_ALL:
            text += ":packed=all"
        elif self.pack is not None and self.pack > 1:
            text += f":packed={self.pack}"
        elif self.pack == 1 and self.packs_by_default:
            # Only worth saying where it overrides a route that would pack.
            # The grammar takes one modifier, so session and batch routes
            # cannot also carry `:unpacked` and have no need to do so.
            text += ":unpacked"
        if self.effort:
            text += f"@{self.effort}"
        return text

    def describe(self) -> str:
        parts = [self.name]
        if self.is_codex:
            parts.append(self.provider)
        if self.batch:
            parts.append("batch")
        if self.session:
            parts.append("session")
        if self.pack == PACK_ALL:
            parts.append(
                f"every verse, in requests of at most {PACK_ALL_CAP}"
            )
        elif self.pack == 1:
            parts.append("one verse per request")
        elif self.pack is not None:
            parts.append(f"{self.pack} verses per request")
        if self.effort:
            parts.append(self.effort)
        return " ".join(parts) if len(parts) == 1 else f"{parts[0]} ({', '.join(parts[1:])})"


def parse_route(spec: str) -> Route:
    """Read one route, or say what is wrong with it and stop.

    Every refusal here is one a run would otherwise discover after it had
    already fetched a chapter, prepared its packets, and started paying.
    """
    text = (spec or "").strip()
    if not text:
        raise ValueError("a route cannot be empty")

    effort: str | None = None
    if "@" in text:
        text, _, effort = text.partition("@")
        effort = effort.strip().lower()
        if effort not in EFFORTS:
            raise ValueError(
                f"unknown effort {effort!r} in route {spec!r}; "
                f"expected one of {', '.join(EFFORTS)}"
            )

    batch = False
    session = False
    pack: int | None = None
    if ":" in text:
        text, _, suffix = text.partition(":")
        suffix = suffix.strip().lower()
        name, _, size = suffix.partition("=")
        if name not in MODIFIERS:
            raise ValueError(
                f"unknown route modifier {suffix!r} in {spec!r}; "
                f"expected one of {', '.join(':' + name for name in MODIFIERS)}"
            )
        if size and name != "packed":
            raise ValueError(
                f"only :packed takes a size ({spec!r}); :{name} does not"
            )
        if name == "unpacked":
            pack = 1
        batch = name == "batch"
        session = name == "session"
        if name == "packed":
            if size == "all":
                pack = PACK_ALL
            elif not size:
                pack = PACK_DEFAULT
            elif not size.isdigit():
                raise ValueError(
                    "a pack size must be a whole number of verses or `all` "
                    f"({spec!r})"
                )
            else:
                pack = int(size)
            if pack != PACK_ALL and pack < 2:
                raise ValueError(
                    f"a pack holds at least two verses ({spec!r}); "
                    "one verse per request is the plain route, spelled `code`"
                )
            if pack > PACK_LIMIT:
                raise ValueError(
                    f"a pack of {pack} is past the {PACK_LIMIT}-verse ceiling "
                    f"({spec!r}); a response that large is bisected back down "
                    "at the cost of the request that discovered it"
                )

    provider, _, model = text.strip().partition("/")
    provider = ALIASES.get(provider.strip().lower(), provider.strip().lower())
    model = model.strip() or None

    if provider not in PROVIDERS:
        # A bare model name is a complete route, so a provider this does not
        # recognize gets one more reading before it is an error.
        inferred = MODEL_PROVIDERS.get(text.strip())
        if inferred is None:
            raise ValueError(
                f"unknown route {spec!r}; expected one of {', '.join(PROVIDERS)}, "
                f"or a model name such as {', '.join(sorted(MODEL_PROVIDERS))}"
            )
        provider, model = inferred, text.strip()

    if model and provider not in (CODEX, *CODEX_PINNED):
        model = canonical_model(provider, model)

    if batch and provider in BATCHLESS:
        # Not a limitation to work around and not a thing to downgrade
        # silently: `:batch` is a claim about price and latency that this
        # endpoint cannot honor, and a run report that said batch when the
        # verses went synchronously would be wrong about both.
        raise ValueError(
            f"{provider} has no batch endpoint ({spec!r}); "
            "drop :batch to run the pass synchronously, or route it to "
            + ", ".join(
                name for name in sorted(BACKENDS) if name not in BATCHLESS
            )
        )

    if provider == CODE:
        if batch:
            # Not a limitation to work around: a Claude Code session is
            # interactive by construction and there is no batch endpoint behind
            # it. Saying so here is cheaper than a run that silently ignored it.
            raise ValueError(
                "there is no batch endpoint for Claude Code sessions; "
                "use anthropic:batch to send the same passes to the API"
            )
        if model:
            raise ValueError(
                f"the code route does not take a model ({spec!r}); "
                "it runs `claude -p --model opus`"
            )

    if provider in (CODEX, *CODEX_PINNED):
        if batch:
            # Same reason as `code`, and worth saying separately because the
            # model behind it does have a batch endpoint — reached by naming
            # the API rather than the CLI in front of it.
            raise ValueError(
                "there is no batch endpoint for Codex CLI sessions; "
                "use openai:batch to send the same passes to the API"
            )
        if model:
            # Validated against the OpenAI names, because the CLI runs OpenAI
            # models — but reported as a codex route, since that is what was
            # asked for and naming the other one would send the reader to the
            # wrong flag.
            try:
                model = canonical_model(CODEX_PROVIDER, model)
            except ValueError as wrong:
                raise ValueError(f"{wrong} — the codex route runs OpenAI models") from wrong
            pinned = CODEX_PINNED.get(provider)
            if pinned is not None and model != pinned:
                raise ValueError(
                    f"the {provider} route runs {pinned}, not {model}; "
                    f"use codex/{model} to select another model"
                )

    if session and provider != CODE:
        # Refused rather than ignored, for the reason every other refusal here
        # is: the flag means "run this verse as an agentic session instead of
        # one request", and only Claude Code has both shapes to choose between.
        raise ValueError(
            f"{provider} has no session route ({spec!r}); "
            "only code runs a verse pass either way"
        )

    return Route(
        provider=provider, model=model, batch=batch, effort=effort,
        session=session, pack=pack,
    )


# What a run does when told nothing. Claude Code for every pass; verse work is
# tool-free and reconciliation uses the bounded shape.
RECONCILE = "reconcile"
DECISIONS = "decisions"

# The two passes that are a chapter rather than a verse. Their local CLI routes
# include machinery no provider route exposes: decisions record a whole closed
# candidate set, while bounded reconciliation validates and lands patches in
# the driver. The verse passes have no such restriction, which is the whole
# reason the batch endpoints exist here.
CHAPTER_PASSES = (DECISIONS, RECONCILE)

# The two verse passes pack by default. Measured on Hebrews: a chapter aligned
# and reviewed one request per pass spends a sixth of the attended tokens one
# request per verse does, and the five-hour window meters attended tokens.
# `:unpacked` is how a pass asks for the older shape back, and it is still the
# shape that checks a packed one, so a chapter run to compare the two says so
# in its routes.
DEFAULT_ROUTES = {
    "packet": Route(CODE, pack=PACK_ALL), "review": Route(CODE, pack=PACK_ALL),
    # The decisions pass has one shape and takes the modifier only for the sake
    # of saying so: its briefing arrives inline and it records the chapter with
    # one `decide-many`, which is a tool call and cannot be a returned object.
    DECISIONS: Route(CODE, session=True),
    # Reconciliation has two. The bounded request is the default: one validated
    # packet cannot grow a coordinator transcript or spawn an unmetered child
    # tree. HEB.13's agentic route did both and cost $26.64; `code:session`
    # remains the explicit fallback while correction recall is benchmarked.
    RECONCILE: Route(CODE),
}

WHY_A_CHAPTER_PASS_IS_A_SESSION = {
    DECISIONS: "it judges every unresolved unit in the chapter and records them "
               "in one file",
    RECONCILE: "it adjudicates a whole chapter through CLI-specific machinery",
}


def resolve_routes(
    align: str | None = None, review: str | None = None,
    reconcile: str | None = None, decisions: str | None = None,
) -> dict[str, Route]:
    """The route for each mode, keyed as the passes are keyed elsewhere."""
    stated = {
        "packet": align, "review": review,
        RECONCILE: reconcile, DECISIONS: decisions,
    }
    routes = {
        mode: parse_route(spec) if spec else DEFAULT_ROUTES[mode]
        for mode, spec in stated.items()
    }
    # A route that said nothing about packing is resolved here, because this is
    # the only place that knows which pass it is for. A verse pass packs; the
    # two whole-chapter passes are one request over the chapter already and
    # have nothing to pack. The default is not applied to a route that cannot
    # take it: `code:session` writes its own files and a batch already groups
    # work at the provider.
    for mode, route in routes.items():
        if route.pack is not None or mode in CHAPTER_PASSES:
            # A whole-chapter pass is one request over the chapter already, so
            # it is left unstated rather than told it is unpacked — there is
            # nothing there to pack, and a route that said `:unpacked` about it
            # would be answering a question nobody asked.
            continue
        routes[mode] = replace(
            route, pack=PACK_ALL if route.packs_by_default else 1,
        )
    for mode in CHAPTER_PASSES:
        if routes[mode].packed or routes[mode].pack == PACK_ALL:
            # Packing is several verse tasks in one request, and neither of
            # these is a verse task: each is already one request about the
            # whole chapter.
            raise ValueError(
                f"{mode} cannot be packed ({routes[mode].spec()}): "
                "it is one whole-chapter pass, not a verse at a time"
            )
        if not routes[mode].is_cli:
            # Refused rather than silently downgraded, and before the run has
            # fetched anything: a flag that is quietly ignored is worse than one
            # that is turned down.
            raise ValueError(
                f"{mode} cannot run on {routes[mode].spec()}: "
                f"{WHY_A_CHAPTER_PASS_IS_A_SESSION[mode]}, which is not one "
                "stateless request. Use code or codex."
            )
    return routes

# Chapter driver

`make drive` is the production entry point that built almost every accepted
chapter in this repository. It joins deterministic source preparation and
validation to bounded model judgments while keeping accepted files as the
durable checkpoint.

## The normal command

```bash
make drive CHAPTER=PSA.15
```

`CHAPTER` is always explicit. It may also be a range or a space-separated set:

```bash
make drive CHAPTER=PSA.15-17
make drive CHAPTER="PSA.15-17 PRO.3"
```

A successful full run commits the finished chapter automatically. Repeating
the same command is the normal way to resume; work already accepted and
reviewed is not repeated.

## What a full run does

In order, the driver:

1. Validates the selected routes and required credentials.
2. Fetches the chapter's pinned source repositories if necessary.
3. Resolves textual decisions before alignment.
4. Builds and prepares one canonical input packet per verse.
5. Aligns every owed verse.
6. Independently reviews every accepted verse.
7. Compiles and validates the chapter.
8. Reconciles chapter-wide inconsistencies and any generalizable rule changes.
9. Runs the deterministic finishing targets.
10. Records source, contract, schema, model, and review provenance.
11. Stages and commits the completed worktree.

Scripts may fetch, normalize, render, validate, compile, and export. They do
not invent alignment judgments. Model responses are validated against the
prepared input before the driver writes accepted files.

## Stages

`STAGE` selects which verse passes run:

```bash
make drive CHAPTER=PSA.15 STAGE=align
make drive CHAPTER=PSA.15 STAGE=review
make drive CHAPTER=PSA.15 STAGE=full
```

Every stage performs setup first: source checks, textual decisions, building,
and preparation. `align` runs only the alignment pass. `review` runs only the
review pass over accepted alignments. `full`, the default, runs both passes,
then reconciliation, finishing checks, provenance, and the commit.

## Verse requests and packing

Alignment and review pack by default. Each pass uses as few requests as the
configured cap allows, while each returned verse object is still validated and
landed independently. A malformed or absent verse does not become acceptable
because its neighbors validated.

Useful spellings are:

```bash
make drive CHAPTER=PSA.15 ALIGN=code:packed=8
make drive CHAPTER=PSA.15 ALIGN=code:packed=all
make drive CHAPTER=PSA.15 ALIGN=code:unpacked
make drive CHAPTER=PSA.15 ALIGN=code:session
```

- `:packed=all` divides the owed verses into the fewest capped packs.
- `:packed=N` fixes a pack size.
- `:unpacked` uses one request per verse.
- `code:session` selects the older agentic verse shape in which the worker
  writes and validates its own file.

Packing shares transport, not judgment. The response wrapper holds separate
canonical verse objects, and the same validator governs every object.

## Routes

A route has the form:

```text
provider[/model][:modifier][@effort]
```

Common routes:

```text
code                         Claude Code
codex                        Codex CLI on the ChatGPT subscription
codex-astra                  explicit pinned Codex Astra route
codex-sol                    explicit pinned Codex Sol route
sol                          OpenAI API, GPT-5.6 Sol
terra                        OpenAI API, GPT-5.6 Terra
sol:batch                    OpenAI batch API
anthropic/claude-opus-5:batch
gemini                       synchronous Gemini
```

Select alignment and review independently:

```bash
make drive CHAPTER=PSA.15 ALIGN=gemini REVIEW=code
make drive CHAPTER=PSA.15 ALIGN=sol:batch REVIEW=terra:batch
```

The textual-decisions and reconciliation passes are chapter-wide and accept a
local CLI route (`code` or `codex`), not a stateless API route. `STEPS` sets all
four routes at once, while a specifically named pass wins:

```bash
make drive CHAPTER=PSA.15 STEPS=codex
make drive CHAPTER=PSA.15 STEPS=codex REVIEW=code
```

Unstated effort follows the task and model defaults. Alignment normally uses
medium effort and review high effort; model-specific floors may raise it.

## Durable checkpoints and resuming

The filesystem, not the model transcript, is the run's memory. Durable state
includes:

- textual decisions under `config/`
- prepared verse inputs under `data/output/`
- accepted verse files and review receipts under `data/alignments/`
- provider and cost ledgers under `data/driver/`
- halt instructions under `needs-alignment/`

After an interruption:

```bash
make status CHAPTER=PSA.15
make drive CHAPTER=PSA.15
```

The driver reconstructs what is owed. A valid accepted verse is not aligned
again; a current hash-bound review receipt is not repeated; stale derived
artifacts are rebuilt mechanically.

If a run stops for a judgment or failed check, its queue entry names the reason
and the resume command. A full run that reaches reconciliation or finishing
checks does not reopen unrelated finished chapters.

## Running several chapters

Ranges are sequential within one invocation. The remaining-work selectors are:

```bash
make drive CHAPTER=head LIMIT=10
make drive CHAPTER=tail LIMIT=10
make drive CHAPTER=tail UNTIL=9
make drive CHAPTER=headsize LIMIT=10
```

`head` and `tail` select canonical endpoints of the unfinished inventory;
`headsize` and `tailsize` use its size ordering. `LIMIT` bounds completed
chapters. `UNTIL` starts another chapter only while every relevant subscription
window has at least the stated percentage remaining. Neither interrupts a
chapter already in progress.

Parallel chapter runs require separate worktrees because fetching, contracts,
the source lock, and repository-level documentation are shared.

## Provider credentials and billing

API credentials may be exported or placed in the ignored `keys.txt` file.
Keys are handed only to the client that needs them and are removed from local
CLI session environments so a subscription route cannot silently bill an API
account.

The `codex` route is specifically the ChatGPT subscription and refuses to run
when it detects API-key billing. Use `sol` or `terra` to spend an OpenAI API key
intentionally. Gemini is synchronous-only. Anthropic and OpenAI support the
batch route.

Every provider request can cost money. Route parsing, credential checks, and
briefing construction happen before the request where possible. Usage and
resolved model names are written to the run record.

## Rebuilding versus creating judgments

A different model run may return a different valid alignment. Reproducing the
accepted publication therefore means rebuilding from the committed accepted
judgments, not asking a model to rediscover them.

These are deterministic:

```bash
make validate-alignment CHAPTER=PSA.15
make compile-alignment CHAPTER=PSA.15
make aligned-usx CHAPTER=PSA.15
make textual-report CHAPTER=PSA.15
```

On a fresh clone, first recreate the deliberately omitted source and prepared
inputs with `make fetch`, `make build`, and `make prepare-alignment` for the
same chapter, in that order. The commands above then rebuild from the committed
accepted judgments; they do not ask a model to rediscover them.

`make drive` is for producing new judgments or resuming an unfinished chapter.

## Automatic commit

After a successful full run, the driver runs `git add -A` and creates a
formulaic `Finalize <CHAPTER>` commit. This behavior is intentional for the
production repository. Start a drive only in a worktree whose current changes
belong in that chapter's finishing commit.

To leave the finished chapter unstaged and uncommitted, opt out for that run:

```bash
make drive CHAPTER=PSA.15 COMMIT=0
```

Automatic committing remains the default. If the run halts, its printed resume
command retains `COMMIT=0`.

---
name: align-chapter
description: Prepare, align, independently review, reconcile, and finish one ASV original-language chapter.
---

# Align a chapter

Use this skill for a complete chapter workflow. Read `AGENTS.md`, `DRIVER.md`,
and the applicable testament contract before coordinating a chapter. A worker
that receives a rendered single-verse briefing needs only that briefing.

## Preferred workflow

Run the resumable driver:

```bash
make drive CHAPTER=EPH.1
```

It performs deterministic setup, alignment, independent review,
reconciliation, finishing checks, provenance, and the default commit. Use
`COMMIT=0` to leave the successful run unstaged and uncommitted.

After any interruption:

```bash
make status CHAPTER=EPH.1
make drive CHAPTER=EPH.1
```

Never recreate a verse that already validates or repeat a review whose receipt
still matches the accepted verse hash.

## Deterministic setup

The driver normally performs these steps itself. They may be inspected or run
individually:

```bash
make fetch CHAPTER=EPH.1
make build CHAPTER=EPH.1
make init-decisions CHAPTER=EPH.1
make prepare-alignment CHAPTER=EPH.1
```

Resolve textual decisions before alignment. Prepared packets under
`data/output/<CHAPTER>/verses/` are mechanical derivatives and contain the
selected source projection, ASV tokens, provenance, and exact identifiers.

## Alignment pass

For manual single-verse work, render exactly one complete briefing:

```bash
make render CHAPTER=EPH.1 VERSES=8-8
```

Reason from that briefing, return the required canonical JSON object, and
validate it against its prepared packet. Do not choose a different textual
reading during alignment. A manually written accepted file is validated with:

```bash
PYTHONPATH=src python3 scripts/generate_alignments.py validate-verse EPH.1.8
```

Write and validate in the same atomic interaction. One model context performs
one pass; it does not continue into review or another verse.

## Independent review

Review every verse in a fresh context on a model at least as strong as the
aligner:

```bash
make render CHAPTER=EPH.1 VERSES=8-8 MODE=review
```

An unchanged review returns an unchanged verdict. A correction returns the
complete replacement alignment and must validate before it is written. The
driver records a hash-bound review receipt; a later alignment edit makes that
receipt stale.

Large source units and other diagnostics are review candidates, not automatic
errors. Structural validation proves coverage and schema shape, not linguistic
quality.

## Reconciliation and finish

Reconciliation examines chapter-wide consistency after the independent verse
passes. Record chapter-local recurring judgments in `conventions.md`. Promote
only genuinely general rules into the testament contract, bump its version,
and never renumber existing rules.

Finish deterministically:

```bash
make finish CHAPTER=EPH.1
```

That compiles accepted verses, validates the ledger, generates chapter USX and
reverse-interlinear output, writes textual and evaluation reports, and fails at
the first unmet invariant. Run `make test` after changing published code.

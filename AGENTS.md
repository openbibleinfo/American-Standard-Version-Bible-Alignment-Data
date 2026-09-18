# Working instructions for alignment agents

This repository is a reference implementation for creating reproducible
original-language alignments for the American Standard Version. Read this file,
the root `README.md`, and the applicable alignment contract before changing an
alignment:

- `config/alignment-instructions-greek.md`
- `config/alignment-instructions-hebrew.md`

The contract governs linguistic judgment. This file governs the workflow.
`.claude/skills/align-chapter/SKILL.md` is the concise chapter procedure.

## Keep judgment separate from machinery

- Scripts may fetch, normalize, isolate, validate, compile, report, and render.
  They must not invent alignment judgments.
- Make textual decisions before verse alignment. A verse pass receives the
  selected source projection and does not choose another reading.
- Treat accepted files under `data/alignments/` as versioned source data.
  Ordinary builds must not overwrite them.
- Prepared inputs under `data/output/` are reproducible derivatives, not
  accepted judgments.

## One verse, one judgment

Every verse packet is complete and independent. A verse pass receives the
testament contract, chapter conventions when present, exact response shape,
and one prepared verse. It returns the canonical JSON object; the driver
validates that object against its packet before writing it.

Review is a separate pass in a fresh model context. Every accepted verse is
reviewed, regardless of confidence. A correction replaces the complete verse
object and must validate before it can overwrite accepted work. Review receipts
are bound to the accepted file hash, so a later edit makes the receipt stale.

Use durable files rather than model transcripts as memory. After an
interruption, run:

```bash
make status CHAPTER=PSA.15
make drive CHAPTER=PSA.15
```

The driver skips accepted, validated, and currently reviewed work.

## Normal chapter workflow

The supported end-to-end entry point is:

```bash
make drive CHAPTER=PSA.15
```

It fetches pinned sources, prepares inputs, obtains alignment and independent
review judgments, reconciles the chapter, runs deterministic finishing checks,
records provenance, and commits a successful full run. Use `COMMIT=0` when a
particular run should remain unstaged and uncommitted.

The deterministic stages may also be run explicitly:

```bash
make fetch CHAPTER=PSA.15
make build CHAPTER=PSA.15
make prepare-alignment CHAPTER=PSA.15
make compile-alignment CHAPTER=PSA.15
make validate-alignment CHAPTER=PSA.15
make aligned-usx CHAPTER=PSA.15
make textual-report CHAPTER=PSA.15
make evaluate-alignment CHAPTER=PSA.15
```

`make finish CHAPTER=PSA.15` runs the required deterministic finishing
sequence. Successful structural validation proves coverage and shape; it does
not replace linguistic review.

## Manual verse work

Render one complete briefing:

```bash
make render CHAPTER=PSA.15 VERSES=1-1
make render CHAPTER=PSA.15 VERSES=1-1 MODE=review
```

Do not make a verse worker rediscover repository structure or open unrelated
files. Validate a manually produced result with:

```bash
PYTHONPATH=src python3 scripts/generate_alignments.py validate-verse PSA.15.1
```

Writing and validation should be one atomic interaction. Record a recurring
chapter judgment immediately with `generate_alignments.py record-convention`.
Promote a genuinely general rule into the applicable testament contract; never
renumber existing rules.

## Source and repository discipline

- Source versions and sparse paths are pinned in `config/sources.lock.json`.
- Accepted chapters preserve their historical contract stamps and source
  entries. Do not rewrite them merely because current rules evolved.
- Parallel chapter work requires separate worktrees because source checkouts,
  contracts, and repository-level files are shared.
- Use `make clean-chapter CHAPTER=<ref> CONFIRM=<ref>` only for a confirmed
  systemic redo. It deliberately preserves pinned and normalized sources.
- Run `make test` after changing published code.

The finished ASV USX publication is maintained separately; this repository is
the development process and the accepted chapter records.

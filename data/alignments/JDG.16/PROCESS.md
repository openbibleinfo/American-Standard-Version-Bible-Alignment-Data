# JDG.16 process record

This directory contains the accepted, independently reviewed judgments for
JDG.16. Its run record retains the historical `ot-hebrew-verse-v185` contract stamp;
the current governing contracts for new work are under `config/`.

- `verses/` contains the accepted verse judgments.
- `reviews/` contains hash-bound independent-review receipts.
- `conventions.md`, when present, records chapter-local judgments.
- `provenance.json` records the contract, models, and artifact hashes.
- `source-lock-entries.json` records the pinned source entries used by this
  chapter.

The compiled chapter is `../JDG.16.jsonl`. Generated validation and
textual-projection reports are reproducible local outputs rather than published
source data.

Prepared verse inputs are not published. They contain no additional judgment:
from the repository root, `make fetch CHAPTER=JDG.16`, then
`make build CHAPTER=JDG.16`, then
`make prepare-alignment CHAPTER=JDG.16` recreates them under
`data/output/JDG.16/verses/` from the pinned sources and recorded textual
decisions.

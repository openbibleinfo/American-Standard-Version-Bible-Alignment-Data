# PSA.64 process record

This directory contains the accepted, independently reviewed judgments for
PSA.64. Its run record retains the historical `ot-hebrew-verse-v217` contract stamp;
the current governing contracts for new work are under `config/`.

- `verses/` contains the accepted verse judgments.
- `reviews/` contains hash-bound independent-review receipts.
- `conventions.md`, when present, records chapter-local judgments.
- `provenance.json` records the contract, models, and artifact hashes.
- `source-lock-entries.json` records the pinned source entries used by this
  chapter.

The compiled chapter is `../PSA.64.jsonl`. Generated validation and
textual-projection reports are reproducible local outputs rather than published
source data.

Prepared verse inputs are not published. They contain no additional judgment:
from the repository root, `make fetch CHAPTER=PSA.64`, then
`make build CHAPTER=PSA.64`, then
`make prepare-alignment CHAPTER=PSA.64` recreates them under
`data/output/PSA.64/verses/` from the pinned sources and recorded textual
decisions.

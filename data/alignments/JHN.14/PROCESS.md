# JHN.14 process record

This directory contains the accepted, independently reviewed judgments for
JHN.14. Its run record retains the historical `nt-greek-verse-v29` contract stamp;
the current governing contracts for new work are under `config/`.

- `verses/` contains the accepted verse judgments.
- `reviews/` contains hash-bound independent-review receipts.
- `conventions.md`, when present, records chapter-local judgments.
- `provenance.json` records the contract, models, and artifact hashes.
- `source-lock-entries.json` records the pinned source entries used by this
  chapter.

The compiled chapter is `../JHN.14.jsonl`. Generated validation and
textual-projection reports are reproducible local outputs rather than published
source data.

Prepared verse inputs are not published. They contain no additional judgment:
from the repository root, `make fetch CHAPTER=JHN.14`, then
`make build CHAPTER=JHN.14`, then
`make prepare-alignment CHAPTER=JHN.14` recreates them under
`data/output/JHN.14/verses/` from the pinned sources and recorded textual
decisions.

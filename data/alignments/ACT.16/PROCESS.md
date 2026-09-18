# ACT.16 process record

This directory contains the accepted, independently reviewed judgments for
ACT.16. Its run record retains the historical `nt-greek-verse-v80` contract stamp;
the current governing contracts for new work are under `config/`.

- `verses/` contains the accepted verse judgments.
- `reviews/` contains hash-bound independent-review receipts.
- `conventions.md`, when present, records chapter-local judgments.
- `provenance.json` records the contract, models, and artifact hashes.
- `source-lock-entries.json` records the pinned source entries used by this
  chapter.

The compiled chapter is `../ACT.16.jsonl`. Generated validation and
textual-projection reports are reproducible local outputs rather than published
source data.

Prepared verse inputs are not published. They contain no additional judgment:
from the repository root, `make fetch CHAPTER=ACT.16`, then
`make build CHAPTER=ACT.16`, then
`make prepare-alignment CHAPTER=ACT.16` recreates them under
`data/output/ACT.16/verses/` from the pinned sources and recorded textual
decisions.

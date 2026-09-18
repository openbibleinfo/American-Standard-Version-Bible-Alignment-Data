PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

CHAPTER ?=
MODE ?= packet
VERSES ?=
REF ?=
PROVIDER ?=
MODEL ?=
EFFORT ?=
STAGE ?= full
WIDTH ?=
SAFE ?=
STEPS ?=
ALIGN ?= $(STEPS)
REVIEW ?= $(STEPS)
RECONCILE ?= $(STEPS)
DECISIONS ?= $(STEPS)
COMMIT ?= 1

require-chapter = @if [ -z "$(CHAPTER)" ]; then \
	  echo "make $@: CHAPTER is required (for example, CHAPTER=1CH.8)" >&2; \
	  exit 2; \
	fi

BATCH_FLAGS = $(if $(PROVIDER),--provider $(PROVIDER),) $(if $(MODEL),--model $(MODEL),) $(if $(EFFORT),--effort $(EFFORT),)
DRIVE_VARS = CHAPTER chapter STAGE WIDTH SAFE UNTIL LIMIT STEPS ALIGN REVIEW RECONCILE DECISIONS COMMIT PYTHON
DRIVE_UNKNOWN = $(filter-out $(DRIVE_VARS),$(foreach v,$(.VARIABLES),$(if $(filter command line,$(origin $(v))),$(v))))

LEMMA ?=
MORPH ?=
TARGET ?=
STATUS ?=
TESTAMENT ?=
LIKE ?=
PREV ?=
NEXT ?=
PREV_TARGET ?=
NEXT_TARGET ?=
NOTES ?=
CORPUS_FLAGS = $(if $(NOTES),--notes,) $(if $(LEMMA),--lemma '$(LEMMA)',) $(if $(MORPH),--morph '$(MORPH)',) $(if $(TARGET),--target '$(TARGET)',) $(if $(STATUS),--status '$(STATUS)',) $(if $(TESTAMENT),--testament '$(TESTAMENT)',) $(if $(LIKE),--like '$(LIKE)',) $(if $(PREV),--prev '$(PREV)',) $(if $(NEXT),--next '$(NEXT)',) $(if $(PREV_TARGET),--prev-target '$(PREV_TARGET)',) $(if $(NEXT_TARGET),--next-target '$(NEXT_TARGET)',)

.DEFAULT_GOAL := help

.PHONY: help fetch prefetch build check render status remaining init-decisions \
	init-divisions prepare-alignment compile-alignment validate-alignment \
	aligned-usx reverse-interlinear reverse-interlinear-index textual-report \
	evaluate-alignment finish drive batch-status batch-dry-run batch-submit \
	batch-poll batch-collect align-now batch-verse reconcile-view \
	reconcile-packet corpus-query verse-view clean-chapter test

help:
	@echo "ASV original-language alignment process"
	@echo
	@echo "Chapter workflow:"
	@echo "  make fetch CHAPTER=1CH.8"
	@echo "  make build CHAPTER=1CH.8"
	@echo "  make prepare-alignment CHAPTER=1CH.8"
	@echo "  make drive CHAPTER=1CH.8"
	@echo "  make status CHAPTER=1CH.8"
	@echo "  make finish CHAPTER=1CH.8"
	@echo
	@echo "Use COMMIT=0 on make drive to leave changes unstaged."
	@echo "See DRIVER.md and .claude/skills/align-chapter/SKILL.md."

fetch:
	$(require-chapter)
	./scripts/fetch_sources.sh --chapter $(CHAPTER)

prefetch:
	PYTHONPATH=src $(PYTHON) scripts/prefetch_sources.py $(if $(CHAPTER),$(CHAPTER),all)

build:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) -m alignment_research build $(CHAPTER)

check:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) -m alignment_research check $(CHAPTER)

render:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/render_alignment.py $(CHAPTER) --mode $(MODE) $(if $(VERSES),--verses $(VERSES),)

status:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py status $(CHAPTER)

remaining:
	PYTHONPATH=src $(PYTHON) scripts/remaining_chapters.py

init-decisions:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py init-decisions $(CHAPTER)

init-divisions:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py init-divisions $(CHAPTER)

prepare-alignment:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py prepare $(CHAPTER)

compile-alignment:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py compile $(CHAPTER)

validate-alignment:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_alignments.py validate $(CHAPTER)

aligned-usx:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_aligned_usx.py $(CHAPTER)

reverse-interlinear:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_reverse_interlinear.py --chapter $(CHAPTER)

reverse-interlinear-index:
	PYTHONPATH=src $(PYTHON) scripts/generate_reverse_interlinear.py --index-only

textual-report:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/generate_textual_report.py $(CHAPTER)

evaluate-alignment:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/evaluate_alignment.py $(CHAPTER)

finish: compile-alignment validate-alignment aligned-usx reverse-interlinear reverse-interlinear-index textual-report evaluate-alignment
	@echo
	@echo "$(CHAPTER) finishing checks passed."

drive:
	@if [ -z "$(CHAPTER)" ] && [ -z "$(chapter)" ]; then \
	  echo "make drive: CHAPTER is required (for example, CHAPTER=1CH.8)" >&2; \
	  exit 2; \
	fi
	@if [ -n "$(DRIVE_UNKNOWN)" ]; then \
	  echo "make drive: unrecognized variable(s): $(DRIVE_UNKNOWN)" >&2; \
	  echo "make drive reads: $(DRIVE_VARS)" >&2; \
	  exit 2; \
	fi
	@set -e; \
	drive_chapter='$(if $(chapter),$(chapter),$(CHAPTER))'; \
	PYTHON=$(PYTHON) ./scripts/drive.sh $(STAGE) "$$drive_chapter" $(if $(UNTIL),--until $(UNTIL),) $(if $(LIMIT),--limit $(LIMIT),) $(if $(WIDTH),--width $(WIDTH),) $(if $(SAFE),--safe,) $(if $(ALIGN),--align $(ALIGN),) $(if $(REVIEW),--review $(REVIEW),) $(if $(RECONCILE),--reconcile $(RECONCILE),) $(if $(DECISIONS),--decisions $(DECISIONS),) $(if $(filter 0,$(COMMIT)),--no-commit,)

batch-status:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py status $(CHAPTER)

batch-dry-run:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py submit $(CHAPTER) --mode $(MODE) $(BATCH_FLAGS) --dry-run $(if $(VERSES),--verses $(VERSES),)

batch-submit:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py submit $(CHAPTER) --mode $(MODE) $(BATCH_FLAGS) $(if $(VERSES),--verses $(VERSES),)

batch-poll:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py poll $(CHAPTER) --mode $(MODE) $(BATCH_FLAGS)

batch-collect:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py collect $(CHAPTER) --mode $(MODE) $(BATCH_FLAGS)

align-now:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py submit $(CHAPTER) --mode $(MODE) $(BATCH_FLAGS) --sync $(if $(VERSES),--verses $(VERSES),)

batch-verse:
	PYTHONPATH=src $(PYTHON) scripts/align_batch.py verse $(REF) --mode $(MODE) $(BATCH_FLAGS)

reconcile-view:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/reconcile_view.py $(CHAPTER)

reconcile-packet:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/reconcile_packet.py $(CHAPTER)

corpus-query:
	PYTHONPATH=src $(PYTHON) scripts/corpus_query.py $(CORPUS_FLAGS)

verse-view:
	PYTHONPATH=src $(PYTHON) scripts/verse_view.py $(REF)

clean-chapter:
	$(require-chapter)
	PYTHONPATH=src $(PYTHON) scripts/clean_chapter.py $(CHAPTER) --confirm '$(CONFIRM)'

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

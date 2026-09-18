#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENDOR="$ROOT/data/vendor"
CHAPTERS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --chapter)
      [[ $# -ge 2 ]] || { echo "--chapter requires a value" >&2; exit 2; }
      # Repeatable, and several chapters in one run is not a convenience: each
      # of these repositories is shared by every chapter, and checking one out
      # costs the same whether it was asked for one book file or forty. Asking
      # once for the union is the difference between a checkout per chapter and
      # a checkout per repository.
      for chapter_ref in ${2//,/ }; do
        CHAPTERS+=("$chapter_ref")
      done
      shift 2
      ;;
    *)
      echo "usage: $0 [--chapter BOOK.N]..." >&2
      exit 2
      ;;
  esac
done

[[ ${#CHAPTERS[@]} -gt 0 ]] || CHAPTERS=("JHN.1")

# The same pattern twice is the same checkout, and the pattern set is matched
# against the whole index on every checkout, so a duplicate is not free.
unique_lines() {
  [[ $# -gt 0 ]] || return 0
  printf '%s\n' "$@" | awk 'NF && !seen[$0]++'
}

# The supported books and their per-source coordinates live in one place:
# alignment_research.scope.BOOKS. An unsupported reference fails here.
asv_paths=()
oshb_paths=(/wlc/VerseMap.xml)
greek_paths=(/SBLGNT/tsv/macula-greek-SBLGNT.tsv)
sblgnt_paths=()
ot_chapters=()
nt_chapters=()
for CHAPTER in "${CHAPTERS[@]}"; do
  book_env=$(PYTHONPATH="$ROOT/src" python3 -m alignment_research book-env "$CHAPTER")
  eval "$book_env"
  asv_paths+=("/usx/$BOOK_USX_FILE")
  if [[ "$TESTAMENT" == "OT" ]]; then
    ot_chapters+=("$CHAPTER")
    # Usually just this book. A chapter with a verse the pinned edition does
    # not contain reads the parallel that supplies it out of another book, and
    # that decision is recorded in config/, so it can be asked for before
    # anything has been checked out.
    while read -r oshb_file; do
      oshb_paths+=("/wlc/$oshb_file")
    done < <(PYTHONPATH="$ROOT/src" python3 -m alignment_research \
      hebrew-oshb-files "$CHAPTER")
  else
    nt_chapters+=("$CHAPTER")
    greek_paths+=("/SBLGNT/lowfat/$BOOK_LOWFAT_FILE")
    sblgnt_paths+=(
      "/data/sblgnt/text/$BOOK_SBLGNT_NAME.txt"
      "/data/sblgntapp/text/$BOOK_SBLGNT_NAME.txt"
    )
  fi
done

mkdir -p "$VENDOR"

checkout_sparse() {
  local name=$1 url=$2 commit=$3
  shift 3
  local directory="$VENDOR/$name"

  if [[ ! -d "$directory/.git" ]]; then
    git clone --filter=blob:none --no-checkout "$url" "$directory"
  fi
  if ! git -C "$directory" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$directory" fetch --depth=1 origin "$commit"
  fi
  # Add to the existing pattern set instead of replacing it, so fetching a new
  # book keeps an already-processed chapter's sources on disk.
  local sparse="$directory/.git/info/sparse-checkout"
  if [[ ! -f "$sparse" ]]; then
    git -C "$directory" sparse-checkout init --no-cone
    git -C "$directory" sparse-checkout set --no-cone "$@"
    git -C "$directory" checkout --detach "$commit"
    return 0
  fi

  # Only what is not already asked for. `sparse-checkout add` appends whatever
  # it is given, so re-fetching a book it already has used to write the same
  # pattern again, because every duplicate is matched against every index
  # entry on every checkout.
  local wanted=() pattern
  for pattern in "$@"; do
    grep -qxF -- "$pattern" "$sparse" || wanted+=("$pattern")
  done

  # Nothing new, and already at the pinned commit, is the common case once a
  # book has been fetched, and it is worth detecting rather than doing: for
  # MACULA Hebrew, whose pattern set is one file per chapter, re-matching it
  # costs 13 seconds and the checkout another 31, to arrive at the working
  # tree that is already on disk.
  if [[ ${#wanted[@]} -eq 0 ]] &&
     [[ "$(git -C "$directory" rev-parse HEAD 2>/dev/null || true)" == "$commit" ]]; then
    return 0
  fi

  [[ ${#wanted[@]} -eq 0 ]] || git -C "$directory" sparse-checkout add "${wanted[@]}"
  git -C "$directory" checkout --detach "$commit"
}

mapfile -t asv_paths < <(unique_lines "${asv_paths[@]}")
checkout_sparse asv \
  https://github.com/openbibleinfo/American-Standard-Version-Bible.git \
  5c83ee265c75b3b1c056435eff622a875f1edc45 \
  "${asv_paths[@]}"

# The two testaments are aligned against different editions, so each fetches
# its own source side. MACULA Hebrew publishes one lowfat file per chapter, so
# only the chapters this one is built from are checked out; its whole-Bible TSV
# is a Git LFS object and is deliberately not used.
if [[ ${#ot_chapters[@]} -gt 0 ]]; then
  # OSHB comes first because it carries VerseMap.xml, and the versification
  # table is what says which Hebrew chapters these ASV chapters are written in.
  # Usually that is the chapter of the same number alone; the ASV's Isaiah 64:1
  # is written in the Hebrew Isaiah 63, so Isaiah 64 needs both.
  mapfile -t oshb_paths < <(unique_lines "${oshb_paths[@]}")
  checkout_sparse oshb \
    https://github.com/openscriptures/morphhb.git \
    3d15126fb1ef74867fc1434be1942e837932691f \
    "${oshb_paths[@]}"

  # Asked only now, because the answer is read out of the table the checkout
  # above is what puts on disk.
  hebrew_lowfat_paths=()
  for CHAPTER in "${ot_chapters[@]}"; do
    while read -r lowfat_file; do
      hebrew_lowfat_paths+=("/WLC/lowfat/$lowfat_file")
    done < <(PYTHONPATH="$ROOT/src" python3 -m alignment_research \
      hebrew-lowfat-files "$CHAPTER")
  done

  mapfile -t hebrew_lowfat_paths < <(unique_lines "${hebrew_lowfat_paths[@]}")
  checkout_sparse macula-hebrew \
    https://github.com/Clear-Bible/macula-hebrew.git \
    47db250bd55d0d8577f2a94fba114ef16c35b23c \
    "${hebrew_lowfat_paths[@]}"

fi

if [[ ${#nt_chapters[@]} -gt 0 ]]; then
  mapfile -t greek_paths < <(unique_lines "${greek_paths[@]}")
  checkout_sparse macula-greek \
    https://github.com/Clear-Bible/macula-greek.git \
    8423afe47b9e8f24b7772e808af45c7159a6fe7e \
    "${greek_paths[@]}"

  mapfile -t sblgnt_paths < <(unique_lines "${sblgnt_paths[@]}")
  checkout_sparse sblgnt \
    https://github.com/Faithlife/SBLGNT.git \
    c4d241a9c1c479a55b989ba35a4976c1d0b8052c \
    "${sblgnt_paths[@]}"

  checkout_sparse sblgnt-tei \
    https://github.com/jjmccollum/sblgnt-tei.git \
    2a95589b2a9cb9b8bda5372650c7f55745ceecad \
    /xml/sblgnt_tei.xml \
    /SBL_Errata.txt

fi

echo "Fetched and pinned sources for ${CHAPTERS[*]} are ready."

#!/usr/bin/env bash
# Drive several chapters through scripts/chapter.py, one after another.
#
# The driver itself takes exactly one chapter, and that is the right shape for
# it: a run owns one chapter's fetch, packets, quota window, and alignment
# queue. What a range needs is only a loop, so this is a loop and nothing else
# — a BOOK.N or BOOK.N-M spec expanded into chapter names and handed over one
# at a time.
#
# Chapters run in sequence within one invocation. On the Claude Code route that
# is a requirement rather than a preference: two drivers would race for the same
# 5-hour window, and the second would spend on a pass the first had already made
# unaffordable. On a provider route there is no window to race for, and running
# several of these at once in separate shells is how a run gets wide — each
# still owns one chapter's fetch, packets, and queue entry, which is what makes
# that safe. The two whole-chapter passes, textual decisions and reconciliation,
# default to Claude Code whatever the verse passes use, so wide runs meet at the
# window for those two unless they are routed to `codex` as well.
set -euo pipefail

# A run takes tens of minutes, and editing this file in place during one used to
# break it: bash reads a script in chunks as it goes, so rewriting the same
# inode shifted every byte past the chunk it had reached and it resumed parsing
# mid-line. Everything below is one group, which bash parses whole before
# running any of it, and the group ends in an `exit` so that bash never comes
# back to the file for a next command. An edit mid-run then lands in the next
# run instead of this one.
{

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-}
STAGE=""
SPECS=()
PASSTHROUGH=()
MAKE_ARGS=()
WINDOW_ARGS=()
BEYOND_MAKE=false
UNTIL=""
LIMIT=""
SELECTOR=""

usage() {
  echo "usage: $0 {align|review|full} BOOK.N[-M] [BOOK.N[-M] ...] [driver flags]" >&2
  echo "       --align ROUTE / --review ROUTE pick the model for each verse pass" >&2
  echo "       (code, code@high, codex, codex-astra, codex-sol, codex/terra, sol, terra:batch, gemini)" >&2
  echo "       --reconcile ROUTE / --decisions ROUTE pick the CLI for each" >&2
  echo "       whole-chapter pass (code, codex, codex-astra, or codex-sol)" >&2
  echo "       --no-commit leaves a finished run unstaged and uncommitted" >&2
  echo "       head|tail|headsize|tailsize --until PERCENT loops until a relevant window is lower" >&2
  echo "       head|tail|headsize|tailsize --limit COUNT stops after COUNT completed chapters" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    align|review|full)
      if [[ -n $STAGE ]]; then
        usage
      fi
      STAGE=$1
      shift
      ;;
    -h|--help)
      usage
      ;;
    --width|--align|--review|--reconcile|--decisions|--until|--limit)
      # The driver flags that take a value, so the value has to be carried
      # across rather than mistaken for a chapter. Each has a `make drive`
      # spelling, which is what a printed resume uses.
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      case "$1" in
        --width) MAKE_ARGS+=("WIDTH=$2") ;;
        --align) MAKE_ARGS+=("ALIGN=$2") ;;
        --review) MAKE_ARGS+=("REVIEW=$2") ;;
        --reconcile) MAKE_ARGS+=("RECONCILE=$2") ;;
        --decisions) MAKE_ARGS+=("DECISIONS=$2") ;;
        --until)
          UNTIL=$2
          MAKE_ARGS+=("UNTIL=$2")
          ;;
        --limit)
          LIMIT=$2
          MAKE_ARGS+=("LIMIT=$2")
          ;;
      esac
      if [[ $1 != --until && $1 != --limit ]]; then
        PASSTHROUGH+=("$1" "$2")
      fi
      case "$1" in
        --align|--review|--reconcile|--decisions)
          WINDOW_ARGS+=("$1" "$2")
          ;;
      esac
      shift 2
      ;;
    --safe)
      PASSTHROUGH+=("$1")
      MAKE_ARGS+=("SAFE=1")
      shift
      ;;
    --no-commit)
      PASSTHROUGH+=("$1")
      MAKE_ARGS+=("COMMIT=0")
      shift
      ;;
    -*)
      # Anything else is the driver's business: --json, --no-auto-safe and the
      # rest stay declared in one place rather than twice. They have no `make
      # drive` spelling, so a resume that used one is quoted as the script call
      # it was.
      PASSTHROUGH+=("$1")
      BEYOND_MAKE=true
      shift
      ;;
    *)
      case "$1" in
        HEAD|head) [[ -z $SELECTOR ]] || usage; SELECTOR=head ;;
        TAIL|tail) [[ -z $SELECTOR ]] || usage; SELECTOR=tail ;;
        HEADSIZE|headsize) [[ -z $SELECTOR ]] || usage; SELECTOR=headsize ;;
        TAILSIZE|tailsize) [[ -z $SELECTOR ]] || usage; SELECTOR=tailsize ;;
        *) SPECS+=("$1") ;;
      esac
      shift
      ;;
  esac
done

[[ -n $STAGE ]] || usage
[[ ${#SPECS[@]} -gt 0 || -n $SELECTOR ]] || usage
[[ ${#SPECS[@]} -eq 0 || -z $SELECTOR ]] || usage
if [[ -n $UNTIL ]]; then
  [[ $UNTIL =~ ^[0-9]+$ ]] && ((10#$UNTIL <= 100)) ||
    { echo "UNTIL must be an integer between 0 and 100" >&2; exit 2; }
fi
if [[ -n $LIMIT ]]; then
  [[ $LIMIT =~ ^[0-9]+$ ]] && ((10#$LIMIT > 0)) ||
    { echo "LIMIT must be a positive integer" >&2; exit 2; }
  [[ -n $SELECTOR ]] ||
    { echo "LIMIT requires CHAPTER=head, tail, headsize, or tailsize" >&2; exit 2; }
fi

# Expand the specs first, so a typo in the last one fails before the first
# chapter has spent anything.
CHAPTERS=()
for spec in "${SPECS[@]}"; do
  [[ $spec =~ ^([A-Z1-9]{3})\.([0-9]+)(-([0-9]+))?$ ]] ||
    { echo "chapter must look like GEN.1 or GEN.3-10, not '$spec'" >&2; exit 2; }
  book=${BASH_REMATCH[1]}
  first=${BASH_REMATCH[2]}
  last=${BASH_REMATCH[4]:-$first}
  # A range runs within one book, so the second number is a chapter, not BOOK.N.
  [[ $((10#$last)) -ge $((10#$first)) ]] ||
    { echo "range must not run backwards: '$spec'" >&2; exit 2; }
  for ((n = 10#$first; n <= 10#$last; n++)); do
    CHAPTERS+=("$book.$n")
  done
done

cd "$ROOT"

if [[ -z $PYTHON ]]; then
  PYTHON=$([[ -x .venv/bin/python ]] && echo .venv/bin/python || echo python3)
fi

# Name the whole remainder in a resume, not just the chapter that stopped: a
# halt partway through a range otherwise loses the chapters after it silently.
resume_spec() {
  local from=$1 rest=()
  local seen=false
  for chapter in "${CHAPTERS[@]}"; do
    if [[ $chapter == "$from" ]]; then
      seen=true
    fi
    if [[ $seen == true ]]; then
      rest+=("$chapter")
    fi
  done
  echo "${rest[*]}"
}

next_selected_chapter() {
  local flag=--head
  local sort=()
  [[ $SELECTOR == tail || $SELECTOR == tailsize ]] && flag=--tail
  [[ $SELECTOR == headsize || $SELECTOR == tailsize ]] && sort=(--sort size)
  PYTHONPATH=src "$PYTHON" scripts/remaining_chapters.py "$flag" "${sort[@]}"
}

check_until() {
  [[ -z $UNTIL ]] && return 0
  local status=0
  PYTHONPATH=src "$PYTHON" scripts/drive_until.py "$STAGE" "$UNTIL" \
    ${WINDOW_ARGS[@]+"${WINDOW_ARGS[@]}"} || status=$?
  return "$status"
}

run_chapter() {
  local chapter=$1 status=0
  if [[ -n $SELECTOR || ${#CHAPTERS[@]} -gt 1 ]]; then
    echo "==> $STAGE $chapter" >&2
  fi
  PYTHONPATH=src "$PYTHON" scripts/chapter.py "$STAGE" "$chapter" \
    ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || status=$?
  return "$status"
}

index=0
completed=0
declare -A ATTEMPTED_CHAPTERS=()
while true; do
  if [[ -n $SELECTOR ]]; then
    status=0
    chapter=$(next_selected_chapter) || status=$?
    if [[ $status -ne 0 || -z $chapter ]]; then
      echo "==> make remaining listed no chapters" >&2
      [[ -n $UNTIL || -n $LIMIT ]] && exit 0
      exit 1
    fi
    if [[ -n ${ATTEMPTED_CHAPTERS[$chapter]+present} ]]; then
      echo "==> fatal: $chapter exited successfully but was selected again from" \
        "the $SELECTOR of make remaining; refusing an unbounded retry" >&2
      echo "==> inspect it, then resume with: make drive CHAPTER=$SELECTOR" \
        "STAGE=$STAGE${MAKE_ARGS[@]+ ${MAKE_ARGS[*]}}" >&2
      exit 1
    fi
  else
    ((index < ${#CHAPTERS[@]})) || break
    chapter=${CHAPTERS[$index]}
  fi

  status=0
  check_until || status=$?
  if [[ $status -eq 3 ]]; then
    echo "==> resume with: make drive CHAPTER=${SELECTOR:-\"$(resume_spec "$chapter")\"}" \
      "STAGE=$STAGE${MAKE_ARGS[@]+ ${MAKE_ARGS[*]}}" >&2
    exit 0
  elif [[ $status -ne 0 ]]; then
    exit "$status"
  fi

  status=0
  run_chapter "$chapter" || status=$?
  if [[ $status -ne 0 ]]; then
    # The driver has already said what it halted on, written the alignment
    # queue, and named its own resume; the only thing it cannot know is what
    # came after it. So say nothing more when nothing else was owed, and
    # otherwise re-say the resume in the same words the driver used — `make
    # drive` is the entry point, and two dialects for one command read as two
    # commands.
    remaining=$(resume_spec "$chapter")
    if [[ -n $SELECTOR || $remaining != "$chapter" ]]; then
      echo >&2
      echo "==> $chapter halted; the rest of the run did not start" >&2
      if [[ $BEYOND_MAKE == true ]]; then
        echo "==> resume the rest with: $0 $STAGE ${SELECTOR:-$remaining}" \
          "${PASSTHROUGH[*]}${UNTIL:+ --until $UNTIL}" >&2
      else
        echo "==> resume the rest with: make drive CHAPTER=\"${SELECTOR:-$remaining}\"" \
          "STAGE=$STAGE${MAKE_ARGS[@]+ ${MAKE_ARGS[*]}}" >&2
      fi
    fi
    exit "$status"
  fi
  ATTEMPTED_CHAPTERS[$chapter]=1
  ((index += 1))
  ((completed += 1))
  if [[ -n $LIMIT ]] && ((completed >= 10#$LIMIT)); then
    break
  fi
  if [[ -z $UNTIL && -z $LIMIT && -n $SELECTOR ]]; then
    break
  fi
done

# Inside the group, so bash never reads this file again after the loop.
exit
}

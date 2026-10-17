# Jev: align any English translation

Jev aligns one verse of any English translation to the Greek or Hebrew, word
by word. It runs the
[Make Your Own Interlinear](https://www.openbible.info/labs/make-your-own-interlinear/)
page. Unlike the ASV process in the rest of this repository, it starts from
plain English text, with no prepared decisions or saved runs. It makes several
rounds of requests to a model through OpenRouter.

Python 3.10 or newer, standard library only. Run the commands below from the
repository root.

## Try it

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python3 jev/demo.py
```

aligns the ASV text of John 3:16, whose Greek tokens are written into
`demo.py`, and prints each English word beside the Greek it translates. Words
the model was unsure of are marked `?`. It makes one paid run, a cent or two.

## Source tokens

The service is given the verse's source tokens. They are not in this
repository; build them from the pinned sources for each chapter you need:

```bash
make fetch CHAPTER=JHN.3
make build CHAPTER=JHN.3
python3 jev/build_source_bundle.py
```

This writes `jev/source-bundle/` from every chapter built under
`data/normalized/`. For the whole Bible, run `make prefetch`, then
`make build` for every chapter, before building the bundle.

Readings are base SBLGNT and MACULA WLC. Verses follow NLT numbering: Psalm
titles are verse 0, Hebrew verses take their English numbers, the last words of
2 Cor 13:12 and Acts 19:40 become 2 Cor 13:13 and Acts 19:41 (SBLGNT's
2 Cor 13:13 becomes 13:14), and verses modern editions omit (Matt 17:21 and
others) come from the SBLGNT apparatus. Source credits are in
[`ATTRIBUTION.md`](../ATTRIBUTION.md).

## Aligning a verse

`jev/service.py` reads one JSON request on stdin and writes one JSON result on
stdout. It writes nothing to disk. Set `OPENROUTER_API_KEY`; every run makes
paid requests. Progress and errors go to stderr.

### Request

```json
{"ref": "JHN.3.16", "translation": "For God so loved the world...", "source": [...]}
```

- `ref`: one verse, as a USX book code, chapter and verse in NLT numbering
  (`PSA.3.0` for a Psalm title).
- `translation`: the English text, at most 400 characters.
- `source`: the verse's tokens, the list stored under `JHN.3.16` in
  `jev/source-bundle/JHN.3.json`.

For example, after building JHN.3:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python3 -c "
import json
tokens = json.load(open('jev/source-bundle/JHN.3.json'))['JHN.3.16']
print(json.dumps({'ref': 'JHN.3.16', 'source': tokens,
                  'translation': 'For God so loved the world, that he gave his only begotten Son.'}))
" | python3 jev/service.py
```

### Result

A failure is `{"ok": false, "error": "..."}`. The error explains a bad request
(an unknown reference, missing text, malformed tokens); anything else, such as
a missing key, is `"The alignment could not be completed."`, with the cause on
stderr.

A success is:

```json
{"ok": true, "reader": {...}, "flagged": ["JHN.3.16.a002:t003", ...], "verse_flagged": false}
```

The alignment is a list of **units**. Each unit joins some English words to the
source words they translate. In `reader`:

- `ref` (`JHN.3`), `name` (`John 3:16`), `language` (`grc` or `hbo`),
  `sourceName` (`Greek` or `Hebrew`) and `direction` (`ltr` or `rtl`).
- `units`: an object keyed by unit id (`JHN.3.16.a001`). Each unit has
  `english` and `source`, its English and source tokens in reading order;
  `englishHead` and `sourceHead`, the id of its main English and source word
  when it has one; and `status`:
  - `aligned`: the English translates these source words.
  - `noncompositional`: the English renders the source words as a whole, as
    with an idiom, not word by word.
  - `added`: English with no source words (`source` is empty).
  - `unrepresented`: source words the English leaves untranslated (`english`
    is empty).
- `english`: one verse, whose `parts` rebuild the English text. A part is a
  string (the spaces and punctuation between words), a paragraph marker
  (`{"paragraph": 1, "style": "p"}`), or an English token.
- `source`: the same verse in source order; its `parts` are the source tokens.
- `paragraphs` and `kjv` (always empty) are there so the output can be
  displayed by the ASV interlinear reader.

An English token is `{"id": "t001", "unit": "JHN.3.16.a001", "text": "For", ...}`.
Its `attrs` hold the transliteration of all its unit's source words
(`x-translit`) and the `lemma` and `strong` of the unit's main source word,
with `lemmaTranslit` beside them. A source token is:

```json
{"id": "SBLGNT.JHN.3.16.002", "unit": "JHN.3.16.a001", "text": "γὰρ",
 "attrs": {"lemma": "γάρ", "strong": "G1063", "x-morph": "CONJ", "x-gloss": "for", "x-translit": "gar"},
 "lemmaTranslit": "gar"}
```

Tokens also carry `italic` and, on source tokens, `sourceChanged`; both are
always `false` here.

`flagged` lists the words the model was unsure of, each as `unit:token id`,
English or source. Words left out of every unit are always flagged.
`verse_flagged` is `true` when the model was unsure of the verse as a whole.

The guidance the model follows is `jev/policies/catalog.json`, and
`navigation.json` beside it lays out its sections. The model is
`run_probe.MODEL`.

## Tests

```bash
make test
```

runs `jev/test_service.py` with the rest of the suite, with no model calls.

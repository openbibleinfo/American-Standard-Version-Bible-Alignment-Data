"""Build the service's source bundle: base-text tokens keyed by NLT verse.

Readings are the base texts only (SBLGNT, MACULA WLC); no ASV variant
decision is consulted. Versification follows the NLT:

* NT verses are SBLGNT verses, except where English numbering has a verse
  that SBLGNT folds into its neighbour (NT_MOVES).
* OT verses use the English numbering the normalized rows already carry
  (`ref`, with `source_ref` the Hebrew verse); superscriptions are verse 0.
* A verse that modern editions omit (a gap in the chapter's numbering) is
  filled from the SBLGNT apparatus reading that carries that verse label,
  with lemma/morphology/gloss copied only where the surface form is
  unambiguous elsewhere in the NT.

Outputs:
* jev/source-bundle/: per-chapter JSON with an index.json, read by bundle_input.py.
* jev/books.json: book codes and English names for the service.
"""
import hashlib
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO / 'src'))
from alignment_research.alignment import alternative_token, lexical_index, reading_token_refs  # noqa: E402
from alignment_research.scope import BOOKS  # noqa: E402
from alignment_research.transliteration import greek_transliteration, hebrew_transliteration  # noqa: E402

OUTPUT = ROOT / 'source-bundle'
NT = 'MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV'.split()
OT = 'GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP HAG ZEC MAL'.split()
# (SBLGNT verse, number of trailing tokens, NLT verse): English numbers these
# words as their own verse. A count of None moves the whole verse.
NT_MOVES = [
    ('2CO.13.13', None, '2CO.13.14'),
    ('2CO.13.12', 5, '2CO.13.13'),   # ἀσπάζονται ὑμᾶς οἱ ἅγιοι πάντες
    ('ACT.19.40', 6, 'ACT.19.41'),   # καὶ ταῦτα εἰπὼν ἀπέλυσεν τὴν ἐκκλησίαν
]
EXCLUDED_RAW = {'english', 'gloss', 'lemma', 'surface', 'normalized', 'transliteration'}


def verse_key(ref):
    book, chapter, verse = ref.split('.')
    return int(chapter), int(verse)


def lemma_transliteration(row):
    lemma = row.get('lemma')
    if not lemma:
        return ''
    return (greek_transliteration if row.get('language') == 'grc' else hebrew_transliteration)(lemma)


def token(row, origin):
    annotations = {k: v for k, v in row.get('raw', {}).items() if k not in EXCLUDED_RAW and v not in (None, '')}
    for key in ('role', 'source_xml_id', 'oshb_word'):
        if row.get(key) is not None:
            annotations[key] = row[key]
    return {'project_id': row['id'], 'surface': row['surface'], 'lemma': row.get('lemma'),
            'gloss': row.get('gloss'), 'morph': row.get('morph'), 'strong': row.get('strong'),
            'transliteration': row.get('transliteration'),
            'lemma_transliteration': lemma_transliteration(row),
            'native_ref': row.get('source_ref') or row['ref'], 'oshb_word': row.get('oshb_word'),
            'annotations': annotations, 'origin': origin}


def chapters(book):
    return sorted((p for p in (REPO / 'data/normalized').glob(f'{book}.*') if (p / 'source-tokens.jsonl').exists()),
                  key=lambda p: int(p.name.split('.')[1]))


def base_rows(book):
    for directory in chapters(book):
        for line in (directory / 'source-tokens.jsonl').read_text().splitlines():
            row = json.loads(line)
            if row.get('edition') in ('SBLGNT', 'WLC'):
                yield row


def apparatus_verse(book, ref, lexicon):
    """Tokens the apparatus labels as verse `ref`, from its first such reading."""
    chapter = '.'.join(ref.split('.')[:2])
    path = REPO / 'data/normalized' / chapter / 'source-variants.jsonl'
    for line in path.read_text().splitlines():
        unit = json.loads(line)
        for candidate in unit['candidates']:
            if candidate['base'] or not re.search(rf'(?<!\w){ref.split(".")[2]}\s+\S', candidate['text']):
                continue
            words = [w for w, at in reading_token_refs(candidate['text'].lstrip('+ '), unit['ref']) if at == ref]
            if words:
                rows = [alternative_token({'id': unit['id']}, candidate['id'], i, w, ref, lexicon)
                        for i, w in enumerate(words, 1)]
                return [token(r, 'apparatus:' + ','.join(candidate['witnesses'])) for r in rows]
    return []


def build_book(book, nt_lexicon):
    verses = {}
    rows = list(base_rows(book))
    for row in rows:
        if book in NT:
            ref = row.get('source_ref') or row['ref']
        else:
            ref = row['ref']
        verses.setdefault(ref, []).append(token(row, 'base'))
    if book in NT:
        for source, count, target in NT_MOVES:
            if source not in verses:   # its chapter is not built
                continue
            moving = verses[source] if count is None else verses[source][-count:]
            verses[source] = [] if count is None else verses[source][:-count]
            verses[target] = moving + verses.get(target, [])
        verses = {ref: tokens for ref, tokens in verses.items() if tokens}
        by_chapter = {}
        for ref in verses:
            by_chapter.setdefault(int(ref.split('.')[1]), set()).add(int(ref.split('.')[2]))
        for chapter, numbers in by_chapter.items():
            # English numbering can run past the base text (Rom 16:25-27 is
            # only in this SBLGNT's apparatus), so fill to the longer count.
            english = REPO / 'data/normalized' / f'{book}.{chapter}' / 'asv-target-tokens.jsonl'
            last = max([max(numbers)] + [int(json.loads(line)['ref'].split('.')[2]) for line in english.read_text().splitlines()])
            for number in range(1, last + 1):
                ref = f'{book}.{chapter}.{number}'
                if number not in numbers:
                    tokens = apparatus_verse(book, ref, nt_lexicon)
                    if not tokens:
                        raise ValueError(f'No base or apparatus text for {ref}')
                    verses[ref] = tokens
    return dict(sorted(verses.items(), key=lambda item: verse_key(item[0])))


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    OUTPUT.mkdir(exist_ok=True)
    # Every book, built or not, so the service accepts any reference.
    books = {book: {'name': BOOKS[book].name} for book in OT + NT}
    nt_lexicon = lexical_index([r for book in NT for r in base_rows(book)])
    index = {}
    for book in OT + NT:
        verses = build_book(book, nt_lexicon if book in NT else {})
        if not verses:
            continue
        by_chapter = {}
        for ref, tokens in verses.items():
            by_chapter.setdefault(int(ref.split('.')[1]), {})[ref] = tokens
        hashes = {}
        for chapter, chapter_verses in by_chapter.items():
            path = OUTPUT / f'{book}.{chapter}.json'
            path.write_text(json.dumps(chapter_verses, ensure_ascii=False, separators=(',', ':')))
            hashes[chapter] = hashlib.sha256(path.read_bytes()).hexdigest()
        apparatus = [ref for ref, tokens in verses.items() if tokens[0]['origin'] != 'base']
        index[book] = {'name': BOOKS[book].name, 'verses': len(verses), 'chapters': {str(c): {'verses': list(v), 'sha256': hashes[c]}
                                                           for c, v in by_chapter.items()},
                       'apparatus_verses': apparatus}
        print(book, len(verses), apparatus or '')
    (OUTPUT / 'index.json').write_text(json.dumps({'versification': 'NLT', 'books': index}, indent=2) + '\n')
    (ROOT / 'books.json').write_text(json.dumps(books, ensure_ascii=False, indent=1) + '\n')


if __name__ == '__main__':
    main()

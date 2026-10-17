"""Prepare one NLT-numbered verse from its source tokens.

This is the service's source path; the page reads the tokens from
bible_db.interlinear_source_verses, and local runs from jev/source-bundle/. Readings are the base texts (SBLGNT, MACULA
WLC), plus apparatus text for verses modern editions omit; see
build_source_bundle.py.
"""
import hashlib
import json
from pathlib import Path
import re

from general_input import HEBREW_INSTRUCTIONS, NT, OT, base_state

BUNDLE = Path(__file__).resolve().parent / 'source-bundle'
REF = re.compile(r'([1-3A-Z]{3})\.(\d+)\.(\d+)')
# Every single BSB verse fits (the longest, Esth 8:9, is 400 characters).
MAX_TRANSLATION_CHARS = 400
ANNOTATED = ('class', 'type', 'role', 'syntax')


class Bundle:
    def __init__(self, root=BUNDLE):
        self.root = root
        self.index = json.loads((root / 'index.json').read_text())['books']
        self._chapters = {}

    def chapter(self, book, chapter):
        key = (book, chapter)
        if key not in self._chapters:
            entry = self.index.get(book, {}).get('chapters', {}).get(str(chapter))
            if entry is None:
                raise ValueError(f'{book} {chapter} is not available.')
            raw = (self.root / f'{book}.{chapter}.json').read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Source bundle file changed.')
            self._chapters[key] = (json.loads(raw), hashlib.sha256(raw).hexdigest())
        return self._chapters[key]

    def verse(self, label):
        """The tokens for one canonical NLT verse (see canonical_ref)."""
        book, chapter, _ = label.split('.')
        known = str(int(chapter)) in self.index.get(book, {}).get('chapters', {})
        verses = self.chapter(book, int(chapter))[0] if known else {}
        if label not in verses:
            raise ValueError(f'{book} {label.split(".", 1)[1].replace(".", ":")} is not in the NLT versification.')
        return verses[label]


def canonical_ref(ref):
    """BOOK.C.V for one NLT verse; verse 0 is a Psalm title."""
    match = REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise ValueError('Use a single-verse reference such as JHN.3.16.')
    book, chapter, verse = match.groups()
    if book not in NT | OT:
        raise ValueError('Unsupported book.')
    return book, f'{book}.{int(chapter)}.{int(verse)}'


def checked_tokens(tokens):
    """Tokens from the interlinear_source_verses table, as the page supplies them."""
    required = {'project_id', 'surface', 'lemma', 'gloss', 'morph', 'native_ref', 'annotations', 'origin'}
    if (not isinstance(tokens, list) or not tokens or len(tokens) > 300
            or any(not isinstance(t, dict) or not required <= set(t) or not isinstance(t['annotations'], dict) for t in tokens)):
        raise ValueError('No source text is available for this reference.')
    return tokens


def hebrew_words(tokens, ids):
    groups, by_key, mapping = [], {}, {}
    for sid, token in zip(ids, tokens):
        if not isinstance(token.get('oshb_word'), int):
            raise ValueError('No authoritative word index; do not infer boundaries')
        key = (token['native_ref'], token['oshb_word'])
        if key not in by_key:
            group = {'id': f'w{len(groups) + 1:03d}', 'source_ref': key[0], 'oshb_word': key[1], 'members': [], 'surface': ''}
            by_key[key] = group
            groups.append(group)
        group = by_key[key]
        if groups[-1] is not group:
            raise ValueError('Noncontiguous word group')
        group['members'].append(sid)
        group['surface'] += token['surface']
        mapping[sid] = {'word_id': group['id'], 'segment_index': len(group['members']),
                        'source_class': token['annotations'].get('class'),
                        'source_type': token['annotations'].get('type')}
    for group in groups:
        for sid in group['members']:
            mapping[sid]['same_word_members'] = group['members']
    return {'description': 'Ordered Hebrew orthographic words from the selected projection’s authoritative OSHB word indices. Members are separate alignment morphemes within that word; grouping is source spelling evidence, not an English alignment or permission to merge units.',
            'words': groups, 'morpheme_locations': mapping}


def prepare_reference(ref, translation, tokens=None, bundle=None):
    """Tokens come from the page (its database row) or, locally, from the bundle."""
    book, label = canonical_ref(ref)
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError('A nonempty English translation is required.')
    if len(translation) > MAX_TRANSLATION_CHARS:
        raise ValueError(f'Enter at most {MAX_TRANSLATION_CHARS} characters of translation.')
    tokens = checked_tokens((bundle or Bundle()).verse(label) if tokens is None else tokens)
    hashes = {label: hashlib.sha256(json.dumps(tokens, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    ids = [f's{i:03d}' for i in range(1, len(tokens) + 1)]
    source = {sid: {k: t.get(k) for k in ('surface', 'lemma', 'gloss', 'morph')} | {'project_id': t['project_id']}
              for sid, t in zip(ids, tokens)}
    language = 'greek' if book in NT else 'hebrew'
    state, target = base_state(label, language, source, translation)
    provenance = {'source': 'interlinear_source_verses', 'versification': 'NLT',
                  'tokens_sha256': hashes, 'reading_origin': tokens[0]['origin'],
                  'translation_sha256': hashlib.sha256(translation.encode()).hexdigest(),
                  'baseline_access': False, 'target_metadata_inherited': False,
                  # Display only; never part of the model state.
                  'display': [{'id': sid, 'strong': tok.get('strong'), 'transliteration': tok.get('transliteration'),
                               'lemma_transliteration': tok.get('lemma_transliteration'),
                               'origin': tok['origin']} for sid, tok in zip(ids, tokens)]}
    if language == 'hebrew':
        state['source_word_context'] = hebrew_words(tokens, ids) | {'provenance': {
            'source_tokens_sha256': hashes, 'field': 'oshb_word', 'baseline_access': False}}
        state['instructions'] += HEBREW_INSTRUCTIONS
    enriched, unavailable = {}, {}
    for sid, token in zip(ids, tokens):
        annotations = dict(token['annotations'])
        if token['origin'] != 'base':
            annotations['reading_origin'] = token['origin']
        enriched[sid] = source[sid] | {'annotations': annotations}
        unavailable[sid] = [key for key in ANNOTATED if not annotations.get(key)]
    packet = {'ref': label, 'source_tokens': [{'id': sid, **source[sid]} for sid in ids],
              'target_tokens': [{'id': tid, 'text': word} for tid, word in target.items()],
              'runtime_evidence': {'source_tokens': enriched, 'source_annotation_context': {
                  'status': 'selected_projection_verified', 'unavailable_annotation_fields': unavailable,
                  'provenance': {'source_tokens_sha256': hashes, 'matching_fields': ['project_id', 'surface', 'lemma', 'gloss', 'morph'],
                                 'baseline_access': False, 'contextual_english_substituted': False}}}}
    return state, packet, provenance

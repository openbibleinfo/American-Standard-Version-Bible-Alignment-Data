"""Turn one aligned verse into the ASV interlinear reader's chapter data.

The page embeds this as #chapter-data and runs the published reader.js and
reader.css unchanged; see src/alignment_research/html_reader.chapter_data for
the model it mirrors. Words the alignment left unassigned become their own
added/unrepresented units so every word still displays, and are flagged.
"""
import json
from pathlib import Path

BOOKS = json.loads((Path(__file__).resolve().parent / 'books.json').read_text())


def reader_data(ref, language, translation, state, tokens, units, flags):
    """`tokens` are the source rows in state order; `flags` are {'source', 'target'} id lists."""
    book, chapter, verse = ref.split('.')
    public = f'{book}.{chapter}.{verse}'
    source_ids = list(state['source_tokens'])
    row = dict(zip(source_ids, tokens))
    target_ids = list(state['target_tokens'])
    units = [dict(u) for u in units]
    assigned_target = {t for u in units for t in u['target']}
    assigned_source = {s for u in units for s in u['source']}
    flagged_target = {t for f in flags for t in f['target']}
    flagged_source = {s for f in flags for s in f['source']}
    for tid in target_ids:
        if tid not in assigned_target:
            units.append({'source': [], 'target': [tid], 'source_head': None, 'target_head': tid, 'status': 'added'})
            flagged_target.add(tid)
    for sid in source_ids:
        if sid not in assigned_source:
            units.append({'source': [sid], 'target': [], 'source_head': sid, 'target_head': None, 'status': 'unrepresented'})
            flagged_source.add(sid)

    unit_of_target, unit_of_source, model_units = {}, {}, {}
    for number, unit in enumerate(units, 1):
        uid = f'{public}.a{number:03d}'
        sources = [s for s in source_ids if s in unit['source']]
        source_tokens = [{
            'id': row[s]['project_id'], 'unit': uid, 'text': row[s]['surface'], 'italic': False,
            'attrs': {k: v for k, v in {'lemma': row[s].get('lemma'), 'strong': row[s].get('strong'),
                                         'x-morph': row[s].get('morph'), 'x-gloss': row[s].get('gloss'),
                                         'x-translit': row[s].get('transliteration')}.items() if v},
            'lemmaTranslit': row[s].get('lemma_transliteration') or '', 'sourceChanged': False} for s in sources]
        head = row.get(unit['source_head']) if unit['source_head'] else None
        english_attrs = {k: v for k, v in {
            'x-translit': ' '.join(row[s].get('transliteration') or '' for s in sources).strip(),
            'lemma': head and head.get('lemma'), 'strong': head and head.get('strong')}.items() if v}
        english_tokens = [{'id': t, 'unit': uid, 'text': state['target_tokens'][t], 'italic': False,
                           'attrs': dict(english_attrs), 'lemmaTranslit': (head or {}).get('lemma_transliteration') or ''}
                          for t in target_ids if t in unit['target']]
        model = {'english': english_tokens, 'source': source_tokens, 'status': unit['status']}
        if unit['target_head'] in unit['target']:
            model['englishHead'] = unit['target_head']
        if unit['source_head'] in unit['source']:
            model['sourceHead'] = row[unit['source_head']]['project_id']
        model_units[uid] = model
        for token in english_tokens:
            unit_of_target[token['id']] = token
        for s, token in zip(sources, source_tokens):
            unit_of_source[s] = token

    spans = state['target_word_spans']
    parts, cursor = [{'paragraph': 1, 'style': 'p'}], 0
    for tid in target_ids:
        if spans[tid]['start'] > cursor:
            parts.append(translation[cursor:spans[tid]['start']])
        parts.append(unit_of_target[tid])
        cursor = spans[tid]['end']
    if cursor < len(translation):
        parts.append(translation[cursor:])
    unrepresented = [unit_of_source[s] for s in source_ids
                     if not model_units[unit_of_source[s]['unit']]['english']]
    number = 'title' if verse == '0' else verse
    attrs = {'number': number, 'style': 'v'}
    if unrepresented:
        attrs.update({'x-unrepresented-source': ' '.join(t['text'] for t in unrepresented),
                      'x-unrepresented-translit': ' '.join(t['attrs'].get('x-translit', '') for t in unrepresented),
                      'x-unrepresented-src': ' '.join(t['id'] for t in unrepresented)})
    english = {'ref': public, 'number': number, 'parts': parts, 'attrs': attrs}
    source = {'ref': public, 'number': number, 'attrs': {'number': number, 'style': 'v'},
              'parts': [{'paragraph': 2, 'style': 'p'}] + [unit_of_source[s] for s in source_ids]}
    name = BOOKS[book]['name'] + (f' {chapter} title' if verse == '0' else f' {chapter}:{verse}')
    flagged = sorted({f"{unit_of_target[t]['unit']}:{t}" for t in flagged_target if t in unit_of_target}
                     | {f"{unit_of_source[s]['unit']}:{unit_of_source[s]['id']}" for s in flagged_source if s in unit_of_source})
    return {'ref': f'{book}.{chapter}', 'name': name,
            'language': 'grc' if language == 'greek' else 'hbo',
            'sourceName': 'Greek' if language == 'greek' else 'Hebrew',
            'direction': 'ltr' if language == 'greek' else 'rtl',
            'paragraphs': [{'id': 1, 'style': 'p'}], 'english': [english], 'source': [source],
            'units': model_units, 'kjv': []}, flagged

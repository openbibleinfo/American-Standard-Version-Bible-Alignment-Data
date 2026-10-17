"""Recover Hebrew orthographic grouping from selected source metadata only."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent

def hebrew_word_context(packet):
    book,chapter,_=packet['ref'].split('.')
    path=ROOT/'data/output'/f'{book}.{chapter}'/'projected-source-tokens.jsonl'
    rows={row['id']:row for line in path.read_text().splitlines() if (row:=json.loads(line))['ref']==packet['ref']}
    groups=[];by_key={};mapping={};seen=set()
    for token in packet['source_tokens']:
        row=rows[token['project_id']]
        for key in ['surface','lemma','morph','gloss']:
            if row.get(key)!=token.get(key):raise ValueError(f'Selected token metadata differs: {token["id"]} {key}')
        if not isinstance(row.get('oshb_word'),int):raise ValueError('No authoritative word index; do not infer boundaries')
        # OSHB ketiv rows carry only their own verse ref.
        key=(row.get('source_ref',row['ref']),row['oshb_word'])
        if key not in by_key:
            group={'id':f'w{len(groups)+1:03d}','source_ref':key[0],'oshb_word':key[1],'members':[],'surface':''}
            by_key[key]=group;groups.append(group)
        group=by_key[key]
        if groups[-1] is not group:raise ValueError('Noncontiguous word group')
        group['members'].append(token['id']);group['surface']+=token['surface']
        mapping[token['id']]={'word_id':group['id'],'segment_index':len(group['members']),
                              'source_class':row.get('raw',{}).get('class'),
                              'source_type':row.get('raw',{}).get('type')}
        seen.add(token['id'])
    assert seen=={t['id'] for t in packet['source_tokens']}
    for group in groups:
        for sid in group['members']:mapping[sid]['same_word_members']=group['members']
    return {'description':'Ordered Hebrew orthographic words from the selected projection’s authoritative OSHB word indices. Members are separate alignment morphemes within that word; grouping is source spelling evidence, not an English alignment or permission to merge units.',
            'words':groups,'morpheme_locations':mapping,
            'provenance':{'path':str(path.relative_to(ROOT)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'field':'oshb_word','baseline_access':False}}


def selected_source_annotations(packet, repo=ROOT):
    """Read provenance-backed annotation fields without replacing gloss inputs.

    Only exact selected projection rows qualify. Missing annotations are recorded,
    not inferred from spelling, morphology, accepted English, or a historical run.
    The returned supplement belongs to later evidence decisions, not the initial
    ownership payload.
    """
    book, chapter, _ = packet['ref'].split('.')
    path = repo / 'data/output' / f'{book}.{chapter}' / 'projected-source-tokens.jsonl'
    if not path.exists():
        return {'source_annotation_context': {'status': 'unavailable', 'reason': 'selected_projection_annotations_missing'}}
    raw_bytes = path.read_bytes()
    rows = {row['id']: row for line in raw_bytes.decode().splitlines()
            if (row := json.loads(line))['ref'] == packet['ref']}
    enriched, unavailable = {}, {}
    for token in packet['source_tokens']:
        row = rows.get(token.get('project_id'))
        if row is None:
            raise ValueError(f'Selected source annotation row missing: {token["id"]}')
        for key in ('surface', 'lemma', 'morph', 'gloss'):
            if row.get(key) != token.get(key):
                raise ValueError(f'Selected source annotation differs: {token["id"]} {key}')
        fields = {key: value for key, value in row.get('raw', {}).items()
                  if key not in {'english', 'gloss', 'lemma', 'surface', 'normalized', 'transliteration'}
                  and value is not None and value != ''}
        for key in ('role', 'source_xml_id', 'oshb_word'):
            if row.get(key) is not None:
                fields[key] = row[key]
        enriched[token['id']] = {key: token.get(key) for key in ('surface', 'lemma', 'gloss', 'morph', 'project_id')}
        enriched[token['id']]['annotations'] = fields
        unavailable[token['id']] = [key for key in ('class', 'type', 'role', 'syntax') if not fields.get(key)]
    return {'source_tokens': enriched, 'source_annotation_context': {
        'status': 'selected_projection_verified', 'unavailable_annotation_fields': unavailable,
        'provenance': {'path': str(path.relative_to(repo)), 'sha256': hashlib.sha256(raw_bytes).hexdigest(),
                       'matching_fields': ['project_id', 'ref', 'surface', 'lemma', 'gloss', 'morph'],
                       'baseline_access': False, 'contextual_english_substituted': False}}}

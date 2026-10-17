"""Neutral source input plus arbitrary English; no accepted-answer reads."""
import hashlib
import json
import re
from pathlib import Path

from source_context import hebrew_word_context, selected_source_annotations
from translation_context import WORD_RE, punctuated_context

REPO = Path(__file__).resolve().parent.parent
NT = set('MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV'.split())
OT = set('GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP HAG ZEC MAL'.split())


def base_state(ref, language, source, translation):
    """The verse state every source path shares: tokens, English context, task."""
    target = {f't{i:03d}': m.group() for i, m in enumerate(WORD_RE.finditer(translation), 1)}
    if not target or len(target) > 300:
        raise ValueError('Supply between 1 and 300 lexical target occurrences.')
    state = {
        'ref': ref, 'language': language, 'source_tokens': source,
        'target_tokens': target, **punctuated_context(translation, target),
        'task': 'Align the supplied English verse to this fixed source projection. IDs distinguish occurrences. Source and English order can differ.',
        'instructions': 'Prefer the smallest independently defensible correspondence. Use lexical meaning, morphology, and context. English helpers belong to what licenses them. Glosses are fallible hints. Keep the selected reading fixed.',
    }
    return state, target


HEBREW_INSTRUCTIONS = ' Segmented prefixes and suffixes are independent source tokens. Present function morphemes have priority over supplied helpers. Do not merge untranslated morphemes into their hosts.'


def prepare(ref, translation, repo=REPO, evidence=None):
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Z1-3]{3}\.[1-9][0-9]*\.[1-9][0-9]*', ref):
        raise ValueError('Use a canonical verse reference such as JHN.1.1.')
    book, chapter, verse = ref.split('.')
    if book not in NT | OT:
        raise ValueError('Unsupported book.')
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError('A nonempty English translation is required.')
    if len(translation.encode()) > 20000:
        raise ValueError('Single-verse translation exceeds the input limit.')
    path = repo / 'data/output' / f'{book}.{chapter}' / 'verses' / f'{int(verse):02d}.json'
    if not path.exists():
        path = path.with_name(f'{verse}.json')
    raw = path.read_bytes()
    packet = json.loads(raw)
    if packet['ref'] != ref:
        raise ValueError('Selected source packet is invalid.')
    declared_omission = packet.get('source_omission')
    if declared_omission is not None:
        if not isinstance(declared_omission, dict):
            raise ValueError('Malformed prepared source-omission declaration.')
        allowed = {'resolution', 'reason_code', 'origin_ref', 'source_edition'}
        if not set(declared_omission) <= allowed:
            raise ValueError('Prepared omission accepts canonical metadata, not commentary.')
        if declared_omission.get('resolution') not in {'no_replacement', 'parallel_source', 'parallel_replacement'}:
            raise ValueError('Unknown prepared source-omission resolution.')
        if any(not isinstance(value, str) or not value.strip() for value in declared_omission.values()):
            raise ValueError('Malformed prepared source-omission field.')
        if 'origin_ref' in declared_omission and not re.fullmatch(r'[A-Z1-3]{3}\.[1-9][0-9]*\.[1-9][0-9]*', declared_omission['origin_ref']):
            raise ValueError('Omission origin must be a canonical verse reference.')
        if declared_omission['resolution'] != 'no_replacement' and 'origin_ref' not in declared_omission:
            raise ValueError('Parallel replacement needs an actual origin reference.')
    if not packet['source_tokens']:
        if (book not in OT or not declared_omission
                or declared_omission.get('resolution') != 'no_replacement'
                or not isinstance(declared_omission.get('reason_code'), str)
                or not re.fullmatch(r'[A-Z][A-Z0-9_]*', declared_omission['reason_code'])):
            raise ValueError('An empty source requires a declared Hebrew omission with no_replacement and a canonical reason_code.')
    elif declared_omission and declared_omission.get('resolution') == 'no_replacement':
        raise ValueError('Declared no-replacement omission contradicts nonempty supplied source.')
    source = {
        t['id']: {k: t.get(k) for k in ('surface', 'lemma', 'gloss', 'morph', 'project_id')}
        for t in packet['source_tokens']
    }
    if len(source) != len(packet['source_tokens']):
        raise ValueError('Duplicate source occurrence ID.')
    language = 'greek' if book in NT else 'hebrew'
    state, target = base_state(ref, language, source, translation)
    if language == 'hebrew':
        if repo != REPO and source:
            raise ValueError('Alternate-root Hebrew grouping is not supported.')
        state['source_word_context'] = hebrew_word_context(packet) if source else {
            'words': [], 'morpheme_locations': {}, 'description': 'No source occurrences in the declared omitted verse.'}
        state['instructions'] += HEBREW_INSTRUCTIONS
    neutral_packet = {
        'ref': ref, 'source_tokens': packet['source_tokens'],
        'target_tokens': [{'id': tid, 'text': word} for tid, word in target.items()],
    }
    # Optional independent evidence is supplied only to later scoped decisions,
    # not substituted for glosses or inserted into the preserved initial prompt.
    neutral_packet['runtime_evidence'] = selected_source_annotations(packet, repo)
    if evidence is not None:
        if not isinstance(evidence, dict) or not set(evidence) <= {'evidence', 'translation_context', 'source_lexical_evidence'}:
            raise ValueError('Unsupported supplemental evidence namespace.')
        if len(json.dumps(evidence, ensure_ascii=False).encode()) > 20000:
            raise ValueError('Supplemental evidence exceeds the input limit.')
        neutral_packet['runtime_evidence'].update(json.loads(json.dumps(evidence)))
        neutral_packet['runtime_evidence']['supplemental_evidence_provenance'] = {
            'origin': 'explicitly_supplied', 'selected_source_verified': False,
            'sha256': hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    if declared_omission:
        supplied = neutral_packet['runtime_evidence'].setdefault('evidence', {})
        if supplied.get('source_omission') not in (None, declared_omission):
            raise ValueError('Supplemental evidence cannot override the prepared source-omission declaration.')
        supplied['source_omission'] = declared_omission

    if packet.get('variant_decisions'):
        neutral_packet['source_projection'] = {'variant_units': [
            {key: value for key, value in unit.items()
             if key in {'unit_id', 'base_source_ids', 'status', 'fallback_used',
                        'selected_reading', 'operational_reading', 'compatible_readings', 'projected_source_ids'}}
            for unit in packet['variant_decisions']]}
    provenance = {
        'packet_path': str(path.relative_to(repo)),
        'packet_sha256': hashlib.sha256(raw).hexdigest(),
        'translation_sha256': hashlib.sha256(translation.encode()).hexdigest(),
        'baseline_access': False,
        'target_metadata_inherited': False,
    }
    return state, neutral_packet, provenance

"""The web service path: supplied tokens, reader model, review flags, input errors."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import service
from bundle_input import canonical_ref, prepare_reference
from reader_model import reader_data


def token(n, surface, lemma, morph='N', origin='base'):
    return {'project_id': f'SBLGNT.JHN.1.1.{n:03d}', 'surface': surface, 'lemma': lemma, 'gloss': lemma,
            'morph': morph, 'strong': f'G{n}', 'transliteration': surface, 'lemma_transliteration': lemma,
            'native_ref': 'JHN.1.1', 'oshb_word': None, 'annotations': {'class': 'noun'}, 'origin': origin}


TOKENS = [token(1, 'Ἐν', 'ἐν'), token(2, 'ἀρχῇ', 'ἀρχή'), token(3, 'ἦν', 'εἰμί'), token(4, 'ὁ', 'ὁ')]


class ServicePathTests(unittest.TestCase):
    def test_references_are_single_canonical_verses(self):
        self.assertEqual(('JHN', 'JHN.3.16'), canonical_ref('JHN.03.016'))
        self.assertEqual(('PSA', 'PSA.51.0'), canonical_ref('PSA.51.0'))
        for bad in ['JHN.3.16-17', 'John 3:16', 'XYZ.1.1', None]:
            with self.assertRaises(ValueError):
                canonical_ref(bad)

    def test_supplied_tokens_build_the_state_and_are_checked(self):
        state, packet, provenance = prepare_reference('JHN.1.1', 'In the beginning was', tokens=TOKENS)
        self.assertEqual(['s001', 's002', 's003', 's004'], list(state['source_tokens']))
        self.assertEqual('SBLGNT.JHN.1.1.002', state['source_tokens']['s002']['project_id'])
        self.assertEqual({'t001': 'In', 't002': 'the', 't003': 'beginning', 't004': 'was'}, state['target_tokens'])
        self.assertNotIn('strong', state['source_tokens']['s001'], 'Display fields stay out of the model state')
        for bad in [[], [{'surface': 'x'}], 'tokens']:
            with self.assertRaises(ValueError):
                prepare_reference('JHN.1.1', 'In', tokens=bad)
        with self.assertRaises(ValueError):
            prepare_reference('JHN.1.1', 'x' * 401, tokens=TOKENS)

    def test_reader_model_keeps_every_word_and_flags_unassigned_ones(self):
        text = 'In the beginning was'
        state, _, _ = prepare_reference('JHN.1.1', text, tokens=TOKENS)
        units = [{'source': ['s001'], 'target': ['t001'], 'source_head': 's001', 'target_head': 't001', 'status': 'aligned'},
                 {'source': ['s002'], 'target': ['t002', 't003'], 'source_head': 's002', 'target_head': 't003', 'status': 'aligned'}]
        data, flagged = reader_data(state['ref'], state['language'], text, state, TOKENS, units,
                                    [{'source': ['s002'], 'target': ['t003']}])
        english = [p for p in data['english'][0]['parts'] if isinstance(p, dict) and 'unit' in p]
        self.assertEqual(['In', 'the', 'beginning', 'was'], [t['text'] for t in english])
        self.assertEqual(text, ''.join(p if isinstance(p, str) else p['text'] for p in data['english'][0]['parts'][1:]))
        by_unit = {uid: u for uid, u in data['units'].items()}
        statuses = sorted(u['status'] for u in by_unit.values())
        self.assertEqual(['added', 'aligned', 'aligned', 'unrepresented', 'unrepresented'], statuses)
        self.assertEqual('ἦν ὁ', data['english'][0]['attrs']['x-unrepresented-source'])
        self.assertIn('JHN.1.1.a002:t003', flagged)
        self.assertIn('JHN.1.1.a002:SBLGNT.JHN.1.1.002', flagged)
        self.assertTrue(any(key.endswith(':t004') for key in flagged), 'Unassigned English is flagged')
        self.assertEqual('John 1:1', data['name'])
        self.assertEqual('JHN.1', data['ref'])

    def test_review_flags_resolve_units_and_subjects(self):
        members = {'u1': {'source': ['s002'], 'target': ['t003', 't001']}}
        self.assertEqual({'reason': 'r', 'source': ['s002'], 'target': ['t001', 't003']},
                         service.flagged({'reason': 'r', 'unit': 'u1'}, members))
        self.assertEqual(['s002'], service.flagged({'reason': 'r', 'subject': 'u1'}, members)['source'])
        self.assertEqual(['t004'], service.flagged({'reason': 'r', 'subject': 't004'}, members)['target'])

    def test_input_errors_are_reported_without_calls(self):
        for raw in ['nope', json.dumps({'ref': 'JHN.1.1'}),
                    json.dumps({'ref': 'JHN.1.1-2', 'translation': 'x', 'source': TOKENS}),
                    json.dumps({'ref': 'John.1.1', 'translation': 'x', 'source': TOKENS})]:
            out = io.StringIO()
            with patch('sys.stdin', io.StringIO(raw)), redirect_stdout(out), \
                 patch.object(service, 'ServiceRunner', side_effect=AssertionError('no runner')):
                self.assertEqual(0, service.main())
            result = json.loads(out.getvalue())
            self.assertFalse(result['ok'])
            self.assertNotIn('could not be completed', result['error'])


if __name__ == '__main__':
    unittest.main()

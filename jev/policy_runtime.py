"""Experimental bounded navigation of the draft catalog, without linguistic routing."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_MEMBERSHIP = {'membership.independent_units', 'membership.discontinuous', 'membership.helpers', 'hebrew.morpheme_licensing', 'hebrew.no_convenience_merge', 'hebrew.independent_units', 'hebrew.discontinuous', 'hebrew.verse_locality', 'hebrew.evidence_priority'}
HEBREW_ROLES = {'membership.independent_units': 'hebrew.independent_units',
                'membership.discontinuous': 'hebrew.discontinuous',
                'unit.noncompositional': 'hebrew.noncompositional',
                'head.source_multiple': 'hebrew.source_multiple',
                'head.target_anchor': 'hebrew.target_anchor',
                'head.lexical_predicate': 'hebrew.lexical_predicate'}


class Catalog:
    def __init__(self, path=ROOT / 'policies/catalog.json'):
        raw = path.read_bytes()
        self.data = json.loads(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.policies = {p['id']: p for p in self.data['policies']}
        self.index = json.loads((ROOT / 'policies/navigation.json').read_text())
        indexed = [pid for family in self.index['families'] for pid in family['policies']]
        if len(indexed) != len(set(indexed)) or set(indexed) != set(self.policies):
            raise ValueError('Every catalog policy must occur in one navigation family.')
        for policy in self.policies.values():
            if not set(policy['dependencies']) <= self.policies.keys():
                raise ValueError('Missing policy prerequisite.')

    def eligible(self, language, decision_type):
        return {pid: p for pid, p in self.policies.items()
                if language in p['languages'] and (decision_type in p['decision_types'] or (decision_type == 'unit' and bool(set(p['decision_types']) & {'membership', 'source_head', 'target_head', 'status', 'evidence', 'explanation'})))}

    def families(self, language, decision_type):
        eligible = self.eligible(language, decision_type)
        return {f['id']: {'description': (f.get('descriptions_by_language', {}).get(language, f['description'])
                                         + f' Eligible {language} {decision_type} cards: '
                                         + '; '.join(self.policies[pid]['title'] for pid in f['policies'] if pid in eligible)),
                         'policies': [pid for pid in f['policies'] if pid in eligible]}
                for f in self.index['families'] if any(pid in eligible for pid in f['policies'])}

    def pack(self, selected, language):
        result = {}
        def visit(pid, ancestors):
            if pid in ancestors:
                raise ValueError('Policy prerequisite cycle.')
            p = self.policies[pid]
            if language not in p['languages']:
                raise ValueError('Wrong-language policy.')
            if pid in result:
                return
            for dep in p['dependencies']:
                visit(dep, ancestors | {pid})
            result[pid] = {k: p[k] for k in ('applicability', 'guidance', 'exceptions', 'unresolved_requirements')}
            # Note metadata drives focused explanation questions; the complete
            # policy clauses remain here without duplicating every note template.
            for field in ('evidence_requirements', 'null_target_head_condition'):
                if p.get(field):
                    result[pid][field] = p[field]
        for pid in selected:
            visit(pid, set())
        return result

    def base(self, language):
        return sorted(BASE_MEMBERSHIP & self.eligible(language, 'membership').keys())

    def role(self, pid, language):
        candidate = HEBREW_ROLES.get(pid, pid) if language == 'hebrew' else pid
        if candidate in self.policies and language in self.policies[candidate]['languages']:
            return candidate
        if pid in self.policies and language in self.policies[pid]['languages']:
            return pid
        raise ValueError(f'No {language} policy for structural role {pid}.')

    def family_question(self, language, stage, subject):
        families = self.families(language, stage)
        return {'type': 'choice', 'instructions':
                f'Which guidance family most directly governs {stage} for {subject}? '
                'Select the primary construction from the actual source and supplied English. '
                'No source or target occurrence is excluded by this selection. '
                'Choose GENERAL if the base principles suffice; choose NEEDS_GUIDANCE if the menu does not cover the construction.',
                'criteria': {**{fid: next(f.get('descriptions_by_language', {}).get(language, f['description']) for f in self.index['families'] if f['id'] == fid) for fid in families},
                             'GENERAL': 'The supplied base principles suffice.',
                             'NEEDS_GUIDANCE': 'No defensible guidance family is available.'}}

    def leaf_question(self, language, stage, family, subject):
        candidates = self.families(language, stage)[family]['policies']
        return {'type': 'choice', 'instructions':
                f'Which catalog policy most directly governs {stage} for {subject}? '
                'Select guidance, not an alignment answer. The entire catalog card, including conditions, exceptions, '
                'and prerequisites, will be supplied in the next decision; a partial card does not certify complete contract coverage. '
                'Choose NEEDS_GUIDANCE when these candidates cannot cover the construction.',
                'criteria': {**{pid: self.policies[pid]['applicability'] for pid in candidates},
                             'GENERAL': 'The base principles suffice.',
                             'NEEDS_GUIDANCE': 'No defensible policy is available.'}}

"""Isolated preview: fixed alignment decisions now, resumable notes later."""
from copy import deepcopy

from checkpoint_engine import CheckpointEngine


# Evidence-check identities hold compact JSON bytes; the job stores them as
# tagged UTF-8 so the job is JSON and restores the identical key.
def encode_identity_part(part):
    return {'compact_utf8': part.decode()} if isinstance(part, bytes) else part


def decode_identity_part(part):
    return part['compact_utf8'].encode() if isinstance(part, dict) else part


class DeferredNotesEngine(CheckpointEngine):
    def notes_for_units(self, groups, answers, packs, blocked):
        self.deferred_note_job = deepcopy({
            'version': 1, 'catalog_sha256': self.catalog.sha256,
            'state': self.state, 'required_context': self.required_context,
            'groups': groups, 'answers': answers, 'packs': packs,
            'blocked': sorted(blocked), 'counter': self.counter,
            'ownership_evidence': self.ownership_evidence,
            'subject_scopes': self.subject_scopes,
            'evidence_checks': [{'identity': [encode_identity_part(part) for part in key], 'outcome': value,
                                 'failure': self.evidence_failures.get(key)}
                                for key, value in self.evidence_checks.items()],
            'pending_before_notes': self.pending,
        })
        self.pending.append({'stage': 'notes', 'reason': 'notes_deferred',
                             'units': [group['id'] for group in groups]})
        return {}

    def run(self):
        result = super().run()
        result['notes_status'] = 'deferred'
        result['provisional_alignment'] = True
        return result

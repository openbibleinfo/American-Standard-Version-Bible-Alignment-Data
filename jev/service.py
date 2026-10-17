"""Web entry point: one JSON request on stdin, one JSON result on stdout.

Input: {"ref": "JHN.3.16", "translation": "...", "source": [...]}: one verse,
USX book code and NLT versification (verse 0 is a Psalm title), with its tokens
from jev/source-bundle/ (or, on the site, bible_db.interlinear_source_verses).
Output: {"ok": true, "reader": <reader chapter data>, "flagged": [word keys],
"verse_flagged": bool} or {"ok": false, "error": "..."}.
Nothing is written to disk. The runner's spend bound stops a runaway job.
The OpenRouter key comes from OPENROUTER_API_KEY.
"""
from contextlib import redirect_stdout
import json
import sys
from pathlib import PurePosixPath

from bundle_input import prepare_reference
from deferred_notes_engine import DeferredNotesEngine
from general_engine import GeneralRunner
from policy_runtime import Catalog
from reader_model import reader_data
from run_probe import load_credential
from run_store import MemoryStore


class InputError(ValueError):
    """A problem with the reference or translation, safe to show the user."""


class ServiceRunner(GeneralRunner):
    record_ledger = False

    def make_store(self):
        return MemoryStore()


def flagged(item, members):
    """A review flag as the tokens it concerns; no tokens means the whole verse."""
    ids = [item['unit']] if 'unit' in item else list(item.get('units', []))
    subject = item.get('subject')
    if isinstance(subject, str) and subject.startswith('u'):
        ids.append(subject)
    source = {s for uid in ids for s in members.get(uid, {}).get('source', [])}
    target = {t for uid in ids for t in members.get(uid, {}).get('target', [])}
    if isinstance(subject, str) and subject[:1] in 'st':
        (source if subject.startswith('s') else target).add(subject)
    return {'reason': item['reason'], 'source': sorted(source), 'target': sorted(target)}


def align(ref, translation, source):
    try:
        state, packet, provenance = prepare_reference(ref, translation, tokens=source)
    except ValueError as error:
        raise InputError(str(error)) from None
    load_credential()  # fails fast, before any request, when the key is unset
    catalog = Catalog()
    state['policy_revision'] = catalog.data['revision']
    state['contract'] = next(c['version'] for c in catalog.data['contracts'] if c['language'] == state['language'])
    backend = ServiceRunner(PurePosixPath('/service'), state, True)
    engine = DeferredNotesEngine(state, packet, backend, catalog)
    engine.speculate = True
    try:
        with redirect_stdout(sys.stderr):
            result = engine.run()
    finally:
        with redirect_stdout(sys.stderr):
            backend.close()
    members = {m['id']: m for m in result['proposed_memberships']}
    flags = [flagged(item, members) for item in result['pending'] if item.get('reason') != 'notes_deferred']
    reader, marked = reader_data(state['ref'], state['language'], translation, state, source,
                                 result['proposal']['alignments'], flags)
    # A flag naming no tokens concerns the whole verse.
    return {'ok': True, 'reader': reader, 'flagged': marked,
            'verse_flagged': any(not f['source'] and not f['target'] for f in flags)}


def main():
    try:
        try:
            request = json.loads(sys.stdin.read())
        except ValueError:
            raise InputError('Send a JSON object.') from None
        if not isinstance(request, dict) or set(request) != {'ref', 'translation', 'source'}:
            raise InputError('Send exactly ref, translation and source.')
        output = align(request['ref'], request['translation'], request['source'])
    except InputError as error:
        output = {'ok': False, 'error': str(error)}
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        output = {'ok': False, 'error': 'The alignment could not be completed.'}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
